"""ops-svc opens a case whenever something needs a human."""

from __future__ import annotations

from platform_common.events import subscribe
from platform_common.events.envelope import EventEnvelope

from . import services

# event_type -> (failure_type, which payload field identifies the subject)
CASE_SOURCES = {
    "payment.failed": ("PAYMENT_FAILED", "transaction_id"),
    "payment.returned": ("PAYMENT_RETURNED", "transaction_id"),
    "payment.blocked": ("PAYMENT_BLOCKED", "transaction_id"),
    "schedule.failed": ("SCHEDULE_FAILED", "schedule_id"),
    "ledger.invariant_breached": ("LEDGER_INVARIANT_BREACHED", "currency"),
    "audit.chain_broken": ("AUDIT_CHAIN_BROKEN", "verification_id"),
    "outbox.dead": ("OUTBOX_DEAD", "outbox_id"),
    "security.refresh_reuse_detected": ("TOKEN_REUSE", "user_id"),
}


@subscribe(*CASE_SOURCES)
def on_failure_event(env: EventEnvelope) -> None:
    failure_type, key = CASE_SOURCES[env.event_type]
    subject = env.payload.get(key) or env.aggregate_id
    services.open_case(
        failure_type=failure_type,
        source_service=env.producer,
        subject_ref=subject,
        correlation_id=env.correlation_id,
        detail=env.payload,
    )
