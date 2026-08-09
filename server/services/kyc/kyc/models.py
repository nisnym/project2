"""KYC cases, documents and screening hits.

kyc-svc holds the most sensitive data in the estate: names, dates of birth,
national identifiers, ID documents. That is the whole reason it is a separate
service with its own database -- a bug in payments-svc cannot reach it.

PII never leaves this service. The `kyc.completed` event carries scores and a
risk rating; it carries no name, no DOB, no identifier.
"""

from __future__ import annotations

import hashlib
import uuid

from django.db import models


class KycStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    PASSED = "PASSED", "Passed"
    FAILED = "FAILED", "Failed"
    MANUAL_REVIEW = "MANUAL_REVIEW", "Manual review"


class RiskRating(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"


class KycCase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application_id = models.UUIDField(db_index=True)
    user_id = models.UUIDField(db_index=True)
    status = models.CharField(max_length=15, choices=KycStatus.choices,
                              default=KycStatus.PENDING, db_index=True)

    identity_score = models.IntegerField(null=True, blank=True)
    document_score = models.IntegerField(null=True, blank=True)
    sanctions_hit = models.BooleanField(default=False)
    pep_hit = models.BooleanField(default=False)
    risk_rating = models.CharField(max_length=8, choices=RiskRating.choices, blank=True)

    provider_ref = models.CharField(max_length=60, blank=True)
    failure_reason = models.CharField(max_length=120, blank=True)
    processing_ms = models.IntegerField(null=True, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "kyc_case"
        constraints = [
            models.UniqueConstraint(fields=["application_id"], name="kyc_uniq_per_application")
        ]

    def __str__(self) -> str:
        return f"KYC {self.id} [{self.status}]"


class KycIdentity(models.Model):
    """The PII itself.

    ``national_id_hash`` is searchable (duplicate-application detection) without
    being reversible; the raw value is stored separately so it can be purged on
    a retention schedule while the hash remains useful.
    """

    case = models.OneToOneField(KycCase, on_delete=models.CASCADE, related_name="identity")
    full_name = models.CharField(max_length=140)
    date_of_birth = models.DateField()
    national_id = models.CharField(max_length=64)
    national_id_hash = models.CharField(max_length=64, db_index=True)
    nationality = models.CharField(max_length=2)
    address = models.JSONField(default=dict)
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "kyc_identity"

    @staticmethod
    def hash_national_id(value: str) -> str:
        return hashlib.sha256(value.strip().upper().encode()).hexdigest()

    def save(self, *args, **kwargs):
        if self.national_id and not self.national_id_hash:
            self.national_id_hash = self.hash_national_id(self.national_id)
        super().save(*args, **kwargs)


class KycDocument(models.Model):
    class DocType(models.TextChoices):
        PASSPORT = "PASSPORT", "Passport"
        NATIONAL_ID = "NATIONAL_ID", "National ID"
        UTILITY_BILL = "UTILITY_BILL", "Utility bill"
        SELFIE = "SELFIE", "Selfie"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(KycCase, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=15, choices=DocType.choices)
    filename = models.CharField(max_length=200, blank=True)
    sha256 = models.CharField(max_length=64, db_index=True)
    authenticity_score = models.IntegerField(null=True, blank=True)
    ocr_result = models.JSONField(default=dict)
    status = models.CharField(max_length=15, default="PENDING")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "kyc_document"


class ScreeningHit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(KycCase, on_delete=models.CASCADE, related_name="hits")
    list_name = models.CharField(max_length=30)   # OFAC | UN | EU | INTERNAL_PEP
    matched_name = models.CharField(max_length=140)
    match_score = models.IntegerField()
    is_pep = models.BooleanField(default=False)
    resolved = models.BooleanField(default=False)
    resolution_note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "kyc_screening_hit"
