"""Append-only, hash-chained audit log.

Two properties matter and both are enforced rather than promised:

1. **Append-only.** ``audit_role`` is granted INSERT and SELECT only; UPDATE and
   DELETE are revoked at the database. A regulator can verify that with ``\\dp``
   in one command. See scripts/init_databases.py --with-roles.

2. **Tamper-evident.** Each row's hash covers the previous row's hash, so
   altering or removing any historic row breaks every hash after it. A daily job
   re-walks the chain and raises a critical alert on a break.
"""

from __future__ import annotations

import hashlib

from django.db import models

from platform_common.events.envelope import canonical_json

GENESIS_HASH = "0" * 64


class AuditLog(models.Model):
    # BigAutoField, not UUID: the chain has exactly one valid order and the
    # sequence is that order.
    id = models.BigAutoField(primary_key=True)

    event_id = models.UUIDField(unique=True)
    event_type = models.CharField(max_length=100, db_index=True)
    event_version = models.PositiveIntegerField(default=1)

    occurred_at = models.DateTimeField(db_index=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    producer = models.CharField(max_length=50, db_index=True)
    actor_type = models.CharField(max_length=30, blank=True)
    actor_id = models.CharField(max_length=64, blank=True, db_index=True)

    aggregate_type = models.CharField(max_length=50, db_index=True)
    aggregate_id = models.CharField(max_length=64, db_index=True)
    sequence = models.BigIntegerField(default=0)

    correlation_id = models.CharField(max_length=64, db_index=True, blank=True)
    causation_id = models.CharField(max_length=64, blank=True)

    payload = models.JSONField()

    prev_hash = models.CharField(max_length=64)
    row_hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["id"]
        # No UPDATE/DELETE permissions are ever generated for this model, and
        # the DB role has them revoked besides.
        default_permissions = ("add", "view")
        indexes = [
            models.Index(fields=["aggregate_type", "aggregate_id"], name="audit_aggregate_idx"),
            models.Index(fields=["-occurred_at"], name="audit_occurred_idx"),
        ]

    # -- hashing --------------------------------------------------------

    def hashable_content(self) -> dict:
        """Exactly the fields the hash covers.

        Deliberately excludes ``recorded_at`` (set by the database clock) and
        ``id`` (assigned on insert) so the digest can be computed before saving
        and recomputed identically afterwards.
        """
        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type,
            "event_version": self.event_version,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "producer": self.producer,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "sequence": self.sequence,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "payload": self.payload,
        }

    def compute_hash(self) -> str:
        material = self.prev_hash + canonical_json(self.hashable_content())
        return hashlib.sha256(material.encode()).hexdigest()

    def __str__(self) -> str:
        return f"#{self.id} {self.event_type} {self.aggregate_type}:{self.aggregate_id}"


class ChainVerification(models.Model):
    """Result of each chain walk, so 'we check it' is itself auditable."""

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    from_id = models.BigIntegerField()
    to_id = models.BigIntegerField()
    rows_checked = models.BigIntegerField(default=0)
    ok = models.BooleanField(default=False)
    broken_at_id = models.BigIntegerField(null=True, blank=True)
    detail = models.TextField(blank=True)

    class Meta:
        db_table = "audit_chain_verification"
        ordering = ["-started_at"]
