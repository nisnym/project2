"""ops-svc domain logic: health polling, failure cases, reports."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from platform_common.http import get_client

from .models import FailureCase, HealthSnapshot, Report

logger = logging.getLogger(__name__)

MONITORED = [
    "identity", "onboarding", "kyc", "account", "payments",
    "ledger", "fraud", "notification", "audit",
]


def poll_health() -> dict:
    """Scrape every service's /internal/metrics. Answers the "monitor transfer
    queues" use case."""
    captured = []
    for service in MONITORED:
        try:
            metrics = get_client(service, timeout=2.0, retries=0).get("/internal/metrics")
            known = {
                "queue_depth", "failed_tasks_24h", "outbox_pending", "outbox_dead",
                "inbox_failed", "oldest_pending_age_s",
            }
            snapshot = HealthSnapshot(
                service=service, reachable=True,
                **{k: metrics.get(k) for k in known},
                extra={k: v for k, v in metrics.items()
                       if k not in known and k not in ("service", "captured_at")},
            )
        except Exception as exc:
            snapshot = HealthSnapshot(
                service=service, reachable=False, error=str(exc)[:200]
            )
        snapshot.save()
        captured.append(snapshot)

    unreachable = [s.service for s in captured if not s.reachable]
    if unreachable:
        logger.warning("services unreachable: %s", ", ".join(unreachable))
    return {"polled": len(captured), "unreachable": unreachable}


def latest_health() -> list[HealthSnapshot]:
    """Most recent snapshot per service."""
    latest = []
    for service in MONITORED:
        snapshot = HealthSnapshot.objects.filter(service=service).first()
        if snapshot:
            latest.append(snapshot)
    return latest


def open_case(*, failure_type: str, source_service: str, subject_ref: str,
              correlation_id: str = "", detail: dict | None = None) -> FailureCase | None:
    """Idempotent: one case per (failure_type, subject_ref)."""
    try:
        with transaction.atomic():
            return FailureCase.objects.create(
                failure_type=failure_type, source_service=source_service,
                subject_ref=str(subject_ref), correlation_id=correlation_id,
                detail=detail or {},
            )
    except IntegrityError:
        logger.debug("failure case for %s/%s already open", failure_type, subject_ref)
        return None


def resolve_case(case_id, *, note: str, status: str = FailureCase.Status.RESOLVED):
    updated = FailureCase.objects.filter(pk=case_id).update(
        status=status, resolution_note=note, resolved_at=timezone.now()
    )
    return bool(updated)


def generate_report(report_id) -> Report:
    """Reports are async jobs producing an artefact, never a synchronous query
    on the transactional path."""
    report = Report.objects.get(pk=report_id)
    report.status = Report.Status.RUNNING
    report.save(update_fields=["status"])

    try:
        result = _build(report.report_type, report.params)
        report.result = result
        report.rows = result.get("rows", 0)
        report.status = Report.Status.READY
    except Exception as exc:
        logger.exception("report %s failed", report_id)
        report.result = {"error": str(exc)}
        report.status = Report.Status.FAILED

    report.completed_at = timezone.now()
    report.save(update_fields=["result", "rows", "status", "completed_at"])
    return report


def _build(report_type: str, params: dict) -> dict:
    if report_type == "SERVICE_HEALTH":
        snapshots = latest_health()
        return {
            "rows": len(snapshots),
            "services": [
                {"service": s.service, "status": s.status, "reason": s.reason,
                 "queue_depth": s.queue_depth, "captured_at": s.captured_at.isoformat()}
                for s in snapshots
            ],
        }

    if report_type == "FAILURE_SUMMARY":
        since = timezone.now() - timedelta(days=int(params.get("days", 7)))
        cases = FailureCase.objects.filter(opened_at__gte=since)
        by_type: dict[str, int] = {}
        for case in cases:
            by_type[case.failure_type] = by_type.get(case.failure_type, 0) + 1
        return {
            "rows": cases.count(),
            "by_type": by_type,
            "open": cases.filter(status=FailureCase.Status.OPEN).count(),
        }

    raise ValueError(f"unknown report type {report_type!r}")
