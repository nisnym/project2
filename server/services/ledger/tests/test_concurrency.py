"""Concurrency: the properties a single-threaded test can never demonstrate.

These use transaction=True (a real TransactionTestCase) so each thread commits
independently and genuinely contends on Postgres row locks. They are the tests
that would catch a missing select_for_update or a wrong lock order -- bugs that
pass every sequential test and then lose money in production.
"""

from __future__ import annotations

import threading
import uuid
from decimal import Decimal

import pytest
from django.db import connection, connections

from ledger.models import AccountKind, Balance, Direction, EntryType, JournalEntry, Posting
from ledger.services import Leg, capture_hold, get_or_create_account, place_hold, post_entry
from platform_common.errors import InsufficientFunds

pytestmark = pytest.mark.django_db(transaction=True)


def run_concurrently(target, count: int):
    """Run ``target`` in ``count`` threads, released together by a barrier.

    The barrier maximises real contention; without it the threads tend to run
    one after another and the test proves nothing.
    """
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def worker(index: int):
        try:
            barrier.wait(timeout=10)
            results[index] = ("ok", target(index))
        except Exception as exc:
            results[index] = ("error", exc)
        finally:
            # Each thread gets its own connection; leaking them exhausts the
            # pool and makes later tests fail confusingly.
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


@pytest.fixture
def seeded(django_db_blocker):
    """Build accounts outside the per-test transaction so threads can see them."""

    def make(amount="1000.0000", currency="INR"):
        ref = uuid.uuid4()
        customer = get_or_create_account(
            code=f"CUST:{ref}", kind=AccountKind.CUSTOMER, currency=currency, account_ref=ref
        )
        # A zero-amount entry is (correctly) rejected by the ledger, so an
        # empty account is simply left unfunded.
        if Decimal(amount) > 0:
            clearing = get_or_create_account(
                code=f"INTERNAL:CLEARING:{uuid.uuid4().hex[:6]}",
                kind=AccountKind.CLEARING, currency=currency,
            )
            post_entry(
                entry_type=EntryType.FUNDING,
                legs=[
                    Leg(str(clearing.id), Direction.DEBIT, Decimal(amount), currency),
                    Leg(str(customer.id), Direction.CREDIT, Decimal(amount), currency),
                ],
                idempotency_key=f"seed:{uuid.uuid4()}",
            )
        return customer

    return make


class TestNoOverdraftUnderConcurrency:
    def test_two_transfers_that_jointly_overdraw_leave_exactly_one_winner(self, seeded):
        """The invariant a demo cannot show but a regulator will ask about.

        Balance 1000; two concurrent transfers of 800. Exactly one must succeed
        and the balance must land on 200 -- never -600.
        """
        source = seeded("1000.0000")
        destination = seeded("0.0000")

        def transfer(_index):
            return post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[
                    Leg(str(source.id), Direction.DEBIT, Decimal("800.0000"), "INR"),
                    Leg(str(destination.id), Direction.CREDIT, Decimal("800.0000"), "INR"),
                ],
                idempotency_key=str(uuid.uuid4()),
            )

        results = run_concurrently(transfer, 2)
        outcomes = [kind for kind, _ in results]

        assert outcomes.count("ok") == 1, f"expected exactly one winner, got {results}"
        assert outcomes.count("error") == 1
        assert isinstance(results[outcomes.index("error")][1], InsufficientFunds)

        balance = Balance.objects.get(ledger_account=source)
        assert balance.ledger_balance == Decimal("200.0000")
        assert balance.ledger_balance >= 0

    def test_five_way_contention_never_goes_negative(self, seeded):
        source = seeded("1000.0000")
        destination = seeded("0.0000")

        def transfer(_index):
            return post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[
                    Leg(str(source.id), Direction.DEBIT, Decimal("300.0000"), "INR"),
                    Leg(str(destination.id), Direction.CREDIT, Decimal("300.0000"), "INR"),
                ],
                idempotency_key=str(uuid.uuid4()),
            )

        results = run_concurrently(transfer, 5)
        succeeded = sum(1 for kind, _ in results if kind == "ok")

        assert succeeded == 3  # 1000 / 300
        balance = Balance.objects.get(ledger_account=source)
        assert balance.ledger_balance == Decimal("100.0000")

    def test_concurrent_holds_cannot_over_reserve(self, seeded):
        source = seeded("1000.0000")

        def reserve(_index):
            return place_hold(
                ledger_account_id=source.id, amount=Decimal("600.0000"), currency="INR",
                txn_ref=uuid.uuid4(), idempotency_key=str(uuid.uuid4()),
            )

        results = run_concurrently(reserve, 2)
        succeeded = sum(1 for kind, _ in results if kind == "ok")

        assert succeeded == 1
        balance = Balance.objects.get(ledger_account=source)
        assert balance.held == Decimal("600.0000")
        assert balance.available == Decimal("400.0000")


