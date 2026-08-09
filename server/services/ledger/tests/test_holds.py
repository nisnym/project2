"""Hold lifecycle: reserve -> capture / release / expire.

Reserve-then-capture exists so that two concurrent transfers cannot spend the
same funds while one of them is being fraud-screened (I4).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from ledger.models import Direction, EntryType, Hold, HoldStatus
from ledger.services import (
    HoldNotActive,
    InvalidPosting,
    Leg,
    capture_hold,
    expire_holds,
    place_hold,
    release_hold,
    reverse_entry,
)
from platform_common.errors import InsufficientFunds, NotFound

pytestmark = pytest.mark.django_db


def hold_for(account, amount="1000.0000", txn_ref=None, key=None, ttl=30):
    return place_hold(
        ledger_account_id=account.id,
        amount=Decimal(amount),
        currency="INR",
        txn_ref=txn_ref or uuid.uuid4(),
        idempotency_key=key or str(uuid.uuid4()),
        ttl_minutes=ttl,
    )


class TestPlaceHold:
    def test_reduces_available_but_not_ledger_balance(self, funded_account, balance_of):
        account = funded_account("10000.0000")
        hold_for(account, "3000.0000")

        balance = balance_of(account)
        assert balance.ledger_balance == Decimal("10000.0000")  # money hasn't moved
        assert balance.held == Decimal("3000.0000")
        assert balance.available == Decimal("7000.0000")

    def test_refuses_when_available_is_insufficient(self, funded_account, balance_of):
        account = funded_account("1000.0000")
        with pytest.raises(InsufficientFunds) as exc:
            hold_for(account, "5000.0000")
        assert exc.value.extra["available"] == "1000.0000"
        assert balance_of(account).held == Decimal("0")

    def test_second_hold_sees_the_first_one(self, funded_account):
        """The whole point: a hold is visible to the next request immediately."""
        account = funded_account("10000.0000")
        hold_for(account, "6000.0000")
        with pytest.raises(InsufficientFunds):
            hold_for(account, "6000.0000")

    def test_holds_can_stack_up_to_the_balance(self, funded_account, balance_of):
        account = funded_account("10000.0000")
        hold_for(account, "4000.0000")
        hold_for(account, "6000.0000")
        assert balance_of(account).available == Decimal("0")

    def test_is_idempotent(self, funded_account, balance_of):
        account = funded_account("10000.0000")
        key = str(uuid.uuid4())
        first = hold_for(account, "3000.0000", key=key)
        second = hold_for(account, "3000.0000", key=key)

        assert first.id == second.id
        # Held exactly once -- a retried PLACE_HOLD must not reserve twice.
        assert balance_of(account).held == Decimal("3000.0000")

    def test_rejects_non_positive_amount(self, funded_account):
        account = funded_account("10000.0000")
        with pytest.raises(InvalidPosting):
            hold_for(account, "0")

    def test_rejects_currency_mismatch(self, funded_account):
        account = funded_account("10000.0000")
        with pytest.raises(InvalidPosting, match="account is INR"):
            place_hold(
                ledger_account_id=account.id, amount=Decimal("10"), currency="USD",
                txn_ref=uuid.uuid4(), idempotency_key=str(uuid.uuid4()),
            )

    def test_rejects_unknown_account(self):
        with pytest.raises(NotFound):
            place_hold(
                ledger_account_id=uuid.uuid4(), amount=Decimal("10"), currency="INR",
                txn_ref=uuid.uuid4(), idempotency_key=str(uuid.uuid4()),
            )


class TestCapture:
    def test_moves_the_money_and_clears_the_hold(
        self, funded_account, account_factory, balance_of
    ):
        source = funded_account("10000.0000")
        destination = account_factory()
        hold = hold_for(source, "3000.0000")

        entry = capture_hold(
            hold.id,
            legs=[
                Leg(str(source.id), Direction.DEBIT, Decimal("3000.0000"), "INR"),
                Leg(str(destination.id), Direction.CREDIT, Decimal("3000.0000"), "INR"),
            ],
            idempotency_key=f"capture:{hold.txn_ref}",
        )

        source_balance = balance_of(source)
        assert source_balance.ledger_balance == Decimal("7000.0000")
        assert source_balance.held == Decimal("0")
        assert source_balance.available == Decimal("7000.0000")
        assert balance_of(destination).ledger_balance == Decimal("3000.0000")

        hold.refresh_from_db()
        assert hold.status == HoldStatus.CAPTURED
        assert hold.captured_by_id == entry.id

    def test_capture_is_idempotent(self, funded_account, account_factory, balance_of):
        source = funded_account("10000.0000")
        destination = account_factory()
        hold = hold_for(source, "3000.0000")
        legs = [
            Leg(str(source.id), Direction.DEBIT, Decimal("3000.0000"), "INR"),
            Leg(str(destination.id), Direction.CREDIT, Decimal("3000.0000"), "INR"),
        ]
        key = f"capture:{hold.txn_ref}"

        first = capture_hold(hold.id, legs=legs, idempotency_key=key)
        second = capture_hold(hold.id, legs=legs, idempotency_key=key)

        assert first.id == second.id
        assert balance_of(source).ledger_balance == Decimal("7000.0000")

    def test_capturing_a_released_hold_raises(self, funded_account, account_factory):
        source = funded_account("10000.0000")
        destination = account_factory()
        hold = hold_for(source, "3000.0000")
        release_hold(hold.id)

        with pytest.raises(HoldNotActive):
            capture_hold(
                hold.id,
                legs=[
                    Leg(str(source.id), Direction.DEBIT, Decimal("3000.0000"), "INR"),
                    Leg(str(destination.id), Direction.CREDIT, Decimal("3000.0000"), "INR"),
                ],
                idempotency_key=str(uuid.uuid4()),
            )

    def test_capture_of_the_full_balance_succeeds(
        self, funded_account, account_factory, balance_of
    ):
        """Regression guard: releasing the hold must happen before posting, or
        the overdraft check trips on the customer's own reserved funds."""
        source = funded_account("5000.0000")
        destination = account_factory()
        hold = hold_for(source, "5000.0000")

        capture_hold(
            hold.id,
            legs=[
                Leg(str(source.id), Direction.DEBIT, Decimal("5000.0000"), "INR"),
                Leg(str(destination.id), Direction.CREDIT, Decimal("5000.0000"), "INR"),
            ],
            idempotency_key=str(uuid.uuid4()),
        )
        assert balance_of(source).ledger_balance == Decimal("0")
        assert balance_of(source).held == Decimal("0")


