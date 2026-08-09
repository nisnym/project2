"""Outbox and inbox tables. Present in every service via ``platform_common`` in
INSTALLED_APPS.

The pair is what turns "write to the database" + "tell another service" -- two
operations that cannot be made atomic -- into something that loses nothing:

  * the OUTBOX row is written in the same transaction as the state change, so it
    exists if and only if the change committed;
  * the INBOX row has a unique ``event_id``, so a redelivery is a no-op.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class OutboxStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    DEAD = "DEAD", "Dead"


class InboxStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    PROCESSED = "PROCESSED", "Processed"
    FAILED = "FAILED", "Failed"
    SKIPPED = "SKIPPED", "Skipped"


class OutboxEvent(models.Model):
    """One row per (event, subscriber).

    Fanning out at publish time rather than delivery time means a subscriber
    being down stalls only its own row -- audit still receives an event that
    notification is failing to accept.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_id = models.UUIDField(db_index=True)
    event_type = models.CharField(max_length=100, db_index=True)
    aggregate_id = models.CharField(max_length=64, db_index=True)
    subscriber = models.CharField(max_length=50)
    envelope = models.JSONField()

    status = models.CharField(
        max_length=12, choices=OutboxStatus.choices, default=OutboxStatus.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "pc_outbox_event"
        constraints = [
            # The same event is delivered to a given subscriber exactly once.
            # Also makes the publisher itself idempotent under retry.
            models.UniqueConstraint(
                fields=["event_id", "subscriber"], name="pc_outbox_uniq_event_subscriber"
            )
        ]
        indexes = [
            # Drives the sweeper's hot query.
            models.Index(
                fields=["status", "next_attempt_at"], name="pc_outbox_due_idx"
            ),
            models.Index(fields=["created_at"], name="pc_outbox_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} -> {self.subscriber} [{self.status}]"


class InboxEvent(models.Model):
    """One row per received event. ``event_id`` as the primary key is the whole
    trick: a duplicate POST hits an IntegrityError and we return 200 having done
    nothing."""

    event_id = models.UUIDField(primary_key=True)
    event_type = models.CharField(max_length=100, db_index=True)
    aggregate_id = models.CharField(max_length=64, db_index=True)
    sequence = models.BigIntegerField(default=0)
    producer = models.CharField(max_length=50)
    correlation_id = models.CharField(max_length=64, db_index=True, blank=True)
    envelope = models.JSONField()

    status = models.CharField(
        max_length=12, choices=InboxStatus.choices, default=InboxStatus.RECEIVED
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "pc_inbox_event"
        indexes = [
            models.Index(fields=["status", "received_at"], name="pc_inbox_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} from {self.producer} [{self.status}]"


class ProjectionCursor(models.Model):
    """Last applied sequence per (projection, aggregate).

    Events can arrive out of order because workers run in parallel. Handlers that
    mutate a projection consult this and skip anything that would move the
    aggregate backwards.
    """

    projection = models.CharField(max_length=64)
    aggregate_id = models.CharField(max_length=64)
    last_sequence = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pc_projection_cursor"
        constraints = [
            models.UniqueConstraint(
                fields=["projection", "aggregate_id"], name="pc_cursor_uniq"
            )
        ]
