"""HTTP layer for onboarding-svc."""

from __future__ import annotations

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import IsAuthenticatedPrincipal, IsOps
from platform_common.auth.authentication import JWTAuthentication
from platform_common.errors import NotFound

from . import services
from .models import Application


def _serialise(app: Application) -> dict:
    body = {
        "id": str(app.id), "status": app.status, "status_reason": app.status_reason,
        "timeline": [
            {"status": h.to_status, "at": h.at.isoformat(), "reason": h.reason}
            for h in app.history.all()
        ],
    }
    eligibility = getattr(app, "eligibility", None)
    if eligibility:
        body["eligibility"] = {
            "decision": eligibility.decision, "risk_score": eligibility.risk_score,
            "tier": eligibility.tier, "factors": eligibility.factors,
            "policy_version": eligibility.policy_version,
        }
    if app.account_id:
        body["account"] = {"id": str(app.account_id),
                           "account_number": app.account_number}
    return body


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticatedPrincipal])
def create_application(request):
    app = services.create_application(
        user_id=request.user.id, customer_info=request.data.get("customer_info", {})
    )
    return Response(_serialise(app), status=201)


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticatedPrincipal])
def submit_application(request, application_id):
    app = Application.objects.filter(pk=application_id, user_id=request.user.id).first()
    if app is None:
        raise NotFound("Application not found.")
    return Response(_serialise(services.submit(app.id)), status=202)


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticatedPrincipal])
def get_application(request, application_id):
    """Polled by the SPA while KYC runs."""
    app = Application.objects.filter(pk=application_id).prefetch_related("history").first()
    if app is None:
        raise NotFound("Application not found.")
    if not request.user.is_staff_role and str(app.user_id) != str(request.user.id):
        raise NotFound("Application not found.")   # don't confirm existence
    return Response(_serialise(app))


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsOps])
def ops_decision(request, application_id):
    app = services.ops_decision(
        application_id, approve=bool(request.data.get("approve")),
        actor=str(request.user.id), note=request.data.get("note", ""),
    )
    return Response(_serialise(app))
