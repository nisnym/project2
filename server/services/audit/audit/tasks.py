"""Django Q2 tasks for audit-svc.

Thin, and idempotent -- assume every task runs twice, because a worker can die
after the side effect but before the ack.
"""

from __future__ import annotations

import logging

from django.db import transaction

from platform_common.events import EventEnvelope, publish
from platform_common.observability.context import correlation_scope

from .models import AuditLog
from .services import verify_chain

logger = logging.getLogger(__name__)


def verify_chain_task(from_id: int | None = None) -> dict:
    """Daily chain walk. Publishes a critical event if a link is broken."""
    with correlation_scope() as correlation_id:
        verification = verify_chain(from_id=from_id)

        if not verification.ok:
            with transaction.atomic():
                publish(
                    EventEnvelope(
                        event_type="audit.chain_broken",
                        aggregate_type="audit_chain",
                        aggregate_id=str(verification.id),
                        sequence=0,
                        producer="audit",
                        correlation_id=correlation_id,
                        payload={
                            "verification_id": verification.id,
                            "broken_at_id": verification.broken_at_id,
                            "detail": verification.detail,
                            "rows_checked": verification.rows_checked,
                        },
                    )
                )

        return {
            "ok": verification.ok,
            "rows_checked": verification.rows_checked,
            "broken_at_id": verification.broken_at_id,
        }


def chain_stats() -> dict:
    """Extra numbers for /internal/metrics."""
    return {
        "audit_rows": AuditLog.objects.count(),
        "audit_last_id": AuditLog.objects.order_by("-id")
        .values_list("id", flat=True)
        .first()
        or 0,
    }
