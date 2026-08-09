"""Health and metrics endpoints, identical in every service.

``/internal/metrics`` is what ops-svc scrapes to answer the "monitor transfer
queues" use case. Each service reports its own Q2 depth straight out of the ORM
broker's table -- no external metrics system involved.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

__all__ = ["healthz", "readyz", "metrics"]


def healthz(request):
    """Liveness: the process is up. Must not touch the database -- a DB blip
    should not cause an orchestrator to kill otherwise-healthy pods."""
    return JsonResponse({"status": "ok", "service": settings.SERVICE_NAME})


def readyz(request):
    """Readiness: this instance can actually serve traffic."""
    checks, ok = {}, True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        ok = False

    try:
        from django_q.models import OrmQ

        OrmQ.objects.exists()
        checks["queue"] = "ok"
    except Exception as exc:
        checks["queue"] = f"error: {exc}"
        ok = False

    return JsonResponse(
        {"status": "ready" if ok else "not-ready", "service": settings.SERVICE_NAME,
         "checks": checks},
        status=200 if ok else 503,
    )


def metrics(request):
    """Scraped by ops-svc every 15 seconds."""
    from django_q.models import Failure, OrmQ, Schedule

    from ..models import InboxEvent, InboxStatus, OutboxEvent, OutboxStatus

    now = timezone.now()
    day_ago = now - timedelta(days=1)

    oldest = (
        OutboxEvent.objects.filter(status=OutboxStatus.PENDING)
        .order_by("created_at")
        .values_list("created_at", flat=True)
        .first()
    )

    payload = {
        "service": settings.SERVICE_NAME,
        "captured_at": now.isoformat(),
        "queue_depth": OrmQ.objects.count(),
        "failed_tasks_24h": Failure.objects.filter(started__gte=day_ago).count(),
        "scheduled_tasks": Schedule.objects.count(),
        "outbox_pending": OutboxEvent.objects.filter(
            status=OutboxStatus.PENDING
        ).count(),
        "outbox_dead": OutboxEvent.objects.filter(status=OutboxStatus.DEAD).count(),
        "outbox_sent_24h": OutboxEvent.objects.filter(
            status=OutboxStatus.SENT, sent_at__gte=day_ago
        ).count(),
        "inbox_pending": InboxEvent.objects.filter(
            status=InboxStatus.RECEIVED
        ).count(),
        "inbox_failed": InboxEvent.objects.filter(status=InboxStatus.FAILED).count(),
        "oldest_pending_age_s": int((now - oldest).total_seconds()) if oldest else 0,
    }

    # Services can contribute their own numbers without editing this module.
    for provider_path in getattr(settings, "EXTRA_METRIC_PROVIDERS", []):
        try:
            module_path, _, attr = provider_path.rpartition(".")
            import importlib

            provider = getattr(importlib.import_module(module_path), attr)
            payload.update(provider())
        except Exception:
            logger.exception("metric provider %s failed", provider_path)

    return JsonResponse(payload)
