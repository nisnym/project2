"""Django Q2 tasks for identity-svc."""

from __future__ import annotations

from . import services


def purge_expired_tokens() -> dict:
    return services.purge_expired_tokens()


def unlock_expired_lockouts() -> dict:
    return services.unlock_expired_lockouts()


def rotate_signing_key() -> dict:
    """Rotate the RS256 keypair.

    The outgoing key is retired rather than deleted so tokens signed moments
    before rotation keep verifying until they expire.
    """
    from django.utils import timezone

    from .models import SigningKey

    old = SigningKey.objects.filter(is_active=True)
    old.update(is_active=False, retires_at=timezone.now())
    key = services.generate_key()
    return {"new_kid": key.kid, "retired": old.count()}
