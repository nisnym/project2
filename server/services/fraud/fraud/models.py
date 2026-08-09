"""Fraud engine: rules, decisions, cases, and its own feature read model.

Two things shape this schema:

**The 50 ms budget.** Screening is a synchronous gate on the money path, so it
cannot make a network call to fetch features. fraud-svc therefore keeps its own
denormalised read model (AccountProfile, ScreenedTxn, KnownBeneficiary) fed by
its own screenings and by events. Three indexed reads plus in-memory rules.

**Admin-configurable rules.** Rules are data, not code: a JSON condition tree
evaluated by a whitelisted interpreter. An administrator who can edit rules must
not thereby gain code execution, which is why there is no `eval` anywhere near
this.
"""

from __future__ import annotations

import math
import uuid
from decimal import Decimal

from django.db import models

from platform_common.db import MoneyField


class Decision(models.TextChoices):
    ALLOW = "ALLOW", "Allow"
    REVIEW = "REVIEW", "Review"
    BLOCK = "BLOCK", "Block"


class RuleMode(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    # Evaluated and recorded, but contributes nothing to the decision. This is
    # how a rule is measured against live traffic before it can hurt anyone.
    SHADOW = "SHADOW", "Shadow"
    DISABLED = "DISABLED", "Disabled"


class RuleCategory(models.TextChoices):
    AMOUNT = "AMOUNT", "Amount"
    VELOCITY = "VELOCITY", "Velocity"
    BENEFICIARY = "BENEFICIARY", "Beneficiary"
    GEO = "GEO", "Geography"
    DEVICE = "DEVICE", "Device"
    LIST = "LIST", "List match"
    BEHAVIOUR = "BEHAVIOUR", "Behaviour"


class CaseStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    IN_REVIEW = "IN_REVIEW", "In review"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class Resolution(models.TextChoices):
    CONFIRMED_FRAUD = "CONFIRMED_FRAUD", "Confirmed fraud"
    FALSE_POSITIVE = "FALSE_POSITIVE", "False positive"
    INCONCLUSIVE = "INCONCLUSIVE", "Inconclusive"


class Rule(models.Model):
    """An admin-configurable rule. ``condition`` is the JSON DSL (services.py)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=60, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    condition = models.JSONField()
    weight = models.IntegerField(default=0)
    # A hard rule blocks regardless of the total score: a blacklisted
    # beneficiary is not something a low score should be able to outvote.
    hard_block = models.BooleanField(default=False)

    mode = models.CharField(max_length=10, choices=RuleMode.choices, default=RuleMode.SHADOW)
    reason_code = models.CharField(max_length=20)
    category = models.CharField(max_length=20, choices=RuleCategory.choices)

    version = models.PositiveIntegerField(default=1)
    created_by = models.CharField(max_length=64, blank=True)
    updated_by = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fraud_rule"
        ordering = ["code"]
        indexes = [models.Index(fields=["mode"], name="fraud_rule_mode_idx")]

    def __str__(self) -> str:
        return f"{self.code} (+{self.weight}, {self.mode})"


class RuleStat(models.Model):
    """Per-rule outcome counters -- the false-positive reduction loop.

    Precision is what tells an admin whether a rule is earning its place. A rule
    that fires 1,284 times at 0.34 precision is generating three false alarms
    for every real catch.
    """

    rule = models.OneToOneField(Rule, on_delete=models.CASCADE, related_name="stat")
    fired_count = models.BigIntegerField(default=0)
    shadow_fired_count = models.BigIntegerField(default=0)
    confirmed_fraud = models.BigIntegerField(default=0)
    false_positive = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fraud_rule_stat"

    @property
    def precision(self) -> float | None:
        resolved = self.confirmed_fraud + self.false_positive
        return None if resolved == 0 else self.confirmed_fraud / resolved


class Threshold(models.Model):
    """Score cut-offs. Versioned so a decision can be explained after the fact."""

    allow_below = models.IntegerField(default=40)
    block_at_or_above = models.IntegerField(default=75)
    # Outage-only auto-allow for small amounts. 0 disables it, which is the
    # default: ADR-005 says fail to REVIEW, never to ALLOW. Raising this is an
    # explicit, recorded risk acceptance.
    safe_harbour_amount = MoneyField(default=Decimal("0"))
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    updated_by = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fraud_threshold"


class FraudDecision(models.Model):
    """One row per screening. Never optional -- every transaction gets a decision.

    ``features`` stores the exact feature vector. It costs one JSON column and
    buys risk-free rule tuning: a proposed rule can be replayed against every
    past screening to see what it would have done.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    txn_ref = models.UUIDField(db_index=True)
    account_ref = models.UUIDField(db_index=True)
    user_ref = models.UUIDField(null=True, blank=True)

    decision = models.CharField(max_length=10, choices=Decision.choices, db_index=True)
    score = models.IntegerField()
    reason_codes = models.JSONField(default=list)
    shadow_codes = models.JSONField(default=list)
    features = models.JSONField(default=dict)

    amount = MoneyField()
    currency = models.CharField(max_length=3)
    rail = models.CharField(max_length=20)

    ruleset_version = models.CharField(max_length=40, blank=True)
    # Proves the "milliseconds" claim on every single decision, rather than in
    # a benchmark nobody re-runs.
    latency_ms = models.IntegerField()
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "fraud_decision"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["txn_ref"], name="fraud_uniq_decision_per_txn")
        ]

    def __str__(self) -> str:
        return f"{self.decision} {self.score} ({self.latency_ms}ms)"


