"""Accounts, beneficiaries and limits.

account-svc owns the *policy* side of an account: who owns it, who it may pay,
and how much. It deliberately does not own the balance -- ledger-svc does (I1).
The cached_balance here is a read model for list views only.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from django.db import models

from platform_common.db import MoneyField


class Account(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        FROZEN = "FROZEN", "Frozen"
        DORMANT = "DORMANT", "Dormant"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.UUIDField(db_index=True)
    account_number = models.CharField(max_length=24, unique=True)
    ifsc = models.CharField(max_length=16, blank=True)
    currency = models.CharField(max_length=3, default="INR")
    account_type = models.CharField(max_length=12, default="SAVINGS")
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)
    tier = models.CharField(max_length=12, default="STANDARD")
    opened_at = models.DateTimeField(auto_now_add=True)

    # Creating an account is not naturally idempotent, and the caller may retry
    # after an ambiguous failure (a dropped connection tells you nothing about
    # whether the server acted). Keyed on the onboarding application id, a retry
    # returns the original account instead of opening a second one.
    idempotency_key = models.CharField(max_length=120, blank=True, db_index=True)

    # READ MODEL ONLY -- the authoritative balance lives in ledger-svc.
    cached_balance = MoneyField(default=Decimal("0"))
    balance_as_of = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "account_account"
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=models.Q(idempotency_key__gt=""),
                name="account_uniq_idempotency_key",
            )
        ]

    def __str__(self) -> str:
        return f"{self.account_number} ({self.status})"

    @property
    def masked(self) -> str:
        return f"****{self.account_number[-4:]}"


class Beneficiary(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACTIVE = "ACTIVE", "Active"
        BLOCKED = "BLOCKED", "Blocked"

    class Type(models.TextChoices):
        INTERNAL = "INTERNAL", "Internal"
        DOMESTIC = "DOMESTIC", "Domestic"
        INTERNATIONAL = "INTERNATIONAL", "International"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.UUIDField(db_index=True)
    nickname = models.CharField(max_length=60)
    beneficiary_type = models.CharField(max_length=15, choices=Type.choices)
    account_number = models.CharField(max_length=40)
    bank_code = models.CharField(max_length=20, blank=True)
    swift_bic = models.CharField(max_length=11, blank=True)
    country = models.CharField(max_length=2, default="IN")
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # An anti-fraud control: large transfers to a brand-new payee are restricted
    # for a period, which is when account-takeover fraud usually strikes.
    cooling_off_until = models.DateTimeField(null=True, blank=True)
    fingerprint = models.CharField(max_length=80, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "account_beneficiary"
        constraints = [
            models.UniqueConstraint(fields=["user_id", "fingerprint"],
                                    name="account_uniq_beneficiary")
        ]

    @staticmethod
    def make_fingerprint(beneficiary_type: str, account_number: str, bank_code: str = "") -> str:
        material = f"{beneficiary_type}|{account_number}|{bank_code}".upper()
        return "sha256:" + hashlib.sha256(material.encode()).hexdigest()[:32]

    @property
    def masked(self) -> str:
        return f"****{self.account_number[-4:]}"


class LimitPolicy(models.Model):
    """Admin-managed transaction and account limits."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=10, default="TIER")  # TIER | ACCOUNT
    scope_ref = models.CharField(max_length=64, db_index=True)
    rail = models.CharField(max_length=20, default="ANY")
    currency = models.CharField(max_length=3, default="INR")
    per_txn_max = MoneyField()
    daily_max = MoneyField()
    monthly_max = MoneyField()
    daily_count_max = models.PositiveIntegerField(default=20)
    version = models.PositiveIntegerField(default=1)
    updated_by = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "account_limit_policy"
        constraints = [
            models.UniqueConstraint(fields=["scope", "scope_ref", "rail"],
                                    name="account_uniq_limit_policy")
        ]


class LimitUsage(models.Model):
    """Reserved usage per window.

    Reserving rather than merely reading is what stops two concurrent transfers
    from jointly breaching a limit that each passes individually.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_id = models.UUIDField(db_index=True)
    window = models.CharField(max_length=20)   # "DAY:2026-08-08" | "MONTH:2026-08"
    rail = models.CharField(max_length=20)
    amount_used = MoneyField(default=Decimal("0"))
    count_used = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "account_limit_usage"
        constraints = [
            models.UniqueConstraint(fields=["account_id", "window", "rail"],
                                    name="account_uniq_limit_usage")
        ]


class LimitReservation(models.Model):
    """A held slice of limit budget, released if the transfer does not complete."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_id = models.UUIDField(db_index=True)
    rail = models.CharField(max_length=20)
    amount = MoneyField()
    currency = models.CharField(max_length=3)
    day_window = models.CharField(max_length=20)
    month_window = models.CharField(max_length=20)
    released = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "account_limit_reservation"
