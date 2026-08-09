"""HTTP layer for ops-svc."""

from __future__ import annotations

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import IsAdmin, IsOps
from platform_common.auth.authentication import JWTAuthentication

from . import services
from .models import FailureCase, Report


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsOps])
def queues(request):
    """The "monitor transfer queues" use case.

    Served from stored snapshots, so it still answers while a service is down.
    """
    snapshots = services.latest_health()
    return Response({
        "captured_at": snapshots[0].captured_at.isoformat() if snapshots else None,
        "services": [
            {
                "service": s.service, "status": s.status, "reason": s.reason,
                "reachable": s.reachable, "queue_depth": s.queue_depth,
                "failed_tasks_24h": s.failed_tasks_24h,
                "outbox_pending": s.outbox_pending, "outbox_dead": s.outbox_dead,
                "oldest_pending_age_s": s.oldest_pending_age_s,
                "extra": s.extra,
                "last_seen": s.captured_at.isoformat(),
            }
            for s in snapshots
        ],
        "unhealthy": [s.service for s in snapshots if s.status != "HEALTHY"],
    })


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsOps])
def failures(request):
    """The "investigate failed transfers" use case."""
    status_filter = request.query_params.get("status", FailureCase.Status.OPEN)
    cases = FailureCase.objects.filter(status=status_filter)[:100]
    return Response({
        "results": [
            {
                "id": str(c.id), "failure_type": c.failure_type,
                "source_service": c.source_service, "subject_ref": c.subject_ref,
                "correlation_id": c.correlation_id, "status": c.status,
                "detail": c.detail, "opened_at": c.opened_at.isoformat(),
                "audit_trail_url": f"/api/audit/logs?correlation_id={c.correlation_id}",
            }
            for c in cases
        ]
    })


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsOps])
def resolve_failure(request, case_id):
    ok = services.resolve_case(case_id, note=request.data.get("note", ""))
    return Response({"resolved": ok})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAdmin])
def create_report(request):
    from django_q.tasks import async_task

    report = Report.objects.create(
        report_type=request.data.get("report_type", "SERVICE_HEALTH"),
        params=request.data.get("params", {}),
        requested_by=str(request.user.id),
    )
    async_task("ops.tasks.generate_report", str(report.id), q_options={"save": False})
    return Response({"id": str(report.id), "status": report.status,
                     "poll_url": f"/api/ops/reports/{report.id}"}, status=202)


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAdmin])
def get_report(request, report_id):
    report = Report.objects.filter(pk=report_id).first()
    if report is None:
        return Response({"error": {"code": "NOT_FOUND"}}, status=404)
    return Response({"id": str(report.id), "status": report.status,
                     "rows": report.rows, "result": report.result})
