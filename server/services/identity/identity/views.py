"""HTTP layer for identity-svc."""

from __future__ import annotations

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import IsAuthenticatedPrincipal
from platform_common.auth.authentication import JWTAuthentication
from platform_common.errors import ValidationFailed

from . import services
from .models import User
from .serializers import ChangePasswordSerializer


def _required(data, *fields):
    missing = [f for f in fields if not data.get(f)]
    if missing:
        raise ValidationFailed(
            "Required fields are missing.",
            detail={"missing": missing},
        )


@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def register(request):
    _required(request.data, "email", "password")
    if len(request.data["password"]) < 8:
        raise ValidationFailed("Password must be at least 8 characters.")
    user = services.register(
        email=request.data["email"],
        password=request.data["password"],
        full_name=request.data.get("full_name", ""),
        phone=request.data.get("phone", ""),
    )
    return Response({"id": str(user.id), "email": user.email, "role": user.role}, status=201)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def login(request):
    _required(request.data, "email", "password")
    result = services.login(
        email=request.data["email"],
        password=request.data["password"],
        device_fingerprint=request.data.get("device_fingerprint", ""),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        ip_country=request.data.get("ip_country", ""),
    )
    return Response(result)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def refresh(request):
    _required(request.data, "refresh_token")
    return Response(services.refresh_tokens(request.data["refresh_token"]))


@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def logout(request):
    _required(request.data, "refresh_token")
    return Response({"revoked": services.logout(request.data["refresh_token"])})


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticatedPrincipal])
def me(request):
    user = User.objects.filter(pk=request.user.id).first()
    if user is None:
        return Response({"error": {"code": "NOT_FOUND"}}, status=404)
    return Response({
        "id": str(user.id), "email": user.email, "role": user.role,
        "full_name": user.full_name, "status": user.status,
        "mfa_enabled": user.mfa_enabled,
        "must_change_password": user.must_change_password,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    })


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticatedPrincipal])
def change_password(request):
    payload = ChangePasswordSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    return Response(services.change_password(request.user.id, **payload.validated_data))


@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
def jwks(request):
    """Public keys, fetched and cached by every other service."""
    return Response(services.jwks())
