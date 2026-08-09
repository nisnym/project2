"""Users, credentials, signing keys and devices.

identity-svc is the only service with a user table. The other nine establish who
is calling from the token alone -- which is why a compromised payments-svc
cannot enumerate customers.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    CUSTOMER = "CUSTOMER", "Customer"
    FRAUD_ANALYST = "FRAUD_ANALYST", "Fraud analyst"
    OPS = "OPS", "Operations"
    ADMIN = "ADMIN", "Administrator"


class User(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACTIVE = "ACTIVE", "Active"
        LOCKED = "LOCKED", "Locked"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True)
    password_hash = models.CharField(max_length=255)
    full_name = models.CharField(max_length=120, blank=True)
    role = models.CharField(max_length=15, choices=Role.choices, default=Role.CUSTOMER)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)

    mfa_secret = models.CharField(max_length=64, blank=True)
    mfa_enabled = models.BooleanField(default=False)

    # Throttling brute force at the account rather than only at the edge: an
    # attacker rotating IPs still hits this.
    failed_logins = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "identity_user"

    def __str__(self) -> str:
        return f"{self.email} ({self.role})"

    def set_password(self, raw: str) -> None:
        self.password_hash = make_password(raw)

    def check_password(self, raw: str) -> bool:
        return check_password(raw, self.password_hash)

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > timezone.now())


class SigningKey(models.Model):
    """RS256 keypair. The private key never leaves identity-svc; peers verify
    with the public half fetched from /.well-known/jwks.json."""

    kid = models.CharField(max_length=40, unique=True)
    public_pem = models.TextField()
    private_pem = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    retires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "identity_signing_key"
        ordering = ["-created_at"]


class Device(models.Model):
    """Registered at login; feeds fraud-svc's device-anomaly rules."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    fingerprint_hash = models.CharField(max_length=80, db_index=True)
    user_agent = models.CharField(max_length=255, blank=True)
    last_ip_country = models.CharField(max_length=2, blank=True)
    trusted = models.BooleanField(default=False)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "identity_device"
        constraints = [
            models.UniqueConstraint(fields=["user", "fingerprint_hash"],
                                    name="identity_uniq_device")
        ]


class RefreshToken(models.Model):
    """Rotating refresh token.

    ``replaced_by`` forms a chain. Presenting a token that has already been
    rotated means either theft or a client bug -- and we cannot tell which, so
    the whole family is revoked and the user re-authenticates.
    """

    jti = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.SET_NULL)
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    replaced_by = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replaces"
    )
    # Every token in one login chain shares this, so revoking a family is one query.
    family_id = models.UUIDField(default=uuid.uuid4, db_index=True)

    class Meta:
        db_table = "identity_refresh_token"

    @property
    def is_usable(self) -> bool:
        return (
            self.revoked_at is None
            and self.replaced_by_id is None
            and self.expires_at > timezone.now()
        )
