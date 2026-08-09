"""DRF authentication classes."""

from __future__ import annotations

import logging

from rest_framework import authentication, exceptions

from ..observability.context import set_actor
from .principal import Principal, ServicePrincipal
from .tokens import TokenError, decode_service_token, decode_user_token

logger = logging.getLogger(__name__)

__all__ = ["JWTAuthentication", "ServiceTokenAuthentication", "AnyTokenAuthentication"]


def _bearer(request) -> str | None:
    header = authentication.get_authorization_header(request).decode("latin-1")
    if not header:
        return None
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


class JWTAuthentication(authentication.BaseAuthentication):
    """RS256 user access tokens."""

    keyword = "Bearer"

    def authenticate(self, request):
        token = _bearer(request)
        if not token:
            return None  # unauthenticated; permission classes decide what that means

        try:
            claims = decode_user_token(token)
        except TokenError as exc:
            raise exceptions.AuthenticationFailed(str(exc)) from exc

        if claims.get("typ") == "service":
            raise exceptions.AuthenticationFailed(
                "service token presented on a user endpoint"
            )

        principal = Principal(
            id=str(claims["sub"]),
            role=claims.get("role", "CUSTOMER"),
            email=claims.get("email", ""),
            device_id=claims.get("device_id"),
            token_id=claims.get("jti"),
            claims=claims,
        )
        # Make the actor available to logging and to event envelopes without
        # threading it through every function signature.
        set_actor({"type": principal.role.lower(), "id": principal.id})
        return (principal, token)

    def authenticate_header(self, request) -> str:
        return self.keyword


class ServiceTokenAuthentication(authentication.BaseAuthentication):
    """HS256 service tokens for ``/internal/*``."""

    keyword = "Bearer"

    def authenticate(self, request):
        token = _bearer(request)
        if not token:
            return None

        try:
            claims = decode_service_token(token)
        except TokenError as exc:
            raise exceptions.AuthenticationFailed(str(exc)) from exc

        principal = ServicePrincipal(
            service=str(claims["sub"]),
            scopes=tuple(claims.get("scope", ())),
            claims=claims,
        )
        set_actor({"type": "service", "id": principal.service})
        return (principal, token)

    def authenticate_header(self, request) -> str:
        return self.keyword


class AnyTokenAuthentication(authentication.BaseAuthentication):
    """Accept either kind. For endpoints reachable by a user *or* a service --
    used sparingly, because 'either' usually means the endpoint is doing two
    jobs."""

    def authenticate(self, request):
        token = _bearer(request)
        if not token:
            return None
        try:
            return ServiceTokenAuthentication().authenticate(request)
        except exceptions.AuthenticationFailed:
            return JWTAuthentication().authenticate(request)

    def authenticate_header(self, request) -> str:
        return "Bearer"
