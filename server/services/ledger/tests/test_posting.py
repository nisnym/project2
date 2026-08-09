"""Double-entry posting: the invariants that make this a ledger rather than a
table of numbers."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from ledger.models import AccountKind, Direction, EntryType, JournalEntry, Posting
from ledger.services import (
    InvalidPosting,
    Leg,
    UnbalancedEntry,
    check_invariants,
    post_entry,
    reverse_entry,
)
from platform_common.errors import InsufficientFunds, NotFound

pytestmark = pytest.mark.django_db


def transfer_legs(source, destination, amount="1000.0000", currency="INR"):
    return [
        Leg(str(source.id), Direction.DEBIT, Decimal(amount), currency),
        Leg(str(destination.id), Direction.CREDIT, Decimal(amount), currency),
    ]


class TestValidation:
    def test_rejects_unbalanced_entry(self, account_factory):
        a, b = account_factory(), account_factory()
        with pytest.raises(UnbalancedEntry, match="!="):
            post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[
                    Leg(str(a.id), Direction.DEBIT, Decimal("100"), "INR"),
                    Leg(str(b.id), Direction.CREDIT, Decimal("99"), "INR"),
                ],
                idempotency_key=str(uuid.uuid4()),
            )
        assert not JournalEntry.objects.exists()

    def test_rejects_negative_amount(self, account_factory):
        a, b = account_factory(), account_factory()
        with pytest.raises(InvalidPosting, match="strictly positive"):
            post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[
                    Leg(str(a.id), Direction.DEBIT, Decimal("-100"), "INR"),
                    Leg(str(b.id), Direction.CREDIT, Decimal("-100"), "INR"),
                ],
                idempotency_key=str(uuid.uuid4()),
            )

    def test_rejects_zero_amount(self, account_factory):
        a, b = account_factory(), account_factory()
        with pytest.raises(InvalidPosting):
            post_entry(
                entry_type=EntryType.TRANSFER,
                legs=transfer_legs(a, b, "0"),
                idempotency_key=str(uuid.uuid4()),
            )

    def test_rejects_single_leg(self, account_factory):
        a = account_factory()
        with pytest.raises(InvalidPosting, match="at least two legs"):
            post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[Leg(str(a.id), Direction.DEBIT, Decimal("100"), "INR")],
                idempotency_key=str(uuid.uuid4()),
            )

    def test_rejects_mixed_currencies(self, account_factory):
        inr = account_factory(currency="INR")
        usd = account_factory(currency="USD")
        with pytest.raises(InvalidPosting, match="mix currencies"):
            post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[
                    Leg(str(inr.id), Direction.DEBIT, Decimal("100"), "INR"),
                    Leg(str(usd.id), Direction.CREDIT, Decimal("100"), "USD"),
                ],
                idempotency_key=str(uuid.uuid4()),
            )

    def test_rejects_leg_currency_not_matching_account(self, account_factory):
        a = account_factory(currency="INR")
        b = account_factory(currency="INR")
        with pytest.raises(InvalidPosting, match="is INR, leg is USD"):
            post_entry(
                entry_type=EntryType.TRANSFER,
                legs=transfer_legs(a, b, "100", currency="USD"),
                idempotency_key=str(uuid.uuid4()),
            )

    def test_rejects_unknown_account(self, account_factory):
        a = account_factory()
        with pytest.raises(NotFound):
            post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[
                    Leg(str(a.id), Direction.DEBIT, Decimal("100"), "INR"),
                    Leg(str(uuid.uuid4()), Direction.CREDIT, Decimal("100"), "INR"),
                ],
                idempotency_key=str(uuid.uuid4()),
            )


class TestPosting:
    def test_moves_money_between_customers(self, funded_account, account_factory, balance_of):
        source = funded_account("10000.0000")
        destination = account_factory()

        post_entry(
            entry_type=EntryType.TRANSFER,
            legs=transfer_legs(source, destination, "2500.0000"),
            idempotency_key=str(uuid.uuid4()),
        )

        assert balance_of(source).ledger_balance == Decimal("7500.0000")
        assert balance_of(destination).ledger_balance == Decimal("2500.0000")

    def test_records_balance_after_on_each_posting(self, funded_account, account_factory):
        source = funded_account("10000.0000")
        destination = account_factory()
        entry = post_entry(
            entry_type=EntryType.TRANSFER,
            legs=transfer_legs(source, destination, "2500.0000"),
            idempotency_key=str(uuid.uuid4()),
        )
        debit = entry.postings.get(direction=Direction.DEBIT)
        credit = entry.postings.get(direction=Direction.CREDIT)
        assert debit.balance_after == Decimal("7500.0000")
        assert credit.balance_after == Decimal("2500.0000")

    def test_refuses_to_overdraw_a_customer(self, funded_account, account_factory, balance_of):
        source = funded_account("1000.0000")
        destination = account_factory()

        with pytest.raises(InsufficientFunds):
            post_entry(
                entry_type=EntryType.TRANSFER,
                legs=transfer_legs(source, destination, "5000.0000"),
                idempotency_key=str(uuid.uuid4()),
            )

        # The whole entry rolled back: a partially applied entry would break I1.
        assert balance_of(source).ledger_balance == Decimal("1000.0000")
        assert balance_of(destination).ledger_balance == Decimal("0")
        assert not Posting.objects.filter(ledger_account=destination).exists()

    def test_internal_accounts_may_go_negative(self, account_factory, balance_of):
        """Clearing and nostro accounts legitimately run negative -- refusing
        would make funding from outside the bank impossible."""
        clearing = account_factory(kind=AccountKind.CLEARING)
        customer = account_factory()
        post_entry(
            entry_type=EntryType.FUNDING,
            legs=transfer_legs(clearing, customer, "5000.0000"),
            idempotency_key=str(uuid.uuid4()),
        )
        assert balance_of(customer).ledger_balance == Decimal("5000.0000")

    def test_multi_leg_entry_with_a_fee(self, funded_account, account_factory, balance_of):
        source = funded_account("10000.0000")
        destination = account_factory()
        fees = account_factory(kind=AccountKind.FEE)

        post_entry(
            entry_type=EntryType.TRANSFER,
            legs=[
                Leg(str(source.id), Direction.DEBIT, Decimal("1050.0000"), "INR"),
                Leg(str(destination.id), Direction.CREDIT, Decimal("1000.0000"), "INR"),
                Leg(str(fees.id), Direction.CREDIT, Decimal("50.0000"), "INR"),
            ],
            idempotency_key=str(uuid.uuid4()),
        )
        assert balance_of(source).ledger_balance == Decimal("8950.0000")
        assert balance_of(destination).ledger_balance == Decimal("1000.0000")


class TestIdempotency:
    def test_replaying_the_same_key_returns_the_original(
        self, funded_account, account_factory, balance_of
    ):
        source = funded_account("10000.0000")
        destination = account_factory()
        key = str(uuid.uuid4())
        legs = transfer_legs(source, destination, "1000.0000")

        first = post_entry(entry_type=EntryType.TRANSFER, legs=legs, idempotency_key=key)
        second = post_entry(entry_type=EntryType.TRANSFER, legs=legs, idempotency_key=key)

        assert first.id == second.id
        assert JournalEntry.objects.count() == 2  # the funding seed + this one
        # The money moved exactly once -- this is the control that makes a
        # retry after a lost response safe.
        assert balance_of(source).ledger_balance == Decimal("9000.0000")

    def test_different_keys_post_separately(self, funded_account, account_factory, balance_of):
        source = funded_account("10000.0000")
        destination = account_factory()
        legs = transfer_legs(source, destination, "1000.0000")

        post_entry(entry_type=EntryType.TRANSFER, legs=legs, idempotency_key=str(uuid.uuid4()))
        post_entry(entry_type=EntryType.TRANSFER, legs=legs, idempotency_key=str(uuid.uuid4()))

        assert balance_of(source).ledger_balance == Decimal("8000.0000")


class TestReversal:
    def test_reversal_restores_balances(self, funded_account, account_factory, balance_of):
        source = funded_account("10000.0000")
        destination = account_factory()
        entry = post_entry(
            entry_type=EntryType.TRANSFER,
            legs=transfer_legs(source, destination, "3000.0000"),
            idempotency_key=str(uuid.uuid4()),
        )

        reverse_entry(entry.id, reason="rail returned", idempotency_key=f"rev:{entry.id}")

        assert balance_of(source).ledger_balance == Decimal("10000.0000")
        assert balance_of(destination).ledger_balance == Decimal("0")

    def test_reversal_is_a_new_entry_not_an_edit(self, funded_account, account_factory):
        source = funded_account("10000.0000")
        destination = account_factory()
        entry = post_entry(
            entry_type=EntryType.TRANSFER,
            legs=transfer_legs(source, destination, "3000.0000"),
            idempotency_key=str(uuid.uuid4()),
        )
        reversal = reverse_entry(entry.id, reason="x", idempotency_key=f"rev:{entry.id}")

        entry.refresh_from_db()
        assert reversal.id != entry.id
        assert reversal.reverses_id == entry.id
        assert reversal.entry_type == EntryType.REVERSAL
        # Original postings untouched -- the audit trail stays intact.
        assert entry.postings.count() == 2

    def test_reversal_is_idempotent(self, funded_account, account_factory, balance_of):
        source = funded_account("10000.0000")
        destination = account_factory()
        entry = post_entry(
            entry_type=EntryType.TRANSFER,
            legs=transfer_legs(source, destination, "3000.0000"),
            idempotency_key=str(uuid.uuid4()),
        )
        first = reverse_entry(entry.id, reason="x", idempotency_key=f"rev:{entry.id}")
        second = reverse_entry(entry.id, reason="x", idempotency_key=f"rev:{entry.id}")

        assert first.id == second.id
        assert balance_of(source).ledger_balance == Decimal("10000.0000")

    def test_reversal_may_overdraw_the_recipient(self, funded_account, account_factory, balance_of):
        """The recipient may have already spent the money. A return must still
        succeed -- otherwise the funds are stuck in limbo."""
        source = funded_account("10000.0000")
        destination = account_factory()
        onward = account_factory()

        entry = post_entry(
            entry_type=EntryType.TRANSFER,
            legs=transfer_legs(source, destination, "3000.0000"),
            idempotency_key=str(uuid.uuid4()),
        )
        # Recipient spends it all.
        post_entry(
            entry_type=EntryType.TRANSFER,
            legs=transfer_legs(destination, onward, "3000.0000"),
            idempotency_key=str(uuid.uuid4()),
        )
        assert balance_of(destination).ledger_balance == Decimal("0")

        reverse_entry(entry.id, reason="recall", idempotency_key=f"rev:{entry.id}")
        assert balance_of(destination).ledger_balance == Decimal("-3000.0000")

    def test_reversing_an_unknown_entry_raises(self):
        with pytest.raises(NotFound):
            reverse_entry(uuid.uuid4(), reason="x", idempotency_key="k")


class TestInvariants:
    def test_debits_equal_credits_after_many_entries(self, funded_account, account_factory):
        source = funded_account("100000.0000")
        others = [account_factory() for _ in range(5)]
        for index, destination in enumerate(others):
            post_entry(
                entry_type=EntryType.TRANSFER,
                legs=transfer_legs(source, destination, f"{(index + 1) * 100}.0000"),
                idempotency_key=str(uuid.uuid4()),
            )

        checks = {c.check_name: c for c in check_invariants()}
        assert checks["DEBITS_EQUAL_CREDITS"].ok
        assert checks["BALANCE_MATCHES_POSTINGS"].ok

    def test_invariant_check_detects_tampered_balance(self, funded_account, balance_of):
        """Simulates data corruption or a direct UPDATE that bypassed the
        service layer."""
        from ledger.models import Balance

        source = funded_account("10000.0000")
        Balance.objects.filter(ledger_account=source).update(
            ledger_balance=Decimal("99999.0000")
        )

        checks = {c.check_name: c for c in check_invariants()}
        assert not checks["BALANCE_MATCHES_POSTINGS"].ok
        assert checks["BALANCE_MATCHES_POSTINGS"].detail["drifted"]

    def test_invariants_hold_per_currency(self, account_factory):
        for currency in ("INR", "USD"):
            clearing = account_factory(kind=AccountKind.CLEARING, currency=currency)
            customer = account_factory(currency=currency)
            post_entry(
                entry_type=EntryType.FUNDING,
                legs=transfer_legs(clearing, customer, "1000.0000", currency),
                idempotency_key=str(uuid.uuid4()),
            )
        results = [c for c in check_invariants() if c.check_name == "DEBITS_EQUAL_CREDITS"]
        assert len(results) == 2
        assert all(c.ok for c in results)