class TestRelease:
    def test_returns_the_reservation(self, funded_account, balance_of):
        account = funded_account("10000.0000")
        hold = hold_for(account, "3000.0000")
        release_hold(hold.id)

        balance = balance_of(account)
        assert balance.held == Decimal("0")
        assert balance.available == Decimal("10000.0000")
        assert balance.ledger_balance == Decimal("10000.0000")  # no money moved

    def test_is_idempotent(self, funded_account, balance_of):
        account = funded_account("10000.0000")
        hold = hold_for(account, "3000.0000")
        release_hold(hold.id)
        release_hold(hold.id)
        assert balance_of(account).held == Decimal("0")

    def test_releasing_a_captured_hold_directs_you_to_reversal(
        self, funded_account, account_factory
    ):
        source = funded_account("10000.0000")
        destination = account_factory()
        hold = hold_for(source, "3000.0000")
        capture_hold(
            hold.id,
            legs=[
                Leg(str(source.id), Direction.DEBIT, Decimal("3000.0000"), "INR"),
                Leg(str(destination.id), Direction.CREDIT, Decimal("3000.0000"), "INR"),
            ],
            idempotency_key=str(uuid.uuid4()),
        )
        with pytest.raises(HoldNotActive, match="reverse the journal entry"):
            release_hold(hold.id)


