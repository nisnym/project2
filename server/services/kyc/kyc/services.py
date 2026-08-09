"""KYC domain logic."""

from __future__ import annotations

import logging
import time
from datetime import date

from django.db import transaction
from django.utils import timezone

from platform_common.errors import NotFound
from platform_common.events import EventEnvelope, publish
from platform_common.observability.context import get_correlation_id

from .adapters import document_provider, identity_provider, sanctions_provider
from .models import KycCase, KycDocument, KycIdentity, KycStatus, RiskRating, ScreeningHit

logger = logging.getLogger(__name__)

IDENTITY_PASS = 70
DOCUMENT_PASS = 65


def create_case(*, application_id, user_id, full_name: str, date_of_birth: date,
                national_id: str, nationality: str, address: dict | None = None) -> KycCase:
    """Create the case and return immediately.

    Document processing and sanctions screening take seconds, so the API returns
    PENDING and the work happens on the queue. onboarding-svc resumes when
    `kyc.completed` arrives.
    """
    existing = KycCase.objects.filter(application_id=application_id).first()
    if existing:
        return existing

    with transaction.atomic():
        case = KycCase.objects.create(
            application_id=application_id, user_id=user_id,
            correlation_id=get_correlation_id() or "",
        )
        KycIdentity.objects.create(
            case=case, full_name=full_name, date_of_birth=date_of_birth,
            national_id=national_id, nationality=nationality, address=address or {},
        )
    return case


def add_document(*, case_id, doc_type: str, sha256: str, filename: str = "") -> KycDocument:
    case = KycCase.objects.filter(pk=case_id).first()
    if case is None:
        raise NotFound(f"KYC case {case_id} not found")
    return KycDocument.objects.create(
        case=case, doc_type=doc_type, sha256=sha256, filename=filename
    )


def process_case(case_id) -> KycCase:
    """Run identity, document and sanctions checks. Idempotent."""
    case = KycCase.objects.filter(pk=case_id).select_related("identity").first()
    if case is None:
        raise NotFound(f"KYC case {case_id} not found")
    if case.status in (KycStatus.PASSED, KycStatus.FAILED, KycStatus.MANUAL_REVIEW):
        return case  # already decided; a Q2 redelivery must not re-decide

    started = time.perf_counter()
    with transaction.atomic():
        case.status = KycStatus.IN_PROGRESS
        case.save(update_fields=["status"])

    identity = case.identity
    # Slow external I/O deliberately outside any transaction: a 30s provider
    # call holding a database transaction open is how connection pools die.
    identity_result = identity_provider.verify(
        full_name=identity.full_name, date_of_birth=identity.date_of_birth,
        national_id=identity.national_id, nationality=identity.nationality,
    )
    documents = list(case.documents.all())
    document_results = [
        (doc, document_provider.analyse(doc_type=doc.doc_type, sha256=doc.sha256))
        for doc in documents
    ]
    screening = sanctions_provider.screen(
        full_name=identity.full_name, date_of_birth=identity.date_of_birth,
        nationality=identity.nationality,
    )

    with transaction.atomic():
        for doc, result in document_results:
            doc.authenticity_score = result.authenticity_score
            doc.ocr_result = result.ocr
            doc.status = "PASSED" if result.authenticity_score >= DOCUMENT_PASS else "FAILED"
            doc.save(update_fields=["authenticity_score", "ocr_result", "status"])

        for hit in screening.hits:
            ScreeningHit.objects.create(case=case, **hit)

        case.identity_score = identity_result.score
        case.provider_ref = identity_result.provider_ref
        case.document_score = (
            min(r.authenticity_score for _, r in document_results)
            if document_results else None
        )
        case.sanctions_hit = any(not h["is_pep"] for h in screening.hits)
        case.pep_hit = any(h["is_pep"] for h in screening.hits)
        case.status, case.risk_rating, case.failure_reason = _decide(case)
        case.processing_ms = int((time.perf_counter() - started) * 1000)
        case.completed_at = timezone.now()
        case.save()

        if screening.hits:
            publish(
                EventEnvelope(
                    event_type="kyc.screening_hit",
                    aggregate_type="kyc_case", aggregate_id=str(case.id), sequence=0,
                    producer="kyc", correlation_id=case.correlation_id,
                    payload={
                        "case_id": str(case.id), "application_id": str(case.application_id),
                        "user_id": str(case.user_id),
                        "lists": [h["list_name"] for h in screening.hits],
                        "sanctions_hit": case.sanctions_hit, "pep_hit": case.pep_hit,
                    },
                )
            )

        publish(
            EventEnvelope(
                event_type="kyc.completed",
                aggregate_type="kyc_case", aggregate_id=str(case.id), sequence=1,
                producer="kyc", correlation_id=case.correlation_id,
                # No name, no DOB, no identifier: PII does not leave this service.
                payload={
                    "case_id": str(case.id),
                    "application_id": str(case.application_id),
                    "user_id": str(case.user_id),
                    "status": case.status,
                    "identity_score": case.identity_score,
                    "document_score": case.document_score,
                    "risk_rating": case.risk_rating,
                    "sanctions_hit": case.sanctions_hit,
                    "pep_hit": case.pep_hit,
                    "processing_ms": case.processing_ms,
                },
            )
        )

    logger.info("KYC %s -> %s (%sms)", case.id, case.status, case.processing_ms)
    return case


def _decide(case: KycCase) -> tuple[str, str, str]:
    """Hard stops first, then scores. Order matters: a sanctions hit is not
    something a good identity score should be able to outweigh."""
    if case.sanctions_hit:
        return KycStatus.FAILED, RiskRating.HIGH, "SANCTIONS_HIT"
    if case.pep_hit:
        # A PEP is not disqualified, but is never auto-approved.
        return KycStatus.MANUAL_REVIEW, RiskRating.HIGH, "PEP_MATCH"
    if (case.identity_score or 0) < IDENTITY_PASS:
        return KycStatus.FAILED, RiskRating.HIGH, "IDENTITY_NOT_VERIFIED"
    if case.document_score is None:
        return KycStatus.MANUAL_REVIEW, RiskRating.MEDIUM, "NO_DOCUMENTS"
    if case.document_score < DOCUMENT_PASS:
        return KycStatus.MANUAL_REVIEW, RiskRating.MEDIUM, "DOCUMENT_QUALITY"

    combined = (case.identity_score + case.document_score) / 2
    rating = RiskRating.LOW if combined >= 85 else RiskRating.MEDIUM
    return KycStatus.PASSED, rating, ""


def sweep_stuck_cases(hours: int = 24) -> dict:
    """A case that never completed must not leave an application hanging."""
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(hours=hours)
    stuck = KycCase.objects.filter(
        status__in=[KycStatus.PENDING, KycStatus.IN_PROGRESS], created_at__lt=cutoff
    )[:100]

    swept = 0
    for case in stuck:
        with transaction.atomic():
            case.status = KycStatus.MANUAL_REVIEW
            case.failure_reason = "PROCESSING_TIMEOUT"
            case.risk_rating = RiskRating.MEDIUM
            case.completed_at = timezone.now()
            case.save()
            publish(
                EventEnvelope(
                    event_type="kyc.completed",
                    aggregate_type="kyc_case", aggregate_id=str(case.id), sequence=1,
                    producer="kyc", correlation_id=case.correlation_id,
                    payload={"case_id": str(case.id),
                             "application_id": str(case.application_id),
                             "user_id": str(case.user_id), "status": case.status,
                             "risk_rating": case.risk_rating,
                             "sanctions_hit": False, "pep_hit": False},
                )
            )
        swept += 1
    return {"swept": swept}
