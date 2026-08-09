"""Operational read models.

Snapshots rather than live queries: the ops dashboard has to keep working
*while* a service is down, which is precisely when ops needs it. A dashboard
that fans out to ten live services goes dark in the same incident it exists to
diagnose.
"""

from __future__ import annotations

import uuid

from django.db import models


class HealthSnapshot(models.Model):
    service = models.CharField(max_length=30, db_index=True)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reachable = models.BooleanField(default=False)
    queue_depth = models.IntegerField(null=True, blank=True)
    failed_tasks_24h = models.IntegerField(null=True, blank=True)
    outbox_pending = models.IntegerField(null=True, blank=True)
    outbox_dead = models.IntegerField(null=True, blank=True)
    inbox_failed = models.IntegerField(null=True, blank=True)
    oldest_pending_age_s = models.IntegerField(null=True, blank=True)
    extra = models.JSONField(default=dict)
    error = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "ops_health_snapshot"
        ordering = ["-captured_at"]
        indexes = [models.Index(fields=["service", "-captured_at"],
                                name="ops_health_latest_idx")]

    # Thresholds that turn raw numbers into a status an operator can act on.
    DEPTH_WARN = 200
    AGE_WARN_SECONDS = 120

    @property
    def status(self) -> str:
        if not self.reachable:
            return "UNREACHABLE"
        if (self.outbox_dead or 0) > 0 or (self.inbox_failed or 0) > 0:
            return "DEGRADED"
        if (self.queue_depth or 0) > self.DEPTH_WARN:
            return "DEGRADED"
        if (self.oldest_pending_age_s or 0) > self.AGE_WARN_SECONDS:
            return "DEGRADED"
        return "HEALTHY"

    @property
    def reason(self) -> str:
        if not self.reachable:
            return self.error or "service did not respond"
        problems = []
        if (self.outbox_dead or 0) > 0:
            problems.append(f"{self.outbox_dead} dead event(s)")
        if (self.inbox_failed or 0) > 0:
            problems.append(f"{self.inbox_failed} failed inbound event(s)")
        if (self.queue_depth or 0) > self.DEPTH_WARN:
            problems.append(f"queue depth {self.queue_depth}")
        if (self.oldest_pending_age_s or 0) > self.AGE_WARN_SECONDS:
            problems.append(f"oldest pending {self.oldest_pending_age_s}s")
        return "; ".join(problems)


class FailureCase(models.Model):
    """Something that needs a human. Opened from events, worked by ops."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        INVESTIGATING = "INVESTIGATING", "Investigating"
        RETRIED = "RETRIED", "Retried"
        RESOLVED = "RESOLVED", "Resolved"
        WONT_FIX = "WONT_FIX", "Won't fix"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    failure_type = models.CharField(max_length=40, db_index=True)
    source_service = models.CharField(max_length=30)
    subject_ref = models.CharField(max_length=64, db_index=True)
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=15, choices=Status.choices,
                              default=Status.OPEN, db_index=True)
    detail = models.JSONField(default=dict)
    assigned_to = models.CharField(max_length=64, blank=True)
    resolution_note = models.TextField(blank=True)
    opened_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ops_failure_case"
        ordering = ["-opened_at"]
        constraints = [
            # One case per underlying problem, however many times the event is
            # redelivered.
            models.UniqueConstraint(fields=["failure_type", "subject_ref"],
                                    name="ops_uniq_failure_case")
        ]


class Report(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_type = models.CharField(max_length=40)
    params = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.QUEUED)
    result = models.JSONField(default=dict)
    rows = models.IntegerField(null=True, blank=True)
    requested_by = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ops_report"
        ordering = ["-created_at"]