class TestExpiry:
    def test_expires_holds_past_their_ttl(self, funded_account, balance_of):
        """The crash safety net: if the saga dies between PLACE_HOLD and
        CAPTURE, the customer's money must not be stranded."""
        account = funded_account("10000.0000")
        hold = hold_for(account, "3000.0000")
        Hold.objects.filter(pk=hold.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        assert expire_holds() == {"found": 1, "expired": 1}

        hold.refresh_from_db()
        assert hold.status == HoldStatus.EXPIRED
        assert balance_of(account).available == Decimal("10000.0000")

    def test_leaves_live_holds_alone(self, funded_account, balance_of):
        account = funded_account("10000.0000")
        hold_for(account, "3000.0000", ttl=30)
        assert expire_holds() == {"found": 0, "expired": 0}
        assert balance_of(account).held == Decimal("3000.0000")

    def test_does_not_touch_captured_holds(self, funded_account, account_factory):
        source = funded_account("10000.0000")
        destination = account_factory()
        hold = hold_for(source, "3000.0000")
        capture_hold(
            hold.id,
            legs=[
                Leg(str(source.id), Direction.DEBIT, Decimal("3000.0000"), "INR"),
                Leg(str(destination.id), Direction.CREDIT, Decimal("3000.0000"), "INR"),
            ],
            idempotency_key=str(uuid.uuid4()),
        )
        Hold.objects.filter(pk=hold.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        assert expire_holds()["expired"] == 0
        hold.refresh_from_db()
        assert hold.status == HoldStatus.CAPTURED


class TestFullSagaShape:
    """The sequences payments-svc will actually drive."""

    def test_approved_transfer(self, funded_account, account_factory, balance_of):
        source = funded_account("10000.0000")
        destination = account_factory()
        txn = uuid.uuid4()

        hold = place_hold(
            ledger_account_id=source.id, amount=Decimal("2500.0000"), currency="INR",
            txn_ref=txn, idempotency_key=str(txn),
        )
        capture_hold(
            hold.id,
            legs=[
                Leg(str(source.id), Direction.DEBIT, Decimal("2500.0000"), "INR"),
                Leg(str(destination.id), Direction.CREDIT, Decimal("2500.0000"), "INR"),
            ],
            idempotency_key=f"capture:{txn}",
        )
        assert balance_of(source).available == Decimal("7500.0000")
        assert balance_of(destination).ledger_balance == Decimal("2500.0000")

    def test_blocked_transfer_leaves_the_balance_untouched(self, funded_account, balance_of):
        source = funded_account("10000.0000")
        txn = uuid.uuid4()

        hold = place_hold(
            ledger_account_id=source.id, amount=Decimal("2500.0000"), currency="INR",
            txn_ref=txn, idempotency_key=str(txn),
        )
        release_hold(hold.id, reason="fraud blocked")

        balance = balance_of(source)
        assert balance.ledger_balance == Decimal("10000.0000")
        assert balance.available == Decimal("10000.0000")

    def test_returned_transfer_is_compensated_by_reversal(
        self, funded_account, account_factory, balance_of
    ):
        source = funded_account("10000.0000")
        clearing = account_factory(kind="CLEARING")
        txn = uuid.uuid4()

        hold = place_hold(
            ledger_account_id=source.id, amount=Decimal("2500.0000"), currency="INR",
            txn_ref=txn, idempotency_key=str(txn),
        )
        entry = capture_hold(
            hold.id,
            legs=[
                Leg(str(source.id), Direction.DEBIT, Decimal("2500.0000"), "INR"),
                Leg(str(clearing.id), Direction.CREDIT, Decimal("2500.0000"), "INR"),
            ],
            idempotency_key=f"capture:{txn}",
            entry_type=EntryType.TRANSFER,
        )
        assert balance_of(source).ledger_balance == Decimal("7500.0000")

        reverse_entry(entry.id, reason="BENEFICIARY_ACCOUNT_CLOSED",
                      idempotency_key=f"rev:{txn}")
        assert balance_of(source).ledger_balance == Decimal("10000.0000")
