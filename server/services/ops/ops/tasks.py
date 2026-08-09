"""Django Q2 tasks for ops-svc."""

from __future__ import annotations

from platform_common.observability.context import correlation_scope

from . import services


def poll_health() -> dict:
    """Q2 schedule, every 15 seconds."""
    with correlation_scope():
        return services.poll_health()


def generate_report(report_id: str) -> str:
    with correlation_scope():
        return services.generate_report(report_id).status


def prune_snapshots(days: int = 7) -> dict:
    from datetime import timedelta

    from django.utils import timezone

    from .models import HealthSnapshot

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = HealthSnapshot.objects.filter(captured_at__lt=cutoff).delete()
    return {"deleted": deleted}
