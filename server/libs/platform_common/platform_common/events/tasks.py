"""Django Q2 task entrypoints for the event backbone.

Two loops, both idempotent:

  relay_one / sweep_outbox    publisher side -- get the event to the subscriber
  dispatch_one / sweep_inbox  subscriber side -- run the handlers exactly once

Every function here must tolerate being run twice on the same row: Q2 redelivers
a task whose lock lapses, and the spike confirmed a failed task stays queued
until ``max_attempts``.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from ..models import InboxEvent, InboxStatus, OutboxEvent, OutboxStatus
from .envelope import EventEnvelope
from .transport import PermanentDeliveryError, TransientDeliveryError, deliver

logger = logging.getLogger(__name__)

# Roughly 1h22m of transient-failure tolerance before a human is involved:
# long enough to ride out a deploy or a restart, short enough that a real
# outage surfaces the same day. len() is the attempt cap.
BACKOFF_SECONDS = [1, 2, 5, 15, 60, 300, 900, 3600]
MAX_INBOX_ATTEMPTS = 8

__all__ = [
    "relay_one",
    "sweep_outbox",
    "dispatch_one",
    "sweep_inbox",
    "prune_processed",
]


# --------------------------------------------------------------------------
# publisher side
# --------------------------------------------------------------------------


def relay_one(outbox_id: str) -> str:
    """Deliver a single outbox row.

    The immediate (on_commit) task and the sweeper can both target the same row.
    ``select_for_update(skip_locked=True)`` means the loser simply does nothing
    rather than sending a duplicate.
    """
    with transaction.atomic():
        row = (
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(id=outbox_id, status=OutboxStatus.PENDING)
            .first()
        )
        if row is None:
            # Already sent, already dead, or locked by another worker. All normal.
            return "skipped"

        try:
            status_code = deliver(row.subscriber, row.envelope)
        except PermanentDeliveryError as exc:
            row.status = OutboxStatus.DEAD
            row.attempts += 1
            row.last_error = f"permanent: {exc}"
            row.save(update_fields=["status", "attempts", "last_error"])
            logger.error("outbox %s DEAD (permanent): %s", outbox_id, exc)
            return "dead"
        except TransientDeliveryError as exc:
            row.attempts += 1
            if row.attempts >= len(BACKOFF_SECONDS):
                row.status = OutboxStatus.DEAD
                row.last_error = f"exhausted after {row.attempts} attempts: {exc}"
                logger.error("outbox %s DEAD (exhausted): %s", outbox_id, exc)
            else:
                delay = BACKOFF_SECONDS[row.attempts]
                row.next_attempt_at = timezone.now() + timedelta(seconds=delay)
                row.last_error = str(exc)
                logger.warning(
                    "outbox %s attempt %s failed, retry in %ss: %s",
                    outbox_id, row.attempts, delay, exc,
                )
            row.save(
                update_fields=["status", "attempts", "next_attempt_at", "last_error"]
            )
            return "dead" if row.status == OutboxStatus.DEAD else "retry"

        row.status = OutboxStatus.SENT
        row.sent_at = timezone.now()
        row.attempts += 1
        row.last_error = ""
        row.save(update_fields=["status", "sent_at", "attempts", "last_error"])
        return "duplicate" if status_code == 200 else "sent"


def sweep_outbox(limit: int = 500) -> dict:
    """Safety net, every minute.

    Catches rows whose on_commit task was lost (process died between COMMIT and
    enqueue) and rows waiting out a backoff. Without this, a crash at exactly the
    wrong moment would strand an event forever.
    """
    due = list(
        OutboxEvent.objects.filter(
            status=OutboxStatus.PENDING, next_attempt_at__lte=timezone.now()
        )
        .order_by("created_at")
        .values_list("id", flat=True)[:limit]
    )
    results: dict[str, int] = {}
    for outbox_id in due:
        outcome = relay_one(str(outbox_id))
        results[outcome] = results.get(outcome, 0) + 1
    if due:
        logger.info("outbox sweep processed %s rows: %s", len(due), results)
    return {"processed": len(due), **results}


# --------------------------------------------------------------------------
# subscriber side
# --------------------------------------------------------------------------


def dispatch_one(event_id: str) -> str:
    """Run every handler registered for this event type, once.

    Transaction structure matters here and is easy to get wrong:

      outer atomic   holds the row lock and COMMITS the bookkeeping
        inner atomic (savepoint)  runs the handlers

    Handler side effects roll back with the inner savepoint, so there is never a
    window where the side effect happened but the row says it didn't. The
    attempt counter and error live in the *outer* transaction, so they survive
    the failure -- writing them inside the block that rolls back would reset the
    counter every time and a poison event would retry forever.

    The exception is re-raised only after the outer transaction commits, so Q2
    still records a Failure row for ops without discarding our bookkeeping.
    """
    from .dispatcher import handlers_for

    failure: Exception | None = None

    with transaction.atomic():
        row = (
            InboxEvent.objects.select_for_update(skip_locked=True)
            .filter(event_id=event_id)
            .first()
        )
        if row is None:
            logger.warning("inbox row %s vanished before dispatch", event_id)
            return "missing"
        if row.status == InboxStatus.PROCESSED:
            return "already-processed"
        if row.status == InboxStatus.FAILED:
            return "failed-permanently"

        envelope = EventEnvelope.from_dict(row.envelope)
        handlers = handlers_for(envelope.event_type)
        if not handlers:
            # Subscribed but nothing registered: usually a wiring bug, so make it
            # visible rather than silently dropping it.
            row.status = InboxStatus.SKIPPED
            row.processed_at = timezone.now()
            row.save(update_fields=["status", "processed_at"])
            logger.warning("no handler registered for %s", envelope.event_type)
            return "no-handler"

        try:
            with transaction.atomic():  # savepoint: isolates handler side effects
                for handler in handlers:
                    handler(envelope)
        except Exception as exc:
            failure = exc
            row.attempts += 1
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            row.status = (
                InboxStatus.FAILED
                if row.attempts >= MAX_INBOX_ATTEMPTS
                else InboxStatus.RECEIVED
            )
            row.save(update_fields=["attempts", "last_error", "status"])
            logger.exception(
                "handler failed for %s (attempt %s)", envelope.event_type, row.attempts
            )
        else:
            row.status = InboxStatus.PROCESSED
            row.processed_at = timezone.now()
            row.attempts += 1
            row.save(update_fields=["status", "processed_at", "attempts"])

    if failure is not None:
        # Raised after COMMIT so Q2 records a Failure row; ops reads those.
        raise failure
    return "processed"


def sweep_inbox(limit: int = 500) -> dict:
    """Re-dispatch rows accepted but never processed (worker died, or a handler
    failed and is due another attempt)."""
    stale_before = timezone.now() - timedelta(seconds=30)
    due = list(
        InboxEvent.objects.filter(
            status=InboxStatus.RECEIVED, received_at__lte=stale_before
        )
        .order_by("received_at")
        .values_list("event_id", flat=True)[:limit]
    )
    processed = 0
    for event_id in due:
        try:
            dispatch_one(str(event_id))
            processed += 1
        except Exception:
            # Already logged and recorded on the row; keep sweeping.
            continue
    if due:
        logger.info("inbox sweep re-dispatched %s/%s rows", processed, len(due))
    return {"found": len(due), "processed": processed}


def prune_processed(days: int = 14) -> dict:
    """Keep the tables bounded. Only touches terminal rows."""
    cutoff = timezone.now() - timedelta(days=days)
    sent, _ = OutboxEvent.objects.filter(
        status=OutboxStatus.SENT, sent_at__lt=cutoff
    ).delete()
    inbox, _ = InboxEvent.objects.filter(
        status__in=[InboxStatus.PROCESSED, InboxStatus.SKIPPED], processed_at__lt=cutoff
    ).delete()
    return {"outbox_deleted": sent, "inbox_deleted": inbox}
