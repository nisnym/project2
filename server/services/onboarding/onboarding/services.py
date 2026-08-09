"""Onboarding workflow orchestration."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db import transaction

from platform_common.errors import IllegalStateTransition, NotFound, ValidationFailed
from platform_common.events import EventEnvelope, publish
from platform_common.http import get_client
from platform_common.observability.context import get_correlation_id

from .eligibility import EligibilityRequest, age_from, evaluate
from .models import (
    ALLOWED_TRANSITIONS,
    Application,
    ApplicationStatus,
    EligibilityResult,
    StatusHistory,
)

logger = logging.getLogger(__name__)

REQUIRED_INFO = ("full_name", "date_of_birth", "national_id", "nationality")


def transition(app: Application, to_status: str, *, reason: str = "") -> Application:
    """Move the state machine, guarded by the transition table."""
    if to_status not in ALLOWED_TRANSITIONS.get(app.status, set()):
        raise IllegalStateTransition(
            f"cannot move from {app.status} to {to_status}",
            detail={"from": app.status, "to": to_status,
                    "allowed": sorted(ALLOWED_TRANSITIONS.get(app.status, set()))},
        )
    previous = app.status
    app.status = to_status
    app.status_reason = reason[:120]
    app.sequence += 1
    app.save(update_fields=["status", "status_reason", "sequence", "updated_at"])
    StatusHistory.objects.create(
        application=app, from_status=previous, to_status=to_status, reason=reason
    )
    publish(
        EventEnvelope(
            event_type="onboarding.status_changed",
            aggregate_type="application", aggregate_id=str(app.id),
            sequence=app.sequence, producer="onboarding",
            correlation_id=app.correlation_id or get_correlation_id() or "",
            payload={"application_id": str(app.id), "user_id": str(app.user_id),
                     "from": previous, "to": to_status, "reason": reason},
        )
    )
    return app


def create_application(*, user_id, customer_info: dict) -> Application:
    missing = [f for f in REQUIRED_INFO if not customer_info.get(f)]
    if missing:
        raise ValidationFailed("Required customer information is missing.",
                               detail={"missing": missing})
    with transaction.atomic():
        return Application.objects.create(
            user_id=user_id, customer_info=customer_info,
            correlation_id=get_correlation_id() or "",
        )


def submit(application_id) -> Application:
    """Submit, then hand off to kyc-svc.

    The KYC call is synchronous but trivial -- it only creates the case. The
    actual document and sanctions work happens on kyc-svc's own queue, which is
    why this returns in milliseconds while KYC takes seconds.
    """
    app = Application.objects.filter(pk=application_id).first()
    if app is None:
        raise NotFound("Application not found.")

    with transaction.atomic():
        transition(app, ApplicationStatus.SUBMITTED)

    info = app.customer_info
    try:
        result = get_client("kyc", timeout=5.0).post(
            "/internal/kyc/cases",
            json={
                "application_id": str(app.id), "user_id": str(app.user_id),
                "full_name": info["full_name"], "date_of_birth": info["date_of_birth"],
                "national_id": info["national_id"],
                "nationality": info.get("nationality", "IN"),
                "address": info.get("address", {}),
                "documents": info.get("documents", []),
            },
        )
    except Exception as exc:
        logger.exception("could not create KYC case for %s", app.id)
        with transaction.atomic():
            transition(app, ApplicationStatus.REJECTED, reason="KYC_UNAVAILABLE")
        raise

    with transaction.atomic():
        app.kyc_case_id = result["case_id"]
        app.save(update_fields=["kyc_case_id"])
        transition(app, ApplicationStatus.KYC_PENDING)
    return app


def on_kyc_completed(*, application_id, kyc_status: str, risk_rating: str,
                     identity_score: int | None, sanctions_hit: bool,
                     pep_hit: bool) -> Application | None:
    """Resume the workflow when kyc.completed arrives.

    Order-tolerant: a duplicate or late event finds the application already past
    KYC_PENDING and does nothing.
    """
    app = Application.objects.filter(pk=application_id).first()
    if app is None:
        logger.warning("kyc.completed for unknown application %s", application_id)
        return None
    if app.status != ApplicationStatus.KYC_PENDING:
        logger.info("ignoring kyc.completed for %s in status %s", app.id, app.status)
        return app

    with transaction.atomic():
        if kyc_status == "FAILED":
            transition(app, ApplicationStatus.KYC_FAILED, reason="KYC_FAILED")
            _publish_rejected(app, "KYC_FAILED")
            return app
        if kyc_status == "MANUAL_REVIEW":
            transition(app, ApplicationStatus.MANUAL_REVIEW, reason="KYC_MANUAL_REVIEW")
            _publish_review(app, "KYC_MANUAL_REVIEW")
            return app

        transition(app, ApplicationStatus.KYC_PASSED)

    return run_eligibility(
        app, risk_rating=risk_rating, identity_score=identity_score or 0,
        sanctions_hit=sanctions_hit, pep_hit=pep_hit, kyc_status=kyc_status,
    )


def run_eligibility(app: Application, *, risk_rating: str, identity_score: int,
                    sanctions_hit: bool, pep_hit: bool,
                    kyc_status: str = "PASSED") -> Application:
    info = app.customer_info
    request = EligibilityRequest(
        age=age_from(date.fromisoformat(info["date_of_birth"])),
        nationality=info.get("nationality", "IN"),
        annual_income=Decimal(str(info.get("annual_income", "0"))),
        employment_status=info.get("employment_status", "UNEMPLOYED"),
        kyc_status=kyc_status, kyc_risk_rating=risk_rating,
        identity_score=identity_score, sanctions_hit=sanctions_hit, pep_hit=pep_hit,
    )
    outcome = evaluate(request)

    with transaction.atomic():
        EligibilityResult.objects.update_or_create(
            application=app,
            defaults={
                "decision": outcome.decision, "risk_score": outcome.risk_score,
                "tier": outcome.tier,
                "factors": [f.__dict__ for f in outcome.factors],
                "policy_version": outcome.policy_version,
            },
        )
        if outcome.decision == "FAIL":
            transition(app, ApplicationStatus.REJECTED, reason=outcome.reason)
            _publish_rejected(app, outcome.reason)
            return app
        if outcome.decision == "REVIEW":
            transition(app, ApplicationStatus.MANUAL_REVIEW, reason=outcome.reason)
            _publish_review(app, outcome.reason)
            return app
        transition(app, ApplicationStatus.ELIGIBLE)

    return open_account(app)


def open_account(app: Application) -> Application:
    """Generate the account synchronously, so "instant account details" is
    literally true -- the number comes back in the same response cycle."""
    if app.status != ApplicationStatus.ELIGIBLE:
        raise IllegalStateTransition(f"cannot open an account from {app.status}")

    tier = app.eligibility.tier if hasattr(app, "eligibility") else "STANDARD"
    try:
        result = get_client("account", timeout=5.0).post(
            "/internal/accounts",
            json={"user_id": str(app.user_id),
                  "currency": app.customer_info.get("currency", "INR"), "tier": tier},
            # Derived from the application, so a retry after an ambiguous
            # failure returns the same account rather than opening a second one.
            idempotency_key=f"onboarding:{app.id}",
        )
    except Exception as exc:
        logger.exception("account creation failed for %s", app.id)
        with transaction.atomic():
            transition(app, ApplicationStatus.FAILED, reason=f"ACCOUNT_CREATION_FAILED")
        return app

    with transaction.atomic():
        app.account_id = result["account_id"]
        app.account_number = result["account_number"]
        app.save(update_fields=["account_id", "account_number"])
        transition(app, ApplicationStatus.ACCOUNT_OPENED)
        publish(
            EventEnvelope(
                event_type="onboarding.completed",
                aggregate_type="application", aggregate_id=str(app.id),
                sequence=app.sequence, producer="onboarding",
                correlation_id=app.correlation_id,
                payload={"application_id": str(app.id), "user_id": str(app.user_id),
                         "account_id": str(app.account_id),
                         "account_number_masked": f"****{app.account_number[-4:]}",
                         "tier": tier},
            )
        )
    return app


def _publish_rejected(app: Application, reason: str) -> None:
    publish(
        EventEnvelope(
            event_type="onboarding.rejected",
            aggregate_type="application", aggregate_id=str(app.id),
            sequence=app.sequence, producer="onboarding",
            correlation_id=app.correlation_id,
            payload={"application_id": str(app.id), "user_id": str(app.user_id),
                     "reason": reason},
        )
    )


def _publish_review(app: Application, reason: str) -> None:
    publish(
        EventEnvelope(
            event_type="onboarding.review_required",
            aggregate_type="application", aggregate_id=str(app.id),
            sequence=app.sequence, producer="onboarding",
            correlation_id=app.correlation_id,
            payload={"application_id": str(app.id), "user_id": str(app.user_id),
                     "reason": reason},
        )
    )


def ops_decision(application_id, *, approve: bool, actor: str, note: str = "") -> Application:
    """Manual override from the ops queue."""
    app = Application.objects.filter(pk=application_id).first()
    if app is None:
        raise NotFound("Application not found.")
    if app.status != ApplicationStatus.MANUAL_REVIEW:
        raise IllegalStateTransition(f"application is {app.status}, not under review")

    if not approve:
        with transaction.atomic():
            transition(app, ApplicationStatus.REJECTED, reason=note or "OPS_REJECTED")
            _publish_rejected(app, note or "OPS_REJECTED")
        return app

    with transaction.atomic():
        transition(app, ApplicationStatus.ELIGIBLE, reason=f"approved by {actor}")
        EligibilityResult.objects.update_or_create(
            application=app,
            defaults={"decision": "PASS", "risk_score": 50, "tier": "BASIC",
                      "factors": [{"code": "OPS_OVERRIDE", "points": 0, "detail": note}],
                      "policy_version": "manual"},
        )
    return open_account(app)


def sweep_stuck_applications(hours: int = 24) -> dict:
    """A KYC case that never completes must not leave an application hanging."""
    from datetime import timedelta

    from django.utils import timezone

    cutoff = timezone.now() - timedelta(hours=hours)
    stuck = Application.objects.filter(
        status=ApplicationStatus.KYC_PENDING, updated_at__lt=cutoff
    )[:100]

    swept = 0
    for app in stuck:
        with transaction.atomic():
            transition(app, ApplicationStatus.MANUAL_REVIEW, reason="KYC_TIMEOUT")
            _publish_review(app, "KYC_TIMEOUT")
        swept += 1
    return {"swept": swept}
