"""Customer notifications.

The unique constraint on (source_event_id, channel, user_id) is what makes
at-least-once event delivery safe for something as visible as an email: a
redelivered event cannot produce a second message.
"""

from __future__ import annotations

import uuid

from django.db import models


class Channel(models.TextChoices):
    IN_APP = "IN_APP", "In-app"
    EMAIL = "EMAIL", "Email"
    SMS = "SMS", "SMS"


class Notification(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"
        SUPPRESSED = "SUPPRESSED", "Suppressed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.UUIDField(db_index=True)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    template_code = models.CharField(max_length=50)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.QUEUED, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    source_event_id = models.UUIDField(db_index=True)
    correlation_id = models.CharField(max_length=64, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notification_notification"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_event_id", "channel", "user_id"],
                name="notification_uniq_per_event_channel",
            )
        ]


class Preference(models.Model):
    user_id = models.UUIDField(db_index=True)
    category = models.CharField(max_length=30)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "notification_preference"
        constraints = [
            models.UniqueConstraint(fields=["user_id", "category", "channel"],
                                    name="notification_uniq_pref")
        ]
