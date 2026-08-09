"""KYC processing, decisioning, and PII containment."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from kyc import services
from kyc.models import KycCase, KycStatus, RiskRating, ScreeningHit

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_outbound_events(monkeypatch):
    monkeypatch.setattr(
        "platform_common.events.publisher._enqueue_relay", lambda outbox_id: None
    )


def make_case(name="Asha Menon", national_id="ABCDE1234F", with_docs=True):
    case = services.create_case(
        application_id=uuid.uuid4(), user_id=uuid.uuid4(), full_name=name,
        date_of_birth=date(1994, 3, 12), national_id=national_id, nationality="IN",
    )
    if with_docs:
        services.add_document(case_id=case.id, doc_type="PASSPORT", sha256="a" * 64)
    return case


class TestCaseCreation:
    def test_case_starts_pending(self):
        assert make_case().status == KycStatus.PENDING

    def test_national_id_is_hashed_for_search(self):
        case = make_case()
        identity = case.identity
        assert identity.national_id_hash
        assert identity.national_id_hash != identity.national_id
        assert len(identity.national_id_hash) == 64

    def test_creation_is_idempotent_per_application(self):
        application_id = uuid.uuid4()
        first = services.create_case(
            application_id=application_id, user_id=uuid.uuid4(), full_name="A",
            date_of_birth=date(1990, 1, 1), national_id="X", nationality="IN",
        )
        second = services.create_case(
            application_id=application_id, user_id=uuid.uuid4(), full_name="B",
            date_of_birth=date(1990, 1, 1), national_id="Y", nationality="IN",
        )
        assert first.id == second.id


class TestProcessing:
    def test_a_clean_customer_passes(self):
        case = services.process_case(make_case().id)
        assert case.status == KycStatus.PASSED
        assert case.identity_score >= services.IDENTITY_PASS
        assert case.risk_rating in (RiskRating.LOW, RiskRating.MEDIUM)
        assert case.processing_ms is not None

    def test_processing_is_idempotent(self):
        case = make_case()
        first = services.process_case(case.id)
        second = services.process_case(case.id)
        assert first.completed_at == second.completed_at

    def test_results_are_deterministic(self):
        """Same input, same outcome -- demos repeat and tests do not flake."""
        first = services.process_case(make_case(national_id="SAME123").id)
        second = services.process_case(make_case(national_id="SAME123").id)
        assert first.identity_score == second.identity_score

    def test_a_sanctioned_name_fails_outright(self):
        case = services.process_case(make_case(name="Viktor Petrov").id)
        assert case.status == KycStatus.FAILED
        assert case.failure_reason == "SANCTIONS_HIT"
        assert case.risk_rating == RiskRating.HIGH
        assert ScreeningHit.objects.filter(case=case, list_name="OFAC").exists()

    def test_a_pep_goes_to_manual_review_not_rejection(self):
        """A PEP is bankable, but never auto-approved."""
        case = services.process_case(make_case(name="Maria Santos").id)
        assert case.status == KycStatus.MANUAL_REVIEW
        assert case.failure_reason == "PEP_MATCH"
        assert case.pep_hit is True

    def test_no_documents_means_manual_review(self):
        case = services.process_case(make_case(with_docs=False).id)
        assert case.status == KycStatus.MANUAL_REVIEW
        assert case.failure_reason == "NO_DOCUMENTS"

    def test_a_stuck_case_is_swept_to_review(self):
        from datetime import timedelta

        from django.utils import timezone

        case = make_case()
        KycCase.objects.filter(pk=case.pk).update(
            created_at=timezone.now() - timedelta(hours=48)
        )
        assert services.sweep_stuck_cases()["swept"] == 1
        case.refresh_from_db()
        assert case.status == KycStatus.MANUAL_REVIEW
        assert case.failure_reason == "PROCESSING_TIMEOUT"


class TestPiiContainment:
    def test_the_completed_event_carries_no_pii(self):
        """The whole reason kyc-svc is its own service. A name or national id
        leaking into an event would put PII in nine other databases."""
        from platform_common.models import OutboxEvent

        case = services.process_case(make_case(name="Asha Menon",
                                               national_id="ABCDE1234F").id)
        rows = OutboxEvent.objects.filter(event_type="kyc.completed")
        assert rows.exists()

        for row in rows:
            blob = str(row.envelope)
            assert "Asha" not in blob
            assert "ABCDE1234F" not in blob
            assert "1994-03-12" not in blob
        payload = rows.first().envelope["payload"]
        assert set(payload) >= {"status", "risk_rating", "identity_score"}

    def test_the_status_endpoint_exposes_no_pii(self):
        from django.test import Client

        from platform_common.auth.tokens import issue_service_token

        case = services.process_case(make_case(name="Asha Menon").id)
        response = Client().get(
            f"/internal/kyc/cases/{case.id}",
            HTTP_AUTHORIZATION=f"Bearer {issue_service_token('onboarding')}",
        )
        assert response.status_code == 200
        assert "Asha" not in response.content.decode()
