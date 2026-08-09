"""Django Q2 tasks for ledger-svc."""

from __future__ import annotations

from platform_common.observability.context import correlation_scope

from . import services


def expire_holds() -> dict:
    """Release holds whose saga died mid-flight.

    Without this a payments-svc crash between PLACE_HOLD and CAPTURE strands the
    customer's money indefinitely.
    """
    with correlation_scope():
        return services.expire_holds()


def verify_invariants() -> dict:
    """Debits==credits per currency, and balances agreeing with postings.

    A breach here is an existential event for a bank, so check_invariants()
    publishes a dedicated event that opens an ops case rather than only logging.
    """
    with correlation_scope():
        checks = services.check_invariants()
    return {c.check_name: c.ok for c in checks}


def ledger_metrics() -> dict:
    from .models import Hold, HoldStatus, JournalEntry

    return {
        "ledger_active_holds": Hold.objects.filter(status=HoldStatus.ACTIVE).count(),
        "ledger_journal_entries": JournalEntry.objects.count(),
    }