class FraudCase(models.Model):
    """Analyst work item, opened for every REVIEW and BLOCK."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.OneToOneField(FraudDecision, on_delete=models.PROTECT, related_name="case")
    txn_ref = models.UUIDField(db_index=True)
    account_ref = models.UUIDField(db_index=True)

    status = models.CharField(max_length=12, choices=CaseStatus.choices,
                              default=CaseStatus.OPEN, db_index=True)
    priority = models.CharField(max_length=10, default="MEDIUM", db_index=True)
    resolution = models.CharField(max_length=20, choices=Resolution.choices, blank=True)

    assigned_to = models.CharField(max_length=64, blank=True)
    sla_due_at = models.DateTimeField(db_index=True)
    resolution_note = models.TextField(blank=True)
    resolved_by = models.CharField(max_length=64, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "fraud_case"
        ordering = ["sla_due_at"]

    def __str__(self) -> str:
        return f"case {self.id} [{self.status}]"


# ---------------------------------------------------------------------------
# Local read model. Fed by screenings and by events; never queried across a
# service boundary, because a network hop does not fit in a 50 ms budget.
# ---------------------------------------------------------------------------


class AccountProfile(models.Model):
    """Rolling per-account statistics, maintained incrementally.

    Mean and variance use Welford's online algorithm rather than storing a sum
    of squares: it is numerically stable over millions of updates, where the
    naive form loses precision badly.
    """

    account_ref = models.UUIDField(primary_key=True)
    txn_count = models.BigIntegerField(default=0)
    mean_amount = MoneyField(default=Decimal("0"))
    # Welford's M2 accumulator: sum of squared deviations from the running mean.
    m2 = models.FloatField(default=0.0)
    max_amount = MoneyField(default=Decimal("0"))

    last_country = models.CharField(max_length=2, blank=True)
    last_device_hash = models.CharField(max_length=80, blank=True)
    last_txn_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    account_opened_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fraud_account_profile"

    @property
    def stddev(self) -> Decimal:
        if self.txn_count < 2:
            return Decimal("0")
        return Decimal(str(math.sqrt(self.m2 / (self.txn_count - 1))))


class ScreenedTxn(models.Model):
    """One row per screening: the velocity source.

    The (account_ref, -created_at) index is what makes the 5-minute velocity
    window a fast range scan rather than a table scan.
    """

    txn_ref = models.UUIDField(primary_key=True)
    account_ref = models.UUIDField()
    amount = MoneyField()
    currency = models.CharField(max_length=3)
    rail = models.CharField(max_length=20)
    benef_fingerprint = models.CharField(max_length=80, blank=True)
    country = models.CharField(max_length=2, blank=True)
    device_hash = models.CharField(max_length=80, blank=True)
    decision = models.CharField(max_length=10, blank=True)
    created_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "fraud_screened_txn"
        indexes = [
            models.Index(fields=["account_ref", "-created_at"], name="fraud_velocity_idx"),
        ]


class KnownBeneficiary(models.Model):
    """Which payees an account has paid before. Drives 'new beneficiary' rules."""

    account_ref = models.UUIDField(db_index=True)
    fingerprint = models.CharField(max_length=80)
    first_seen = models.DateTimeField()
    txn_count = models.IntegerField(default=0)
    total_amount = MoneyField(default=Decimal("0"))

    class Meta:
        db_table = "fraud_known_beneficiary"
        constraints = [
            models.UniqueConstraint(
                fields=["account_ref", "fingerprint"], name="fraud_uniq_known_benef"
            )
        ]


class ListEntry(models.Model):
    """Blacklists and high-risk lists. Small enough to hold entirely in memory."""

    class ListType(models.TextChoices):
        BLACKLIST_BENEFICIARY = "BLACKLIST_BENEFICIARY", "Blacklisted beneficiary"
        HIGH_RISK_COUNTRY = "HIGH_RISK_COUNTRY", "High-risk country"
        BLOCKED_DEVICE = "BLOCKED_DEVICE", "Blocked device"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    list_type = models.CharField(max_length=30, choices=ListType.choices, db_index=True)
    value = models.CharField(max_length=120, db_index=True)
    reason = models.CharField(max_length=255, blank=True)
    added_by = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fraud_list_entry"
        constraints = [
            models.UniqueConstraint(fields=["list_type", "value"], name="fraud_uniq_list_value")
        ]