class TestIdempotencyUnderConcurrency:
    def test_the_same_key_submitted_ten_times_debits_once(self, seeded):
        """A double-clicking customer, or a client retrying a lost response."""
        source = seeded("1000.0000")
        destination = seeded("0.0000")
        shared_key = str(uuid.uuid4())

        def transfer(_index):
            return post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[
                    Leg(str(source.id), Direction.DEBIT, Decimal("100.0000"), "INR"),
                    Leg(str(destination.id), Direction.CREDIT, Decimal("100.0000"), "INR"),
                ],
                idempotency_key=shared_key,
            )

        results = run_concurrently(transfer, 10)
        entries = {
            str(value.id) for kind, value in results
            if kind == "ok" and isinstance(value, JournalEntry)
        }

        # Every thread that succeeded got the SAME journal entry back.
        assert len(entries) == 1, f"expected one entry, got {entries}"
        assert JournalEntry.objects.filter(idempotency_key=shared_key).count() == 1
        assert Posting.objects.filter(journal_entry__idempotency_key=shared_key).count() == 2

        balance = Balance.objects.get(ledger_account=source)
        assert balance.ledger_balance == Decimal("900.0000")

    def test_concurrent_duplicate_holds_reserve_once(self, seeded):
        source = seeded("1000.0000")
        shared_key = str(uuid.uuid4())
        txn = uuid.uuid4()

        def reserve(_index):
            return place_hold(
                ledger_account_id=source.id, amount=Decimal("200.0000"), currency="INR",
                txn_ref=txn, idempotency_key=shared_key,
            )

        run_concurrently(reserve, 5)
        balance = Balance.objects.get(ledger_account=source)
        assert balance.held == Decimal("200.0000")


class TestDeadlockAvoidance:
    def test_opposing_transfers_between_the_same_pair_do_not_deadlock(self, seeded):
        """A->B and B->A at the same instant.

        Without deterministic lock ordering this deadlocks: one transaction
        holds A and wants B while the other holds B and wants A. post_entry
        sorts account ids before locking, which makes that impossible.
        """
        alice = seeded("5000.0000")
        bob = seeded("5000.0000")

        def cross_transfer(index):
            source, destination = (alice, bob) if index % 2 == 0 else (bob, alice)
            return post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[
                    Leg(str(source.id), Direction.DEBIT, Decimal("100.0000"), "INR"),
                    Leg(str(destination.id), Direction.CREDIT, Decimal("100.0000"), "INR"),
                ],
                idempotency_key=str(uuid.uuid4()),
            )

        results = run_concurrently(cross_transfer, 6)
        errors = [value for kind, value in results if kind == "error"]

        assert not errors, f"expected no deadlocks, got {errors}"
        # Three each way: both balances return to where they started.
        assert Balance.objects.get(ledger_account=alice).ledger_balance == Decimal("5000.0000")
        assert Balance.objects.get(ledger_account=bob).ledger_balance == Decimal("5000.0000")

    def test_money_is_conserved_across_a_chaotic_mix(self, seeded):
        """Whatever the interleaving, the total across all accounts is constant."""
        accounts = [seeded("2000.0000") for _ in range(4)]
        total_before = sum(
            Balance.objects.get(ledger_account=a).ledger_balance for a in accounts
        )

        def random_transfer(index):
            source = accounts[index % len(accounts)]
            destination = accounts[(index + 1) % len(accounts)]
            return post_entry(
                entry_type=EntryType.TRANSFER,
                legs=[
                    Leg(str(source.id), Direction.DEBIT, Decimal("250.0000"), "INR"),
                    Leg(str(destination.id), Direction.CREDIT, Decimal("250.0000"), "INR"),
                ],
                idempotency_key=str(uuid.uuid4()),
            )

        run_concurrently(random_transfer, 8)

        total_after = sum(
            Balance.objects.get(ledger_account=a).ledger_balance for a in accounts
        )
        assert total_after == total_before

        debits = Posting.objects.filter(direction=Direction.DEBIT).aggregate(
            t=__import__("django.db.models", fromlist=["Sum"]).Sum("amount")
        )["t"]
        credits = Posting.objects.filter(direction=Direction.CREDIT).aggregate(
            t=__import__("django.db.models", fromlist=["Sum"]).Sum("amount")
        )["t"]
        assert debits == credits
