"""Django Q2 tasks for notification-svc."""

from __future__ import annotations

from .models import Notification
from .services import deliver


def retry_failed_deliveries(limit: int = 100) -> dict:
    queued = Notification.objects.filter(status=Notification.Status.QUEUED)[:limit]
    sent = sum(1 for n in queued if deliver(n))
    return {"attempted": len(queued), "sent": sent}


def notification_metrics() -> dict:
    return {
        "notifications_queued": Notification.objects.filter(
            status=Notification.Status.QUEUED).count(),
        "notifications_failed": Notification.objects.filter(
            status=Notification.Status.FAILED).count(),
    }
