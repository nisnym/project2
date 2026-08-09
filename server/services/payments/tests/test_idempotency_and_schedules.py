"""Idempotency, and scheduled/recurring transfers."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connections
from django.utils import timezone

from payments import services
from payments.models import Transaction, TransferSchedule, TxnStatus
from platform_common.errors import (
    IdempotencyConflict,
    RequestInProgress,
    ValidationFailed,
)

pytestmark = pytest.mark.django_db


class TestIdempotency:
    def test_replay_returns_the_stored_response(self, peers):
        user = uuid.uuid4()
        key = str(uuid.uuid4())
        body = {"amount": "5000"}
        calls = []

        def run():
            calls.append(1)
            return 201, {"id": str(uuid.uuid4()), "status": "DISPATCHED"}

        first = services.with_idempotency(
            user_id=user, key=key, endpoint="/api/transfers", body=body, run=run
        )
        second = services.with_idempotency(
            user_id=user, key=key, endpoint="/api/transfers", body=body, run=run
        )

        assert first == second      # byte-for-byte the same response
        assert len(calls) == 1      # the work happened once

    def test_missing_key_is_rejected(self, peers):
        """A missing key is a 400, not a courtesy default -- this is the control
        that makes a double-click harmless."""
        with pytest.raises(ValidationFailed, match="Idempotency-Key"):
            services.with_idempotency(
                user_id=uuid.uuid4(), key="", endpoint="/api/transfers",
                body={}, run=lambda: (201, {}),
            )

    def test_same_key_different_body_is_a_conflict(self, peers):
        user, key = uuid.uuid4(), str(uuid.uuid4())
        services.with_idempotency(
            user_id=user, key=key, endpoint="/api/transfers",
            body={"amount": "5000"}, run=lambda: (201, {"id": str(uuid.uuid4())}),
        )
        with pytest.raises(IdempotencyConflict):
            services.with_idempotency(
                user_id=user, key=key, endpoint="/api/transfers",
                body={"amount": "9999"}, run=lambda: (201, {"id": str(uuid.uuid4())}),
            )

    def test_in_flight_duplicate_is_told_to_retry(self, peers):
        from payments.models import IdempotencyRecord

        user, key = uuid.uuid4(), str(uuid.uuid4())
        IdempotencyRecord.objects.create(
            user_id=user, key=key, endpoint="/api/transfers",
            request_hash=services._hash({"amount": "5000"}),
            state=IdempotencyRecord.State.IN_PROGRESS,
        )
        with pytest.raises(RequestInProgress) as exc:
            services.with_idempotency(
                user_id=user, key=key, endpoint="/api/transfers",
                body={"amount": "5000"}, run=lambda: (201, {}),
            )
        assert exc.value.retryable is True

    def test_different_users_may_reuse_a_key(self, peers):
        """Keys are client-chosen; two customers picking the same uuid must not
        collide."""
        key = str(uuid.uuid4())
        for _ in range(2):
            status, _ = services.with_idempotency(
                user_id=uuid.uuid4(), key=key, endpoint="/api/transfers",
                body={"amount": "1"}, run=lambda: (201, {"id": str(uuid.uuid4())}),
            )
            assert status == 201

    def test_the_same_key_never_creates_two_transactions(self, peers):
        user, account = uuid.uuid4(), uuid.uuid4()
        key = str(uuid.uuid4())

        for _ in range(3):
            try:
                services.create_transfer(
                    user_id=user, account_id=account, beneficiary_id=uuid.uuid4(),
                    amount=Decimal("100"), currency="INR", rail="DOMESTIC",
                    idempotency_key=key,
                )
            except Exception:
                pass  # unique constraint does its job

        assert Transaction.objects.filter(user_id=user, idempotency_key=key).count() == 1


@pytest.mark.django_db(transaction=True)
class TestConcurrentIdempotency:
    def test_ten_concurrent_submits_run_the_work_once(self, peers):
        """The double-clicking customer, for real, with threads."""
        user, key = uuid.uuid4(), str(uuid.uuid4())
        body = {"amount": "5000"}
        ran = []
        barrier = threading.Barrier(10)
        outcomes: list = [None] * 10

        def submit(index):
            try:
                barrier.wait(timeout=10)

                def run():
                    ran.append(1)
                    return 201, {"id": "fixed-id", "status": "DISPATCHED"}

                outcomes[index] = services.with_idempotency(
                    user_id=user, key=key, endpoint="/api/transfers", body=body, run=run
                )
            except Exception as exc:
                outcomes[index] = ("error", exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(ran) == 1, f"work ran {len(ran)} times, expected once"


class TestSchedules:
    def _schedule(self, **overrides):
        defaults = dict(
            user_id=uuid.uuid4(), account_id=uuid.uuid4(), beneficiary_id=uuid.uuid4(),
            amount=Decimal("25000"), currency="INR", rail="DOMESTIC",
            frequency=TransferSchedule.Frequency.MONTHLY,
            start_at=timezone.now() - timedelta(minutes=1),
        )
        defaults.update(overrides)
        return services.create_schedule(**defaults)

    def test_due_schedule_is_claimed_and_advanced(self, peers):
        """next_run_at advances *before* dispatch, so a worker crash cannot
        double-fire the occurrence."""
        schedule = self._schedule()
        original = schedule.next_run_at

        claimed = services.claim_due_schedules()

        assert [s.id for s in claimed] == [schedule.id]
        schedule.refresh_from_db()
        assert schedule.next_run_at > original

    def test_a_schedule_is_claimed_only_once(self, peers):
        self._schedule()
        assert len(services.claim_due_schedules()) == 1
        assert len(services.claim_due_schedules()) == 0

    def test_future_schedules_are_not_claimed(self, peers):
        self._schedule(start_at=timezone.now() + timedelta(days=1))
        assert services.claim_due_schedules() == []

    def test_execution_creates_and_runs_a_transfer(self, peers):
        schedule = self._schedule()
        run_at = timezone.now()
        txn = services.execute_scheduled(schedule.id, run_at=run_at)

        assert txn.status == TxnStatus.DISPATCHED
        assert txn.schedule_id == schedule.id
        schedule.refresh_from_db()
        assert schedule.runs_completed == 1

    def test_re_execution_of_one_occurrence_is_free(self, peers):
        """Deterministic idempotency key: a Q2 redelivery must not pay twice."""
        schedule = self._schedule()
        run_at = timezone.now()

        first = services.execute_scheduled(schedule.id, run_at=run_at)
        second = services.execute_scheduled(schedule.id, run_at=run_at)

        assert first.id == second.id
        assert Transaction.objects.filter(schedule_id=schedule.id).count() == 1

    def test_three_consecutive_failures_stop_the_schedule(self, peers):
        """Stop retrying a doomed standing order rather than failing monthly
        forever."""
        peers.fraud_decision = "BLOCK"
        schedule = self._schedule(frequency=TransferSchedule.Frequency.DAILY)

        for day in range(3):
            services.execute_scheduled(
                schedule.id, run_at=timezone.now() + timedelta(days=day)
            )

        schedule.refresh_from_db()
        assert schedule.consecutive_failures == 3
        assert schedule.status == TransferSchedule.Status.FAILED

    def test_a_success_resets_the_failure_counter(self, peers):
        schedule = self._schedule(frequency=TransferSchedule.Frequency.DAILY)
        peers.fraud_decision = "BLOCK"
        services.execute_scheduled(schedule.id, run_at=timezone.now())
        schedule.refresh_from_db()
        assert schedule.consecutive_failures == 1

        peers.fraud_decision = "ALLOW"
        services.execute_scheduled(schedule.id, run_at=timezone.now() + timedelta(days=1))
        schedule.refresh_from_db()
        assert schedule.consecutive_failures == 0

    def test_max_runs_completes_the_schedule(self, peers):
        schedule = self._schedule(frequency=TransferSchedule.Frequency.DAILY, max_runs=2)
        for day in range(2):
            services.execute_scheduled(
                schedule.id, run_at=timezone.now() + timedelta(days=day)
            )
        schedule.refresh_from_db()
        assert schedule.status == TransferSchedule.Status.COMPLETED

    def test_once_completes_after_a_single_run(self, peers):
        schedule = self._schedule(frequency=TransferSchedule.Frequency.ONCE)
        services.execute_scheduled(schedule.id, run_at=timezone.now())
        schedule.refresh_from_db()
        assert schedule.status == TransferSchedule.Status.COMPLETED

    def test_monthly_clamps_to_a_short_month(self, peers):
        """A 31st standing order must still run in February, not skip it."""
        from datetime import datetime, timezone as dt_timezone

        schedule = self._schedule(
            frequency=TransferSchedule.Frequency.MONTHLY,
            start_at=datetime(2026, 1, 31, 9, 0, tzinfo=dt_timezone.utc),
        )
        upcoming = services.next_run(schedule)
        assert upcoming.month == 2
        assert upcoming.day == 28   # clamped, not skipped
