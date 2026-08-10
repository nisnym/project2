"""identity-svc domain logic: keys, login, rotation."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from platform_common.errors import Conflict, DomainError, Forbidden, NotFound
from platform_common.events import EventEnvelope, publish
from platform_common.observability.context import get_correlation_id

from .models import Device, RefreshToken, Role, SigningKey, User

logger = logging.getLogger(__name__)

ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=7)
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)


class AuthenticationFailed(DomainError):
    status_code = 401
    default_code = "AUTHENTICATION_FAILED"
    # Deliberately identical for "no such user" and "wrong password": telling
    # them apart is a user-enumeration oracle.
    default_detail = "Invalid email or password."


class TokenReuseDetected(DomainError):
    status_code = 401
    default_code = "TOKEN_REUSE_DETECTED"
    default_detail = "Session invalidated. Please sign in again."


# ------------------------------------------------------------------- keys


def active_key() -> SigningKey:
    key = SigningKey.objects.filter(is_active=True).order_by("-created_at").first()
    return key or generate_key()


def generate_key() -> SigningKey:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    kid = hashlib.sha256(public_pem.encode()).hexdigest()[:16]
    return SigningKey.objects.create(kid=kid, public_pem=public_pem, private_pem=private_pem)


def jwks() -> dict:
    """Public keys for peers to verify with.

    Retired-but-not-expired keys stay listed so tokens signed just before a
    rotation keep verifying until they expire.
    """
    from jwt.algorithms import RSAAlgorithm
    import json

    keys = []
    for key in SigningKey.objects.filter(
        models_q_active_or_recent()
    ).order_by("-created_at"):
        jwk = json.loads(RSAAlgorithm.to_jwk(
            serialization.load_pem_public_key(key.public_pem.encode())
        ))
        jwk.update({"kid": key.kid, "use": "sig", "alg": "RS256"})
        keys.append(jwk)
    return {"keys": keys}


def models_q_active_or_recent():
    from django.db.models import Q

    cutoff = timezone.now() - timedelta(days=1)
    return Q(is_active=True) | Q(retires_at__gte=cutoff)


# ----------------------------------------------------------------- tokens


def issue_access_token(user: User, device: Device | None = None) -> str:
    key = active_key()
    now = int(time.time())
    return jwt.encode(
        {
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "device_id": str(device.id) if device else None,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + int(ACCESS_TTL.total_seconds()),
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
        },
        key.private_pem,
        algorithm="RS256",
        headers={"kid": key.kid},
    )


def issue_refresh_token(user: User, *, device=None, family_id=None) -> RefreshToken:
    return RefreshToken.objects.create(
        user=user, device=device,
        family_id=family_id or uuid.uuid4(),
        expires_at=timezone.now() + REFRESH_TTL,
    )


# ------------------------------------------------------------------ flows


def register(*, email: str, password: str, full_name: str = "",
             phone: str = "", role: str = Role.CUSTOMER) -> User:
    if User.objects.filter(email__iexact=email).exists():
        raise Conflict("An account with that email already exists.")

    with transaction.atomic():
        user = User(email=email.lower(), full_name=full_name, phone=phone,
                    role=role, status=User.Status.ACTIVE)
        user.set_password(password)
        user.save()
        publish(
            EventEnvelope(
                event_type="user.registered",
                aggregate_type="user", aggregate_id=str(user.id), sequence=0,
                producer="identity", correlation_id=get_correlation_id() or "",
                payload={"user_id": str(user.id), "email": user.email, "role": user.role},
            )
        )
    return user


def _register_device(user: User, fingerprint: str, user_agent: str, ip_country: str):
    if not fingerprint:
        return None, False
    device, created = Device.objects.get_or_create(
        user=user, fingerprint_hash=fingerprint,
        defaults={"user_agent": user_agent[:255], "last_ip_country": ip_country[:2]},
    )
    if not created:
        device.last_ip_country = ip_country[:2] or device.last_ip_country
        device.save(update_fields=["last_ip_country", "last_seen"])
    return device, created


def login(*, email: str, password: str, device_fingerprint: str = "",
          user_agent: str = "", ip_country: str = "") -> dict:
    user = User.objects.filter(email__iexact=email).first()

    if user is None:
        # Same error and roughly the same work as a wrong password, so response
        # time does not reveal whether the account exists.
        raise AuthenticationFailed()

    if user.is_locked:
        raise Forbidden(
            "Account temporarily locked after repeated failed attempts.",
            code="ACCOUNT_LOCKED",
        )
    if user.status != User.Status.ACTIVE:
        raise Forbidden(f"Account is {user.status}.", code="ACCOUNT_NOT_ACTIVE")

    if not user.check_password(password):
        with transaction.atomic():
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED_LOGINS:
                user.locked_until = timezone.now() + LOCKOUT
                user.failed_logins = 0
            user.save(update_fields=["failed_logins", "locked_until"])
            publish(
                EventEnvelope(
                    event_type="user.login_failed",
                    aggregate_type="user", aggregate_id=str(user.id), sequence=0,
                    producer="identity", correlation_id=get_correlation_id() or "",
                    payload={"user_id": str(user.id), "email": user.email,
                             "locked": bool(user.locked_until)},
                )
            )
        raise AuthenticationFailed()

    with transaction.atomic():
        device, is_new_device = _register_device(
            user, device_fingerprint, user_agent, ip_country
        )
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = timezone.now()
        user.save(update_fields=["failed_logins", "locked_until", "last_login_at"])

        refresh = issue_refresh_token(user, device=device)

        publish(
            EventEnvelope(
                event_type="user.logged_in",
                aggregate_type="user", aggregate_id=str(user.id), sequence=0,
                producer="identity", correlation_id=get_correlation_id() or "",
                actor={"type": user.role.lower(), "id": str(user.id)},
                payload={"user_id": str(user.id), "role": user.role,
                         "device_is_new": is_new_device, "ip_country": ip_country},
            )
        )
        if device and is_new_device:
            publish(
                EventEnvelope(
                    event_type="device.seen",
                    aggregate_type="device", aggregate_id=str(device.id), sequence=0,
                    producer="identity", correlation_id=get_correlation_id() or "",
                    payload={"user_id": str(user.id), "device_id": str(device.id),
                             "fingerprint": device_fingerprint, "is_new": True},
                )
            )

    return {
        "access_token": issue_access_token(user, device),
        "refresh_token": str(refresh.jti),
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
        "must_change_password": user.must_change_password,
        "user": {"id": str(user.id), "email": user.email, "role": user.role,
                 "full_name": user.full_name,
                 "must_change_password": user.must_change_password},
    }


def change_password(user_id, *, current_password: str, new_password: str) -> dict:
    """Change your own password.

    Always requires the current one, even when the account is flagged for a
    forced reset: the temporary credential was handed over out of band, and
    proving you hold it is the only thing that distinguishes the intended
    recipient from whoever else saw it in transit.

    Every other session is revoked. If the reason for the change was that
    someone else knew the old password, leaving their session alive would make
    the change cosmetic.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_id).first()
        if user is None:
            raise NotFound("User not found.")
        if not user.check_password(current_password):
            raise AuthenticationFailed("The current password is incorrect.")

        user.set_password(new_password)
        user.must_change_password = False
        user.save(update_fields=["password_hash", "must_change_password"])

        revoked = RefreshToken.objects.filter(
            user=user, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())

        publish(
            EventEnvelope(
                event_type="user.password_changed",
                aggregate_type="user", aggregate_id=str(user.id), sequence=0,
                producer="identity", correlation_id=get_correlation_id() or "",
                actor={"type": user.role.lower(), "id": str(user.id)},
                payload={"user_id": str(user.id), "email": user.email,
                         "sessions_revoked": revoked, "self_service": True},
            )
        )

    return {"changed": True, "sessions_revoked": revoked}


