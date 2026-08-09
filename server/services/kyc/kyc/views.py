"""HTTP layer for kyc-svc."""

from __future__ import annotations

from django.db import transaction
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import IsService
from platform_common.auth.authentication import ServiceTokenAuthentication

from . import services
from .models import KycCase


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes([IsService])
def create_case(request):
    """Called synchronously by onboarding-svc; returns immediately."""
    from datetime import date

    from django_q.tasks import async_task

    data = request.data
    case = services.create_case(
        application_id=data["application_id"],
        user_id=data["user_id"],
        full_name=data["full_name"],
        date_of_birth=date.fromisoformat(data["date_of_birth"]),
        national_id=data["national_id"],
        nationality=data.get("nationality", "IN"),
        address=data.get("address", {}),
    )
    for document in data.get("documents", []):
        services.add_document(
            case_id=case.id, doc_type=document["doc_type"],
            sha256=document["sha256"], filename=document.get("filename", ""),
        )

    transaction.on_commit(
        lambda: async_task("kyc.tasks.process_case", str(case.id), q_options={"save": False})
    )
    return Response({"case_id": str(case.id), "status": case.status}, status=201)


@api_view(["GET"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes([IsService])
def case_status(request, case_id):
    """Status and scores only -- never the underlying PII."""
    case = KycCase.objects.filter(pk=case_id).first()
    if case is None:
        return Response({"error": {"code": "NOT_FOUND"}}, status=404)
    return Response({
        "case_id": str(case.id), "status": case.status,
        "identity_score": case.identity_score, "document_score": case.document_score,
        "risk_rating": case.risk_rating, "sanctions_hit": case.sanctions_hit,
        "pep_hit": case.pep_hit, "failure_reason": case.failure_reason,
    })
