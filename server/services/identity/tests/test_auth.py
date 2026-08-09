"""Authentication: token issuance, rotation, reuse detection, lockout.

Several of these are the security controls that a "just get login working"
implementation skips, and that show up in a pen test.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import jwt
import pytest
from django.utils import timezone

from identity import services
from identity.models import Device, RefreshToken, Role, SigningKey, User
from platform_common.errors import Conflict, Forbidden

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_outbound_events(monkeypatch):
    monkeypatch.setattr(
        "platform_common.events.publisher._enqueue_relay", lambda outbox_id: None
    )


@pytest.fixture
def user():
    return services.register(
        email="asha@example.com", password="correct-horse-battery",
        full_name="Asha Menon",
    )


class TestRegistration:
    def test_creates_an_active_customer(self, user):
        assert user.role == Role.CUSTOMER
        assert user.status == User.Status.ACTIVE

    def test_password_is_hashed_not_stored(self, user):
        assert "correct-horse-battery" not in user.password_hash
        assert user.check_password("correct-horse-battery")

    def test_production_uses_a_slow_hasher(self):
        """The suite runs MD5 for speed; production must not.

        Guards against someone copying the test hasher into real settings.
        """
        from platform_common.service_settings import base_settings

        configured = base_settings("identity").get("PASSWORD_HASHERS")
        assert configured is None or "MD5" not in str(configured)

    def test_email_is_normalised(self, user):
        assert user.email == "asha@example.com"

    def test_duplicate_email_is_rejected(self, user):
        with pytest.raises(Conflict):
            services.register(email="ASHA@example.com", password="another-password")


class TestLogin:
    def test_returns_a_usable_token_pair(self, user):
        result = services.login(email="asha@example.com", password="correct-horse-battery")
        assert result["token_type"] == "Bearer"
        assert result["expires_in"] == 900
        assert RefreshToken.objects.filter(pk=result["refresh_token"]).exists()

    def test_access_token_carries_the_expected_claims(self, user):
        result = services.login(email="asha@example.com", password="correct-horse-battery")
        claims = jwt.decode(result["access_token"], options={"verify_signature": False})
        assert claims["sub"] == str(user.id)
        assert claims["role"] == Role.CUSTOMER
        assert claims["iss"] == "identity-svc"
        assert claims["aud"] == "banking-platform"

    def test_token_is_signed_rs256_with_a_kid(self, user):
        """RS256 not HS256: peers verify with a public key, so a compromised
        service cannot mint tokens."""
        result = services.login(email="asha@example.com", password="correct-horse-battery")
        header = jwt.get_unverified_header(result["access_token"])
        assert header["alg"] == "RS256"
        assert header["kid"] == SigningKey.objects.get(is_active=True).kid

    def test_token_verifies_against_the_published_jwks(self, user):
        """The exact path every other service takes."""
        from jwt import PyJWK

        result = services.login(email="asha@example.com", password="correct-horse-battery")
        published = services.jwks()["keys"][0]
        claims = jwt.decode(
            result["access_token"], PyJWK(published).key, algorithms=["RS256"],
            issuer="identity-svc", audience="banking-platform",
        )
        assert claims["sub"] == str(user.id)

    def test_wrong_password_is_rejected(self, user):
        with pytest.raises(services.AuthenticationFailed):
            services.login(email="asha@example.com", password="wrong")

    def test_unknown_email_gives_the_same_error(self, user):
        """Different errors would be a user-enumeration oracle."""
        with pytest.raises(services.AuthenticationFailed) as unknown:
            services.login(email="nobody@example.com", password="whatever")
        with pytest.raises(services.AuthenticationFailed) as wrong:
            services.login(email="asha@example.com", password="wrong")
        assert str(unknown.value) == str(wrong.value)

    def test_repeated_failures_lock_the_account(self, user):
        for _ in range(services.MAX_FAILED_LOGINS):
            with pytest.raises(services.AuthenticationFailed):
                services.login(email="asha@example.com", password="wrong")

        user.refresh_from_db()
        assert user.is_locked
        # Even the correct password is refused while locked.
        with pytest.raises(Forbidden, match="locked"):
            services.login(email="asha@example.com", password="correct-horse-battery")

    def test_a_successful_login_clears_the_failure_count(self, user):
        with pytest.raises(services.AuthenticationFailed):
            services.login(email="asha@example.com", password="wrong")
        services.login(email="asha@example.com", password="correct-horse-battery")
        user.refresh_from_db()
        assert user.failed_logins == 0

    def test_a_closed_account_cannot_log_in(self, user):
        User.objects.filter(pk=user.pk).update(status=User.Status.CLOSED)
        with pytest.raises(Forbidden, match="CLOSED"):
            services.login(email="asha@example.com", password="correct-horse-battery")

    def test_device_is_registered_once(self, user):
        for _ in range(2):
            services.login(email="asha@example.com", password="correct-horse-battery",
                           device_fingerprint="sha256:laptop", ip_country="IN")
        assert Device.objects.filter(user=user).count() == 1


class TestRefreshRotation:
    def _login(self):
        return services.login(email="asha@example.com", password="correct-horse-battery")

    def test_refresh_issues_a_new_pair(self, user):
        first = self._login()
        second = services.refresh_tokens(first["refresh_token"])
        assert second["refresh_token"] != first["refresh_token"]

    def test_the_old_token_is_retired(self, user):
        first = self._login()
        services.refresh_tokens(first["refresh_token"])
        old = RefreshToken.objects.get(pk=first["refresh_token"])
        assert old.revoked_at is not None
        assert old.replaced_by_id is not None

    def test_rotation_stays_in_one_family(self, user):
        first = self._login()
        second = services.refresh_tokens(first["refresh_token"])
        assert (
            RefreshToken.objects.get(pk=first["refresh_token"]).family_id
            == RefreshToken.objects.get(pk=second["refresh_token"]).family_id
        )

    def test_replaying_a_token_revokes_the_whole_family(self, user):
        """The control that turns a stolen refresh token from a persistent
        backdoor into a single-use failure that logs the victim out."""
        first = self._login()
        second = services.refresh_tokens(first["refresh_token"])

        with pytest.raises(services.TokenReuseDetected):
            services.refresh_tokens(first["refresh_token"])   # replay

        # The attacker's replay also killed the legitimate current token.
        assert RefreshToken.objects.get(pk=second["refresh_token"]).revoked_at is not None
        with pytest.raises(services.TokenReuseDetected):
            services.refresh_tokens(second["refresh_token"])

    def test_expired_refresh_is_rejected(self, user):
        first = self._login()
        RefreshToken.objects.filter(pk=first["refresh_token"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(services.AuthenticationFailed, match="expired"):
            services.refresh_tokens(first["refresh_token"])

    def test_unknown_token_is_rejected(self, user):
        with pytest.raises(services.AuthenticationFailed):
            services.refresh_tokens(str(uuid.uuid4()))

    def test_logout_revokes(self, user):
        first = self._login()
        assert services.logout(first["refresh_token"]) is True
        with pytest.raises(services.TokenReuseDetected):
            services.refresh_tokens(first["refresh_token"])

    def test_a_frozen_account_cannot_refresh(self, user):
        first = self._login()
        User.objects.filter(pk=user.pk).update(status=User.Status.LOCKED)
        with pytest.raises(Forbidden):
            services.refresh_tokens(first["refresh_token"])


class TestKeyRotation:
    def test_jwks_publishes_the_active_key(self, user):
        services.active_key()
        published = services.jwks()["keys"]
        assert len(published) == 1
        assert published[0]["alg"] == "RS256"
        assert published[0]["kty"] == "RSA"

    def test_jwks_never_exposes_the_private_key(self, user):
        services.active_key()
        blob = str(services.jwks())
        assert "PRIVATE" not in blob
        assert "d" not in services.jwks()["keys"][0]   # RSA private exponent

    def test_rotation_keeps_the_old_key_verifiable(self, user):
        """A token signed a second before rotation must not break."""
        from identity.tasks import rotate_signing_key
        from jwt import PyJWK

        result = services.login(email="asha@example.com", password="correct-horse-battery")
        old_kid = jwt.get_unverified_header(result["access_token"])["kid"]

        rotate_signing_key()

        published = {key["kid"]: key for key in services.jwks()["keys"]}
        assert old_kid in published, "retired key vanished; live tokens would break"
        jwt.decode(
            result["access_token"], PyJWK(published[old_kid]).key, algorithms=["RS256"],
            issuer="identity-svc", audience="banking-platform",
        )

    def test_new_tokens_use_the_new_key(self, user):
        from identity.tasks import rotate_signing_key

        before = jwt.get_unverified_header(
            services.login(email="asha@example.com", password="correct-horse-battery")["access_token"]
        )["kid"]
        rotate_signing_key()
        after = jwt.get_unverified_header(
            services.login(email="asha@example.com", password="correct-horse-battery")["access_token"]
        )["kid"]
        assert after != before


class TestMaintenance:
    def test_lockouts_expire(self, user):
        from identity.tasks import unlock_expired_lockouts

        User.objects.filter(pk=user.pk).update(
            locked_until=timezone.now() - timedelta(minutes=1), failed_logins=5
        )
        assert unlock_expired_lockouts() == {"unlocked": 1}
        user.refresh_from_db()
        assert not user.is_locked

    def test_ancient_tokens_are_purged(self, user):
        from identity.tasks import purge_expired_tokens

        services.login(email="asha@example.com", password="correct-horse-battery")
        RefreshToken.objects.update(expires_at=timezone.now() - timedelta(days=40))
        assert purge_expired_tokens()["deleted"] == 1
