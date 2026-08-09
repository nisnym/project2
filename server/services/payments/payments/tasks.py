"""Django Q2 tasks for payments-svc.

Thin, and idempotent -- assume every task runs twice.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from platform_common.observability.context import correlation_scope

from . import services
from .models import Transaction, TransferSchedule, TxnStatus

logger = logging.getLogger(__name__)


def dispatch_to_rail(txn_id: str) -> str:
    """Hand a posted transfer to the (simulated) rail."""
    from .rails import get_adapter

    txn = Transaction.objects.filter(pk=txn_id).first()
    if txn is None:
        return "missing"
    if txn.status not in (TxnStatus.POSTED, TxnStatus.DISPATCHED):
        return f"skipped:{txn.status}"   # idempotent re-run

    with correlation_scope(txn.correlation_id or None):
        adapter = get_adapter(txn.rail)
        ack = adapter.submit(txn)
        if not txn.rail_ref:
            txn.rail_ref = ack.reference
            txn.save(update_fields=["rail_ref"])
    return "dispatched"


def run_due_schedules() -> dict:
    """Q2 schedule, every minute. Scheduled and recurring transfers."""
    from django_q.tasks import async_task

    now = timezone.now()
    due = services.claim_due_schedules()
    for schedule in due:
        async_task(
            "payments.tasks.execute_scheduled_transfer",
            str(schedule.id), now.isoformat(), q_options={"save": False},
        )
    return {"claimed": len(due)}


def execute_scheduled_transfer(schedule_id: str, run_at_iso: str) -> str:
    from django.utils.dateparse import parse_datetime

    with correlation_scope():
        txn = services.execute_scheduled(schedule_id, run_at=parse_datetime(run_at_iso))
    return txn.status if txn else "skipped"


def retry_compensation(txn_id: str | None = None) -> dict:
    """Drain COMPENSATION_PENDING sagas.

    Money must never stay reserved because a release call happened to fail.
    """
    from .saga import compensate

    query = Transaction.objects.filter(status=TxnStatus.COMPENSATION_PENDING)
    if txn_id:
        query = query.filter(pk=txn_id)

    recovered = 0
    for txn in query[:100]:
        with correlation_scope(txn.correlation_id or None):
            try:
                if not compensate(txn, upto="DISPATCH", reason=txn.status_reason or "retry"):
                    continue  # still failing; stays parked for the next sweep
            except Exception:
                logger.exception("compensation retry failed for %s", txn.reference)
                continue

            # Compensation is now complete, so the transaction can reach its
            # real terminal state instead of sitting in COMPENSATION_PENDING.
            from django.db import transaction as db_transaction

            from .saga import _emit, set_status

            with db_transaction.atomic():
                set_status(txn, TxnStatus.FAILED,
                           txn.status_reason or "compensated after retry")
                _emit(txn, "payment.failed", {"compensation_status": "COMPLETED"})
            recovered += 1
    return {"recovered": recovered}


def expire_stale_reviews(hours: int = 24) -> dict:
    """A case nobody works must not hold a customer's money forever."""
    cutoff = timezone.now() - timedelta(hours=hours)
    stale = Transaction.objects.filter(
        status=TxnStatus.UNDER_REVIEW, updated_at__lt=cutoff
    )[:100]

    expired = 0
    for txn in stale:
        with correlation_scope(txn.correlation_id or None):
            services.resume_after_rejection(txn.id, analyst_id="system:sla-expiry")
            expired += 1
    if expired:
        logger.warning("expired %s review(s) past SLA", expired)
    return {"expired": expired}


def sweep_stuck_sagas(minutes: int = 5) -> dict:
    """Anything mid-saga for too long is a crashed worker. Compensate it."""
    cutoff = timezone.now() - timedelta(minutes=minutes)
    stuck = Transaction.objects.filter(
        status__in=[TxnStatus.INITIATED, TxnStatus.VALIDATED,
                    TxnStatus.RESERVED, TxnStatus.SCREENING],
        updated_at__lt=cutoff,
    )[:100]

    swept = 0
    for txn in stuck:
        logger.warning("sweeping stuck saga %s (%s)", txn.reference, txn.status)
        with correlation_scope(txn.correlation_id or None):
            services.cancel(txn.id, user_id=txn.user_id, reason="SAGA_TIMEOUT")
            swept += 1
    return {"swept": swept}


def payments_metrics() -> dict:
    """Extra numbers for /internal/metrics -- the ops queue dashboard."""
    day_ago = timezone.now() - timedelta(days=1)
    return {
        "txn_under_review": Transaction.objects.filter(status=TxnStatus.UNDER_REVIEW).count(),
        "txn_compensation_pending": Transaction.objects.filter(
            status=TxnStatus.COMPENSATION_PENDING).count(),
        "txn_dispatched_awaiting_settlement": Transaction.objects.filter(
            status=TxnStatus.DISPATCHED).count(),
        "txn_failed_24h": Transaction.objects.filter(
            status__in=[TxnStatus.FAILED, TxnStatus.REVERSED], created_at__gte=day_ago).count(),
        "schedules_active": TransferSchedule.objects.filter(
            status=TransferSchedule.Status.ACTIVE).count(),
        "schedules_failed": TransferSchedule.objects.filter(
            status=TransferSchedule.Status.FAILED).count(),
    }
