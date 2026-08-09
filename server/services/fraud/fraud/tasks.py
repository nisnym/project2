"""Django Q2 tasks for fraud-svc.

Thin, and idempotent -- assume every task runs twice.
"""

from __future__ import annotations

import logging

from platform_common.observability.context import correlation_scope

from . import services

logger = logging.getLogger(__name__)


def update_profile(payload: dict) -> None:
    """Maintain the feature read model after a screening. Off the hot path."""
    with correlation_scope():
        services.update_profile(payload)


def refresh_rule_cache() -> dict:
    """Pull rule/threshold/list changes into process memory (Q2 schedule, 60s)."""
    return services.refresh_caches()


def escalate_sla_breaches() -> dict:
    with correlation_scope():
        return services.escalate_sla_breaches()


def prune_screened_txns(days: int = 90) -> dict:
    """90-day retention on the velocity table; older rows answer no rule."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import ScreenedTxn

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = ScreenedTxn.objects.filter(created_at__lt=cutoff).delete()
    return {"deleted": deleted}


def fraud_metrics() -> dict:
    """Extra numbers for /internal/metrics."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import CaseStatus, FraudCase, FraudDecision

    day_ago = timezone.now() - timedelta(days=1)
    recent = FraudDecision.objects.filter(created_at__gte=day_ago)
    latencies = sorted(recent.values_list("latency_ms", flat=True))
    p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0

    return {
        "fraud_decisions_24h": len(latencies),
        "fraud_latency_p99_ms": p99,
        "fraud_cases_open": FraudCase.objects.filter(
            status__in=[CaseStatus.OPEN, CaseStatus.IN_REVIEW]
        ).count(),
        "fraud_cases_overdue": FraudCase.objects.filter(
            status__in=[CaseStatus.OPEN, CaseStatus.IN_REVIEW],
            sla_due_at__lt=timezone.now(),
        ).count(),
    }
