"""Audit domain logic: append to the chain, and verify it."""

from __future__ import annotations

import logging
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from platform_common.events.envelope import EventEnvelope

from .models import GENESIS_HASH, AuditLog, ChainVerification

logger = logging.getLogger(__name__)


def _parse_occurred_at(value: str | None) -> datetime:
    if not value:
        return timezone.now()
    parsed = parse_datetime(value)
    if parsed is None:
        logger.warning("unparseable occurred_at %r; using now()", value)
        return timezone.now()
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.utc)
    return parsed


def append(envelope: EventEnvelope) -> AuditLog | None:
    """Append one event to the chain.

    ``select_for_update`` on the tail row serialises all appends. That is
    deliberate: a hash chain has exactly one valid order, so concurrent appends
    must queue. It caps audit throughput in the low thousands per second --
    roughly 100x our peak. If that ever binds, the answer is per-aggregate
    chains, not dropping the chain.

    Returns None if this event was already recorded (redelivery).
    """
    actor = envelope.actor or {}

    with transaction.atomic():
        tail = AuditLog.objects.select_for_update().order_by("-id").first()
        prev_hash = tail.row_hash if tail else GENESIS_HASH

        row = AuditLog(
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            event_version=envelope.event_version,
            occurred_at=_parse_occurred_at(envelope.occurred_at),
            producer=envelope.producer,
            actor_type=str(actor.get("type") or ""),
            actor_id=str(actor.get("id") or ""),
            aggregate_type=envelope.aggregate_type,
            aggregate_id=str(envelope.aggregate_id),
            sequence=envelope.sequence,
            correlation_id=envelope.correlation_id or "",
            causation_id=str(envelope.causation_id or ""),
            payload=envelope.payload,
            prev_hash=prev_hash,
        )
        row.row_hash = row.compute_hash()

        try:
            row.save()
        except IntegrityError:
            # Duplicate event_id. The inbox normally absorbs redelivery before
            # we get here; this is the backstop for a replay driven by ops.
            logger.info("audit event %s already recorded", envelope.event_id)
            return None

    return row


def verify_chain(from_id: int | None = None, to_id: int | None = None) -> ChainVerification:
    """Re-walk the chain and confirm every link.

    Detects any row that was altered or removed after the fact -- each row's
    hash covers its predecessor's, so a single edit invalidates everything
    downstream of it.
    """
    query = AuditLog.objects.order_by("id")
    if from_id is not None:
        query = query.filter(id__gte=from_id)
    if to_id is not None:
        query = query.filter(id__lte=to_id)

    first = query.first()
    verification = ChainVerification.objects.create(
        from_id=first.id if first else 0,
        to_id=to_id or (query.last().id if first else 0),
    )

    if not first:
        verification.ok = True
        verification.finished_at = timezone.now()
        verification.detail = "chain is empty"
        verification.save()
        return verification

    # The expected predecessor hash for the first row we examine: genesis if we
    # start at the head, otherwise the actual hash of the row before it.
    predecessor = AuditLog.objects.filter(id__lt=first.id).order_by("-id").first()
    expected_prev = predecessor.row_hash if predecessor else GENESIS_HASH

    checked = 0
    broken_at = None
    detail = ""

    for row in query.iterator(chunk_size=1000):
        if row.prev_hash != expected_prev:
            broken_at = row.id
            detail = (
                f"row {row.id} prev_hash {row.prev_hash[:12]}... "
                f"does not match predecessor {expected_prev[:12]}... "
                "(a row was altered or removed)"
            )
            break
        recomputed = row.compute_hash()
        if recomputed != row.row_hash:
            broken_at = row.id
            detail = (
                f"row {row.id} content hash {recomputed[:12]}... "
                f"does not match stored {row.row_hash[:12]}... (row was edited)"
            )
            break
        expected_prev = row.row_hash
        checked += 1

    verification.rows_checked = checked
    verification.ok = broken_at is None
    verification.broken_at_id = broken_at
    verification.detail = detail or f"verified {checked} rows"
    verification.finished_at = timezone.now()
    verification.save()

    if broken_at is not None:
        logger.critical("AUDIT CHAIN BROKEN at row %s: %s", broken_at, detail)

    return verification