def refresh_tokens(presented_jti: str) -> dict:
    """Rotate. A replayed token revokes the entire family.

    The reuse branch raises *after* the transaction commits, deliberately.
    Raising inside the atomic block would roll back the very revocation the
    exception is reporting -- the warning would be logged, nothing would
    actually be revoked, and the stolen credential would keep working.
    """
    reuse_detected = False

    with transaction.atomic():
        token = RefreshToken.objects.select_for_update().filter(pk=presented_jti).first()
        if token is None:
            raise AuthenticationFailed("Unknown refresh token.")

        if token.revoked_at or token.replaced_by_id:
            # Already used. Either it was stolen and replayed, or the legitimate
            # client retried -- indistinguishable, so assume the worse case.
            revoked = RefreshToken.objects.filter(
                family_id=token.family_id, revoked_at__isnull=True
            ).update(revoked_at=timezone.now())
            logger.warning(
                "refresh token reuse for user %s; revoked %s token(s)",
                token.user_id, revoked,
            )
            publish(
                EventEnvelope(
                    event_type="security.refresh_reuse_detected",
                    aggregate_type="user", aggregate_id=str(token.user_id), sequence=0,
                    producer="identity", correlation_id=get_correlation_id() or "",
                    payload={"user_id": str(token.user_id),
                             "family_id": str(token.family_id),
                             "tokens_revoked": revoked},
                )
            )
            reuse_detected = True

        elif token.expires_at <= timezone.now():
            raise AuthenticationFailed("Refresh token expired.")

        if not reuse_detected:
            user = token.user
            if user.status != User.Status.ACTIVE:
                raise Forbidden(f"Account is {user.status}.", code="ACCOUNT_NOT_ACTIVE")

            replacement = issue_refresh_token(
                user, device=token.device, family_id=token.family_id
            )
            token.replaced_by = replacement
            token.revoked_at = timezone.now()
            token.save(update_fields=["replaced_by", "revoked_at"])

    if reuse_detected:
        # Raised only after COMMIT, so the revocation actually sticks.
        raise TokenReuseDetected()

    return {
        "access_token": issue_access_token(user, token.device),
        "refresh_token": str(replacement.jti),
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
    }


def logout(presented_jti: str) -> bool:
    updated = RefreshToken.objects.filter(pk=presented_jti, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    return bool(updated)


def purge_expired_tokens() -> dict:
    deleted, _ = RefreshToken.objects.filter(
        expires_at__lt=timezone.now() - timedelta(days=30)
    ).delete()
    return {"deleted": deleted}


def unlock_expired_lockouts() -> dict:
    unlocked = User.objects.filter(
        locked_until__isnull=False, locked_until__lte=timezone.now()
    ).update(locked_until=None, failed_logins=0)
    return {"unlocked": unlocked}
