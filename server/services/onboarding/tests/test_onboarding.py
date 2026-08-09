"""Onboarding workflow and eligibility decisioning."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from onboarding import services
from onboarding.eligibility import EligibilityRequest, age_from, evaluate
from onboarding.models import Application, ApplicationStatus
from platform_common.errors import IllegalStateTransition, ValidationFailed

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_outbound_events(monkeypatch):
    monkeypatch.setattr(
        "platform_common.events.publisher._enqueue_relay", lambda outbox_id: None
    )


@pytest.fixture
def peers(monkeypatch):
    """kyc-svc and account-svc, faked at the client boundary."""
    calls = []

    class FakeClient:
        def __init__(self, name):
            self.name = name

        def post(self, path, json=None, **kwargs):
            calls.append((self.name, path, json))
            if self.name == "kyc":
                return {"case_id": str(uuid.uuid4()), "status": "PENDING"}
            return {"account_id": str(uuid.uuid4()), "account_number": "5021123456789",
                    "ifsc": "INGB0000521", "currency": "INR", "tier": "STANDARD"}

    monkeypatch.setattr("onboarding.services.get_client",
                        lambda name, **kwargs: FakeClient(name))
    return calls


def customer_info(**overrides):
    info = {
        "full_name": "Asha Menon", "date_of_birth": "1994-03-12",
        "national_id": "ABCDE1234F", "nationality": "IN",
        "annual_income": "1200000", "employment_status": "SALARIED",
    }
    info.update(overrides)
    return info


class TestEligibilityEngine:
    """Pure function: no I/O, so every branch is trivially reachable."""

    def _request(self, **overrides):
        base = dict(age=30, nationality="IN", annual_income=Decimal("1200000"),
                    employment_status="SALARIED", kyc_status="PASSED",
                    kyc_risk_rating="LOW", identity_score=92)
        base.update(overrides)
        return EligibilityRequest(**base)

    def test_a_strong_applicant_passes(self):
        result = evaluate(self._request())
        assert result.decision == "PASS"
        assert result.tier in ("PREMIUM", "STANDARD")

    def test_sanctions_hit_is_a_hard_stop(self):
        """A good income cannot outweigh a sanctions match."""
        result = evaluate(self._request(sanctions_hit=True, annual_income=Decimal("99999999")))
        assert result.decision == "FAIL"
        assert result.reason == "SANCTIONS_HIT"

    def test_underage_is_rejected(self):
        assert evaluate(self._request(age=17)).reason == "UNDERAGE"

    def test_prohibited_country_is_rejected(self):
        assert evaluate(self._request(nationality="KP")).reason == "PROHIBITED_COUNTRY"

    def test_failed_kyc_is_rejected(self):
        assert evaluate(self._request(kyc_status="FAILED")).reason == "KYC_FAILED"

    def test_a_pep_is_reviewed_not_rejected(self):
        result = evaluate(self._request(pep_hit=True))
        assert result.decision == "REVIEW"
        assert result.reason == "PEP_MATCH"

    def test_a_weak_applicant_is_reviewed(self):
        result = evaluate(self._request(
            annual_income=Decimal("100000"), employment_status="UNEMPLOYED",
            kyc_risk_rating="HIGH", identity_score=71,
        ))
        assert result.decision == "REVIEW"

    def test_higher_income_never_increases_risk(self):
        """Monotonicity: a scoring model that punishes a better applicant is a
        bug, and an easy one to introduce when tweaking weights."""
        low = evaluate(self._request(annual_income=Decimal("300000"))).risk_score
        high = evaluate(self._request(annual_income=Decimal("2500000"))).risk_score
        assert high <= low

    def test_decisions_are_explainable(self):
        result = evaluate(self._request())
        assert result.factors
        assert all(f.code and isinstance(f.points, int) for f in result.factors)
        assert result.policy_version

    def test_age_calculation_handles_a_birthday_not_yet_reached(self):
        assert age_from(date(2000, 12, 31), today=date(2026, 6, 1)) == 25
        assert age_from(date(2000, 1, 1), today=date(2026, 6, 1)) == 26


class TestWorkflow:
    def test_application_requires_core_information(self):
        with pytest.raises(ValidationFailed, match="missing"):
            services.create_application(user_id=uuid.uuid4(),
                                        customer_info={"full_name": "A"})

    def test_submit_creates_a_kyc_case_and_returns_immediately(self, peers):
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        app = services.submit(app.id)

        assert app.status == ApplicationStatus.KYC_PENDING
        assert app.kyc_case_id is not None
        assert peers[0][0] == "kyc"

    def test_kyc_pass_opens_an_account_synchronously(self, peers):
        """'Instant account details' means the number comes back in the same
        cycle, not on a queue."""
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        services.submit(app.id)

        app = services.on_kyc_completed(
            application_id=app.id, kyc_status="PASSED", risk_rating="LOW",
            identity_score=92, sanctions_hit=False, pep_hit=False,
        )
        assert app.status == ApplicationStatus.ACCOUNT_OPENED
        assert app.account_number == "5021123456789"
        assert app.eligibility.decision == "PASS"

    def test_kyc_failure_rejects_without_touching_account_svc(self, peers):
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        services.submit(app.id)
        app = services.on_kyc_completed(
            application_id=app.id, kyc_status="FAILED", risk_rating="HIGH",
            identity_score=20, sanctions_hit=True, pep_hit=False,
        )
        assert app.status == ApplicationStatus.KYC_FAILED
        assert not any(name == "account" for name, _, _ in peers)

    def test_kyc_manual_review_parks_the_application(self, peers):
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        services.submit(app.id)
        app = services.on_kyc_completed(
            application_id=app.id, kyc_status="MANUAL_REVIEW", risk_rating="HIGH",
            identity_score=80, sanctions_hit=False, pep_hit=True,
        )
        assert app.status == ApplicationStatus.MANUAL_REVIEW

    def test_ops_can_approve_a_parked_application(self, peers):
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        services.submit(app.id)
        services.on_kyc_completed(
            application_id=app.id, kyc_status="MANUAL_REVIEW", risk_rating="HIGH",
            identity_score=80, sanctions_hit=False, pep_hit=True,
        )
        app = services.ops_decision(app.id, approve=True, actor="ops-1", note="verified")
        assert app.status == ApplicationStatus.ACCOUNT_OPENED

    def test_ops_can_reject_a_parked_application(self, peers):
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        services.submit(app.id)
        services.on_kyc_completed(
            application_id=app.id, kyc_status="MANUAL_REVIEW", risk_rating="HIGH",
            identity_score=80, sanctions_hit=False, pep_hit=True,
        )
        app = services.ops_decision(app.id, approve=False, actor="ops-1", note="no")
        assert app.status == ApplicationStatus.REJECTED


class TestStateMachine:
    def test_illegal_transitions_raise(self, peers):
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        with pytest.raises(IllegalStateTransition):
            services.transition(app, ApplicationStatus.ACCOUNT_OPENED)

    def test_terminal_states_are_final(self, peers):
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        services.submit(app.id)
        services.on_kyc_completed(
            application_id=app.id, kyc_status="FAILED", risk_rating="HIGH",
            identity_score=10, sanctions_hit=True, pep_hit=False,
        )
        app.refresh_from_db()
        with pytest.raises(IllegalStateTransition):
            services.transition(app, ApplicationStatus.ELIGIBLE)

    def test_a_duplicate_kyc_event_changes_nothing(self, peers):
        """Events are at-least-once; a redelivery must not open a second account."""
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        services.submit(app.id)
        for _ in range(2):
            services.on_kyc_completed(
                application_id=app.id, kyc_status="PASSED", risk_rating="LOW",
                identity_score=92, sanctions_hit=False, pep_hit=False,
            )
        assert sum(1 for name, _, _ in peers if name == "account") == 1

    def test_the_timeline_records_every_step(self, peers):
        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        services.submit(app.id)
        services.on_kyc_completed(
            application_id=app.id, kyc_status="PASSED", risk_rating="LOW",
            identity_score=92, sanctions_hit=False, pep_hit=False,
        )
        statuses = list(
            Application.objects.get(pk=app.id).history.values_list("to_status", flat=True)
        )
        assert statuses == ["SUBMITTED", "KYC_PENDING", "KYC_PASSED",
                            "ELIGIBLE", "ACCOUNT_OPENED"]

    def test_a_stalled_application_is_swept_to_review(self, peers):
        from datetime import timedelta

        from django.utils import timezone

        app = services.create_application(user_id=uuid.uuid4(),
                                          customer_info=customer_info())
        services.submit(app.id)
        Application.objects.filter(pk=app.pk).update(
            updated_at=timezone.now() - timedelta(hours=48)
        )
        assert services.sweep_stuck_applications()["swept"] == 1
        app.refresh_from_db()
        assert app.status == ApplicationStatus.MANUAL_REVIEW
