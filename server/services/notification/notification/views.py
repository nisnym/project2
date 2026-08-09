"""HTTP layer for notification-svc."""

from __future__ import annotations

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import IsAuthenticatedPrincipal
from platform_common.auth.authentication import JWTAuthentication

from .models import Notification


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticatedPrincipal])
def list_notifications(request):
    """Polled by the SPA. `since` is a cursor, not a page number, so newly
    arrived messages cannot shift a page boundary."""
    query = Notification.objects.filter(
        user_id=request.user.id, channel="IN_APP"
    )
    since = request.query_params.get("since")
    if since:
        query = query.filter(created_at__gt=since)
    if request.query_params.get("unread") == "true":
        query = query.filter(read_at__isnull=True)

    results = list(query[:50])
    return Response({
        "results": [
            {
                "id": str(n.id), "subject": n.subject, "body": n.body,
                "template_code": n.template_code,
                "read": n.read_at is not None,
                "created_at": n.created_at.isoformat(),
            }
            for n in results
        ],
        "next_cursor": results[0].created_at.isoformat() if results else since,
        "unread_count": Notification.objects.filter(
            user_id=request.user.id, channel="IN_APP", read_at__isnull=True
        ).count(),
    })


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticatedPrincipal])
def mark_read(request, notification_id):
    from django.utils import timezone

    updated = Notification.objects.filter(
        pk=notification_id, user_id=request.user.id, read_at__isnull=True
    ).update(read_at=timezone.now())
    return Response({"updated": updated})
