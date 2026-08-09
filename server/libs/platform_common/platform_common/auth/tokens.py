"""JWT issue/verify.

User tokens are RS256: identity-svc holds the private key and every other
service verifies with the public key from a cached JWKS. There is no shared
secret to leak, and a compromised service cannot mint tokens.

Service-to-service tokens are HS256 signed with ``INTERNAL_HMAC_KEY``. They
never leave the private network, are short-lived, and using the symmetric key
here avoids every service needing an identity-svc round trip to make an internal
call -- including when identity-svc itself is down.
"""

from __future__ import annotations

import logging
import time

import httpx
import jwt
from django.conf import settings
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

__all__ = [
    "decode_user_token",
    "issue_service_token",
    "decode_service_token",
    "TokenError",
    "reset_jwks_cache",
]

# Tolerate a little clock drift between hosts. 30s is generous for NTP-synced
# machines and far below the 15-minute access-token lifetime.
LEEWAY_SECONDS = 30
SERVICE_TOKEN_TTL_SECONDS = 300

_jwk_client: PyJWKClient | None = None


class TokenError(Exception):
    """Any failure to establish a caller's identity from a token."""


def reset_jwks_cache() -> None:
    global _jwk_client
    _jwk_client = None


def _jwks_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        identity_url = settings.SERVICE_REGISTRY["identity"].rstrip("/")
        _jwk_client = PyJWKClient(
            f"{identity_url}/.well-known/jwks.json",
            cache_keys=True,
            lifespan=getattr(settings, "JWKS_CACHE_SECONDS", 300),
        )
    return _jwk_client


def decode_user_token(token: str) -> dict:
    """Verify an RS256 access token and return its claims."""
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
    except httpx.HTTPError as exc:
        raise TokenError(f"cannot reach identity-svc for JWKS: {exc}") from exc
    except Exception as exc:
        # A rotated key that is not yet in our cached JWKS lands here. Clearing
        # the cache lets the next request pick up the new key rather than
        # failing until the TTL lapses.
        reset_jwks_cache()
        raise TokenError(f"no signing key for this token: {exc}") from exc

    try:
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],           # never accept 'none' or a symmetric alg here
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"TOKEN_INVALID: {exc}") from exc


def issue_service_token(
    caller: str, scopes: tuple[str, ...] = (), *, ttl: int = SERVICE_TOKEN_TTL_SECONDS
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": caller,
            "typ": "service",
            "scope": list(scopes),
            "iat": now,
            "exp": now + ttl,
            "iss": caller,
            "aud": "internal",
        },
        settings.INTERNAL_HMAC_KEY,
        algorithm="HS256",
    )


def decode_service_token(token: str) -> dict:
    try:
        claims = jwt.decode(
            token,
            settings.INTERNAL_HMAC_KEY,
            algorithms=["HS256"],
            audience="internal",
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("SERVICE_TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"SERVICE_TOKEN_INVALID: {exc}") from exc

    if claims.get("typ") != "service":
        # Refuse to let a user token be replayed against an internal endpoint.
        raise TokenError("not a service token")
    return claims
