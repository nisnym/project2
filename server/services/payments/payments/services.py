"""Payments domain logic: create orders, run the saga, cancel, schedule.

The only module that opens transaction.atomic().
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from platform_common.errors import (
    Conflict,
    IdempotencyConflict,
    IllegalStateTransition,
    NotFound,
    RequestInProgress,
    ValidationFailed,
)
from platform_common.events import EventEnvelope, publish
from platform_common.observability.context import get_correlation_id

from . import clients
from .models import (
    IdempotencyRecord,
    Rail,
    Transaction,
    TransferSchedule,
    TxnStatus,
    TxnType,
)
from .saga import compensate, make_reference, run_saga, set_status, _emit

logger = logging.getLogger(__name__)


def _hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


# ------------------------------------------------------------- idempotency


def with_idempotency(*, user_id, key: str, endpoint: str, body: dict, run):
    """Run ``run`` at most once per (user, key, endpoint).

    A replay returns the *stored original response*, which is what makes a
    double-click harmless. Same key with a different body is a client bug and
    returns 409 rather than silently replaying something else.
    """
    if not key:
        raise ValidationFailed(
            "Idempotency-Key header is required for this endpoint.",
            code="IDEMPOTENCY_KEY_REQUIRED",
        )

    request_hash = _hash(body)
    try:
        with transaction.atomic():
            record = IdempotencyRecord.objects.create(
                user_id=user_id, key=key, endpoint=endpoint, request_hash=request_hash
            )
    except IntegrityError:
        record = IdempotencyRecord.objects.get(user_id=user_id, key=key, endpoint=endpoint)
        if record.request_hash != request_hash:
            raise IdempotencyConflict(
                "This Idempotency-Key was already used with a different payload.",
                detail={"key": key},
            )
        if record.state == IdempotencyRecord.State.IN_PROGRESS:
            raise RequestInProgress(
                "An identical request is still being processed.", detail={"key": key}
            )
        return record.status_code, record.response_body

    status_code, response = run()

    record.state = IdempotencyRecord.State.COMPLETE
    record.status_code = status_code
    record.response_body = response
    record.transaction_id = response.get("id")
    record.completed_at = timezone.now()
    record.save(update_fields=["state", "status_code", "response_body",
                               "transaction_id", "completed_at"])
    return status_code, response


# ---------------------------------------------------------------- creation


def create_transfer(*, user_id, account_id, beneficiary_id, amount: Decimal,
                    currency: str, rail: str, idempotency_key: str,
                    remarks: str = "", purpose_code: str = "",
                    context: dict | None = None, schedule_id=None) -> Transaction:
    if Decimal(amount) <= 0:
        raise ValidationFailed("Amount must be greater than zero.")

    reference = make_reference(TxnType.TRANSFER)
    with transaction.atomic():
        txn = Transaction.objects.create(
            reference=reference,
            # The shared reference both sides of an internal transfer quote. On
            # the sender's row it equals their own reference; the recipient's
            # mirrored row borrows it.
            transfer_ref=reference,
            user_id=user_id,
            account_id=account_id,
            txn_type=TxnType.TRANSFER,
            rail=rail,
            direction="DEBIT",
            amount=Decimal(amount),
            currency=currency,
            beneficiary_id=beneficiary_id,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id() or "",
            remarks=remarks,
            purpose_code=purpose_code,
            context=context or {},
            schedule_id=schedule_id,
        )
        _emit(txn, "payment.initiated")
    return txn


def create_funding(*, user_id, account_id, funding_source_id, amount: Decimal,
                   currency: str, rail: str, idempotency_key: str,
                   context: dict | None = None) -> Transaction:
    if Decimal(amount) <= 0:
        raise ValidationFailed("Amount must be greater than zero.")

    reference = make_reference(TxnType.FUNDING)
    with transaction.atomic():
        txn = Transaction.objects.create(
            reference=reference,
            transfer_ref=reference,
            user_id=user_id,
            account_id=account_id,
            txn_type=TxnType.FUNDING,
            rail=rail,
            direction="CREDIT",
            amount=Decimal(amount),
            currency=currency,
            funding_source_id=funding_source_id,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id() or "",
            context=context or {},
        )
        _emit(txn, "payment.initiated")
    return txn


def submit(txn: Transaction) -> Transaction:
    """Run the saga for a freshly created order."""
    return run_saga(txn)


# ------------------------------------------------------------- cancellation


def cancel(txn_id, *, user_id, reason: str = "cancelled by customer") -> Transaction:
    """Cancel before the instruction leaves the building.

    Once DISPATCHED the rail has it; the only remedy then is a recall, which is
    a different (and much slower) process.
    """
    with transaction.atomic():
        txn = Transaction.objects.select_for_update().filter(pk=txn_id).first()
        if txn is None:
            raise NotFound("Transaction not found.")
        if str(txn.user_id) != str(user_id):
            raise NotFound("Transaction not found.")  # don't confirm existence
        if not txn.is_cancellable:
            raise IllegalStateTransition(
                "This transfer can no longer be cancelled.",
                detail={
                    "current_status": txn.status,
                    "cancellable_from": sorted(
                        s.value for s in TxnStatus if s in
                        {TxnStatus.INITIATED, TxnStatus.VALIDATED,
                         TxnStatus.RESERVED, TxnStatus.UNDER_REVIEW}
                    ),
                },
            )

        # Unwind whatever the saga had already done: hold, limit reservation.
        compensate(txn, upto="SCREEN", reason=reason)
        if txn.status != TxnStatus.COMPENSATION_PENDING:
            set_status(txn, TxnStatus.CANCELLED, reason)
            _emit(txn, "payment.cancelled", {"hold_released": bool(txn.hold_id)})
    return txn


# ---------------------------------------------- analyst-driven resumption


def resume_after_approval(txn_id, *, analyst_id: str) -> Transaction:
    """An analyst approved the case: continue from CAPTURE.

    The hold is still in place, so funds do not need re-checking -- that is
    exactly why REVIEW retains it.
    """
    with transaction.atomic():
        txn = Transaction.objects.select_for_update().filter(pk=txn_id).first()
        if txn is None:
            logger.warning("approval for unknown transaction %s", txn_id)
            return None
        if txn.status != TxnStatus.UNDER_REVIEW:
            # Order-tolerant: a duplicate or late event changes nothing.
            logger.info("ignoring approval for %s in status %s", txn.reference, txn.status)
            return txn
        set_status(txn, TxnStatus.APPROVED, f"approved by {analyst_id}")
        _emit(txn, "payment.approved")

    return run_saga(txn, from_step="CAPTURE")


def resume_after_rejection(txn_id, *, analyst_id: str) -> Transaction:
    """An analyst confirmed the block: release the hold, mark BLOCKED."""
    with transaction.atomic():
        txn = Transaction.objects.select_for_update().filter(pk=txn_id).first()
        if txn is None:
            return None
        if txn.status != TxnStatus.UNDER_REVIEW:
            return txn
        compensate(txn, upto="SCREEN", reason="FRAUD_CONFIRMED")
        if txn.status != TxnStatus.COMPENSATION_PENDING:
            set_status(txn, TxnStatus.BLOCKED, f"rejected by {analyst_id}")
            _emit(txn, "payment.blocked", {"hold_released": True})
    return txn


# ------------------------------------------------------------ rail outcomes


def settle_from_rail(txn_id, *, rail_ref: str = "") -> Transaction:
    with transaction.atomic():
        txn = Transaction.objects.select_for_update().filter(pk=txn_id).first()
        if txn is None or txn.status not in (TxnStatus.DISPATCHED, TxnStatus.POSTED):
            return txn
        if rail_ref:
            txn.rail_ref = rail_ref
            txn.save(update_fields=["rail_ref"])
        set_status(txn, TxnStatus.SETTLED)
        _emit(txn, "payment.settled")
    return txn


def return_from_rail(txn_id, *, reason: str) -> Transaction:
    """The rail sent it back. Compensate with a reversing entry, never an edit."""
    with transaction.atomic():
        txn = Transaction.objects.select_for_update().filter(pk=txn_id).first()
        if txn is None or txn.status in (TxnStatus.REVERSED, TxnStatus.RETURNED):
            return txn

        set_status(txn, TxnStatus.RETURNED, reason)
        compensate(txn, upto="DISPATCH", reason=reason)
        if txn.status != TxnStatus.COMPENSATION_PENDING:
            set_status(txn, TxnStatus.REVERSED, reason)
            _emit(txn, "payment.returned", {"return_reason": reason, "funds_returned": True})
    return txn


# --------------------------------------------------------------- schedules


def next_run(schedule: TransferSchedule, *, after=None):
    base = after or schedule.next_run_at
    if schedule.frequency == TransferSchedule.Frequency.DAILY:
        return base + timedelta(days=1)
    if schedule.frequency == TransferSchedule.Frequency.WEEKLY:
        return base + timedelta(weeks=1)
    if schedule.frequency == TransferSchedule.Frequency.MONTHLY:
        # Clamp to the last valid day: a 31st standing order must still run in
        # February rather than silently skipping the month.
        month = base.month + 1
        year = base.year + (month > 12)
        month = 1 if month > 12 else month
        day = min(base.day, _days_in_month(year, month))
        return base.replace(year=year, month=month, day=day)
    return None  # ONCE


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


def create_schedule(*, user_id, account_id, beneficiary_id, amount: Decimal,
                    currency: str, rail: str, frequency: str, start_at,
                    end_date: date | None = None, max_runs: int | None = None,
                    remarks: str = "") -> TransferSchedule:
    with transaction.atomic():
        schedule = TransferSchedule.objects.create(
            user_id=user_id, account_id=account_id, beneficiary_id=beneficiary_id,
            amount=Decimal(amount), currency=currency, rail=rail,
            frequency=frequency, next_run_at=start_at, end_date=end_date,
            max_runs=max_runs, remarks=remarks,
        )
        publish(
            EventEnvelope(
                event_type="schedule.created",
                aggregate_type="transfer_schedule",
                aggregate_id=str(schedule.id),
                sequence=0,
                producer="payments",
                correlation_id=get_correlation_id() or "",
                payload={
                    "schedule_id": str(schedule.id),
                    "user_id": str(user_id),
                    "amount": {"amount": str(amount), "currency": currency},
                    "frequency": frequency,
                    "next_run_at": start_at.isoformat(),
                },
            )
        )
    return schedule


def claim_due_schedules(limit: int = 200) -> list[TransferSchedule]:
    """Claim due schedules and advance them *before* dispatching.

    Advancing first is what prevents a double-fire if the worker dies between
    claiming and executing. skip_locked keeps N workers from contending.
    """
    now = timezone.now()
    with transaction.atomic():
        due = list(
            TransferSchedule.objects.select_for_update(skip_locked=True)
            .filter(status=TransferSchedule.Status.ACTIVE, next_run_at__lte=now)
            .order_by("next_run_at")[:limit]
        )
        for schedule in due:
            upcoming = next_run(schedule)
            schedule.next_run_at = upcoming or (now + timedelta(days=36500))
            schedule.last_run_at = now
            schedule.save(update_fields=["next_run_at", "last_run_at"])
    return due


def execute_scheduled(schedule_id, *, run_at) -> Transaction | None:
    schedule = TransferSchedule.objects.filter(pk=schedule_id).first()
    if schedule is None or schedule.status != TransferSchedule.Status.ACTIVE:
        return None

    # Deterministic key: a re-run of this task for this occurrence is free.
    key = f"sched:{schedule.id}:{run_at.isoformat()}"
    existing = Transaction.objects.filter(
        user_id=schedule.user_id, idempotency_key=key
    ).first()
    if existing:
        return existing

    txn = create_transfer(
        user_id=schedule.user_id, account_id=schedule.account_id,
        beneficiary_id=schedule.beneficiary_id, amount=schedule.amount,
        currency=schedule.currency, rail=schedule.rail,
        idempotency_key=key, remarks=schedule.remarks, schedule_id=schedule.id,
    )
    txn = submit(txn)

    with transaction.atomic():
        schedule.refresh_from_db()
        if txn.status in (TxnStatus.BLOCKED, TxnStatus.REJECTED, TxnStatus.FAILED):
            schedule.consecutive_failures += 1
            if schedule.consecutive_failures >= 3:
                # Stop retrying a doomed standing order rather than failing
                # every month forever.
                schedule.status = TransferSchedule.Status.FAILED
                publish(
                    EventEnvelope(
                        event_type="schedule.failed",
                        aggregate_type="transfer_schedule",
                        aggregate_id=str(schedule.id),
                        sequence=schedule.runs_completed,
                        producer="payments",
                        correlation_id=txn.correlation_id,
                        payload={
                            "schedule_id": str(schedule.id),
                            "user_id": str(schedule.user_id),
                            "consecutive_failures": schedule.consecutive_failures,
                            "last_reason": txn.status_reason,
                        },
                    )
                )
        else:
            schedule.consecutive_failures = 0
            schedule.runs_completed += 1

        if schedule.frequency == TransferSchedule.Frequency.ONCE:
            schedule.status = TransferSchedule.Status.COMPLETED
        elif schedule.max_runs and schedule.runs_completed >= schedule.max_runs:
            schedule.status = TransferSchedule.Status.COMPLETED
        elif schedule.end_date and schedule.next_run_at.date() > schedule.end_date:
            schedule.status = TransferSchedule.Status.COMPLETED

        schedule.save()

    return txn
