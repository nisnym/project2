"""Onboarding applications and eligibility results.

The workflow is an explicit state machine persisted in a column -- never
inferred from the presence of related rows. Inferred state is how a workflow
ends up in two states at once.
"""

from __future__ import annotations

import uuid

from django.db import models


class ApplicationStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    KYC_PENDING = "KYC_PENDING", "KYC pending"
    KYC_PASSED = "KYC_PASSED", "KYC passed"
    KYC_FAILED = "KYC_FAILED", "KYC failed"
    ELIGIBLE = "ELIGIBLE", "Eligible"
    MANUAL_REVIEW = "MANUAL_REVIEW", "Manual review"
    REJECTED = "REJECTED", "Rejected"
    ACCOUNT_OPENED = "ACCOUNT_OPENED", "Account opened"
    FAILED = "FAILED", "Failed"


# Explicit transition table. Anything not listed is illegal, and attempting it
# raises rather than silently corrupting the workflow.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    ApplicationStatus.DRAFT: {ApplicationStatus.SUBMITTED},
    ApplicationStatus.SUBMITTED: {ApplicationStatus.KYC_PENDING, ApplicationStatus.REJECTED},
    ApplicationStatus.KYC_PENDING: {
        ApplicationStatus.KYC_PASSED, ApplicationStatus.KYC_FAILED,
        ApplicationStatus.MANUAL_REVIEW,
    },
    ApplicationStatus.KYC_PASSED: {
        ApplicationStatus.ELIGIBLE, ApplicationStatus.MANUAL_REVIEW,
        ApplicationStatus.REJECTED,
    },
    ApplicationStatus.MANUAL_REVIEW: {ApplicationStatus.ELIGIBLE, ApplicationStatus.REJECTED},
    ApplicationStatus.ELIGIBLE: {ApplicationStatus.ACCOUNT_OPENED, ApplicationStatus.FAILED},
    ApplicationStatus.FAILED: {ApplicationStatus.ELIGIBLE},
    ApplicationStatus.KYC_FAILED: set(),
    ApplicationStatus.REJECTED: set(),
    ApplicationStatus.ACCOUNT_OPENED: set(),
}


class Application(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.UUIDField(db_index=True)
    status = models.CharField(max_length=16, choices=ApplicationStatus.choices,
                              default=ApplicationStatus.DRAFT, db_index=True)
    status_reason = models.CharField(max_length=120, blank=True)
    customer_info = models.JSONField(default=dict)
    kyc_case_id = models.UUIDField(null=True, blank=True)
    account_id = models.UUIDField(null=True, blank=True)
    account_number = models.CharField(max_length=24, blank=True)
    sequence = models.PositiveIntegerField(default=0)
    correlation_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "onboarding_application"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"application {self.id} [{self.status}]"


class StatusHistory(models.Model):
    """Append-only timeline. What the customer sees as progress, and what an
    ops analyst reads when an application is stuck."""

    application = models.ForeignKey(Application, on_delete=models.CASCADE,
                                    related_name="history")
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16)
    reason = models.CharField(max_length=120, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "onboarding_status_history"
        ordering = ["at"]


class EligibilityResult(models.Model):
    application = models.OneToOneField(Application, on_delete=models.CASCADE,
                                       related_name="eligibility")
    decision = models.CharField(max_length=8)          # PASS | REVIEW | FAIL
    risk_score = models.IntegerField()                 # 0-100, higher = riskier
    tier = models.CharField(max_length=12)             # BASIC | STANDARD | PREMIUM
    factors = models.JSONField(default=list)           # explainable, per-factor
    policy_version = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "onboarding_eligibility_result"
