"""HTTP layer: authorise, parse, delegate, respond.

No business logic -- if there's an `if` about the domain, it belongs in
services.py.

Everything here is read-only by construction. The audit log is append-only and
hash-chained; an endpoint that could edit a row would destroy the only property
that makes the log worth keeping.
"""

from __future__ import annotations

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import IsStaff
from platform_common.auth.authentication import JWTAuthentication
from platform_common.errors import NotFound

from . import services
from .models import AuditLog, ChainVerification


def _log_json(row: AuditLog, *, payload: bool = True) -> dict:
    entry = {
        "id": row.id,
        "event_id": str(row.event_id),
        "event_type": row.event_type,
        "event_version": row.event_version,
        "producer": row.producer,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "aggregate_type": row.aggregate_type,
        "aggregate_id": row.aggregate_id,
        "sequence": row.sequence,
        "correlation_id": row.correlation_id,
        "causation_id": row.causation_id,
        "occurred_at": row.occurred_at.isoformat(),
        "recorded_at": row.recorded_at.isoformat(),
        "row_hash": row.row_hash,
        "prev_hash": row.prev_hash,
    }
    if payload:
        entry["payload"] = row.payload
    return entry


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def list_logs(request):
    """Query the trail.

    ``correlation_id`` is the important filter: it reassembles one customer
    action from the fragments each service recorded, which is exactly what an
    investigator needs and what a per-service log cannot give them.
    """
    query = AuditLog.objects.all()

    for field in ("correlation_id", "event_type", "aggregate_type",
                  "aggregate_id", "actor_id", "producer"):
        if value := request.query_params.get(field):
            query = query.filter(**{field: value})

    if since := request.query_params.get("since"):
        query = query.filter(occurred_at__gte=since)
    if until := request.query_params.get("until"):
        query = query.filter(occurred_at__lte=until)
    if before_id := request.query_params.get("before_id"):
        query = query.filter(id__lt=before_id)

    limit = min(int(request.query_params.get("limit", 50)), 200)
    rows = list(query.order_by("-id")[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]

    return Response({
        "results": [_log_json(row) for row in rows],
        "next_before_id": rows[-1].id if has_more and rows else None,
        "has_more": has_more,
    })


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def log_detail(request, log_id):
    row = AuditLog.objects.filter(pk=log_id).first()
    if row is None:
        raise NotFound("Audit entry not found.")
    return Response(_log_json(row))


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def trace(request, correlation_id):
    """One customer action, every service that touched it, in order.

    Ordered by ``occurred_at`` rather than by insertion: services record
    independently, so arrival order at the audit log is not causal order.
    """
    rows = AuditLog.objects.filter(correlation_id=correlation_id).order_by(
        "occurred_at", "id"
    )[:500]
    entries = list(rows)
    services_touched = sorted({row.producer for row in entries})
    span_ms = None
    if len(entries) > 1:
        span_ms = int(
            (entries[-1].occurred_at - entries[0].occurred_at).total_seconds() * 1000
        )

    return Response({
        "correlation_id": correlation_id,
        "count": len(entries),
        "services": services_touched,
        "span_ms": span_ms,
        "results": [_log_json(row) for row in entries],
    })


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def chain_status(request):
    """Has anyone tampered with the log?

    Reports the most recent stored verification, and can run a fresh one on
    demand. Verification is a full re-hash, so it is opt-in via ``?verify=true``
    rather than something a dashboard poll triggers by accident.
    """
    if request.query_params.get("verify") == "true":
        result = services.verify_chain()
    else:
        result = ChainVerification.objects.order_by("-started_at").first()

    total = AuditLog.objects.count()
    if result is None:
        return Response({
            "verified": False,
            "reason": "No verification has been run yet.",
            "total_entries": total,
        })

    return Response({
        "verified": result.ok,
        "rows_checked": result.rows_checked,
        "from_id": result.from_id,
        "to_id": result.to_id,
        "broken_at_id": result.broken_at_id,
        "detail": result.detail,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat() if result.finished_at else None,
        "total_entries": total,
    })


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def stats(request):
    """Volume by event type and producer, for the admin overview."""
    from django.db.models import Count

    return Response({
        "total": AuditLog.objects.count(),
        "by_producer": list(
            AuditLog.objects.values("producer")
            .annotate(count=Count("id")).order_by("-count")
        ),
        "by_event_type": list(
            AuditLog.objects.values("event_type")
            .annotate(count=Count("id")).order_by("-count")[:25]
        ),
    })
