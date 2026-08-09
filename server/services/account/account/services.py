"""account-svc domain logic."""

from __future__ import annotations

import logging
import random
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from platform_common.errors import NotFound, ValidationFailed
from platform_common.events import EventEnvelope, publish
from platform_common.observability.context import get_correlation_id

from .models import Account, Beneficiary, LimitPolicy, LimitReservation, LimitUsage

logger = logging.getLogger(__name__)

DEFAULT_LIMITS = {
    "BASIC":    (Decimal("50000"), Decimal("100000"), Decimal("500000"), 10),
    "STANDARD": (Decimal("200000"), Decimal("500000"), Decimal("2000000"), 20),
    "PREMIUM":  (Decimal("1000000"), Decimal("2500000"), Decimal("10000000"), 50),
}


def _luhn_check_digit(number: str) -> str:
    """Check digit for ``number`` (the payload, without the check digit).

    Luhn doubles every second digit counting from the right of the *complete*
    number, so the parity must be derived from len(payload) + 1 -- not from the
    payload length, which is off by one and produces numbers that fail
    validation.
    """
    total, parity = 0, (len(number) + 1) % 2
    for index, digit in enumerate(number):
        value = int(digit)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


def generate_account_number() -> str:
    base = f"5021{random.randint(0, 99999999):08d}"
    return base + _luhn_check_digit(base)


def day_window() -> str:
    return f"DAY:{timezone.now():%Y-%m-%d}"


def month_window() -> str:
    return f"MONTH:{timezone.now():%Y-%m}"


def open_account(*, user_id, currency="INR", tier="STANDARD",
                 account_type="SAVINGS", idempotency_key: str = "") -> Account:
    """Open an account. Idempotent when an idempotency_key is supplied.

    Without the key a retry after an ambiguous failure would open a second
    account for the same customer -- which is why onboarding always passes the
    application id.
    """
    if idempotency_key:
        existing = Account.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            logger.info("account creation replayed for key %s", idempotency_key)
            return existing

    with transaction.atomic():
        account = Account.objects.create(
            user_id=user_id, account_number=generate_account_number(),
            ifsc="INGB0000521", currency=currency, tier=tier,
            account_type=account_type, idempotency_key=idempotency_key,
        )
        ensure_tier_limits(tier, currency)
        publish(
            EventEnvelope(
                event_type="account.opened",
                aggregate_type="account",
                aggregate_id=str(account.id),
                sequence=0,
                producer="account",
                correlation_id=get_correlation_id() or "",
                payload={
                    "account_id": str(account.id),
                    "user_id": str(user_id),
                    "account_number_masked": account.masked,
                    "currency": currency,
                    "account_type": account_type,
                    "tier": tier,
                    "opened_at": account.opened_at.isoformat(),
                },
            )
        )
    return account


def ensure_tier_limits(tier: str, currency: str = "INR") -> LimitPolicy:
    per_txn, daily, monthly, count = DEFAULT_LIMITS.get(tier, DEFAULT_LIMITS["STANDARD"])
    policy, _ = LimitPolicy.objects.get_or_create(
        scope="TIER", scope_ref=tier, rail="ANY",
        defaults={"currency": currency, "per_txn_max": per_txn, "daily_max": daily,
                  "monthly_max": monthly, "daily_count_max": count},
    )
    return policy


def add_beneficiary(*, user_id, nickname, beneficiary_type, account_number,
                    bank_code="", swift_bic="", country="IN", currency="INR",
                    cooling_off_hours: int = 24) -> Beneficiary:
    from datetime import timedelta

    fingerprint = Beneficiary.make_fingerprint(beneficiary_type, account_number, bank_code)
    with transaction.atomic():
        beneficiary, created = Beneficiary.objects.get_or_create(
            user_id=user_id, fingerprint=fingerprint,
            defaults={
                "nickname": nickname, "beneficiary_type": beneficiary_type,
                "account_number": account_number, "bank_code": bank_code,
                "swift_bic": swift_bic, "country": country, "currency": currency,
                "status": Beneficiary.Status.ACTIVE,
                "cooling_off_until": timezone.now() + timedelta(hours=cooling_off_hours),
            },
        )
        if created:
            publish(
                EventEnvelope(
                    event_type="beneficiary.added",
                    aggregate_type="beneficiary",
                    aggregate_id=str(beneficiary.id),
                    sequence=0,
                    producer="account",
                    correlation_id=get_correlation_id() or "",
                    payload={
                        "beneficiary_id": str(beneficiary.id),
                        "user_id": str(user_id),
                        "beneficiary_type": beneficiary_type,
                        "fingerprint": fingerprint,
                        "country": country,
                        "currency": currency,
                        "cooling_off_until": beneficiary.cooling_off_until.isoformat(),
                    },
                )
            )
    return beneficiary


def resolve_policy(account: Account, rail: str) -> LimitPolicy:
    """Account-scoped policy wins over the tier default."""
    specific = LimitPolicy.objects.filter(
        scope="ACCOUNT", scope_ref=str(account.id), rail__in=[rail, "ANY"]
    ).order_by("rail").first()
    if specific:
        return specific
    tier_policy = LimitPolicy.objects.filter(
        scope="TIER", scope_ref=account.tier, rail__in=[rail, "ANY"]
    ).order_by("rail").first()
    return tier_policy or ensure_tier_limits(account.tier, account.currency)


def check_and_reserve(*, account: Account, rail: str, amount: Decimal,
                      currency: str) -> tuple[bool, str, dict]:
    """Reserve limit budget. Returns (ok, reason, detail).

    Reserving inside a locked transaction is what makes two concurrent transfers
    unable to jointly breach a daily cap.
    """
    policy = resolve_policy(account, rail)

    if amount > policy.per_txn_max:
        return False, "PER_TXN_EXCEEDED", {
            "limit": "PER_TXN_MAX", "cap": str(policy.per_txn_max),
            "requested": str(amount),
        }

    with transaction.atomic():
        day, _ = LimitUsage.objects.select_for_update().get_or_create(
            account_id=account.id, window=day_window(), rail=rail
        )
        month, _ = LimitUsage.objects.select_for_update().get_or_create(
            account_id=account.id, window=month_window(), rail=rail
        )

        if day.amount_used + amount > policy.daily_max:
            return False, "DAILY_EXCEEDED", {
                "limit": "DAILY_MAX", "cap": str(policy.daily_max),
                "used": str(day.amount_used), "requested": str(amount),
            }
        if day.count_used + 1 > policy.daily_count_max:
            return False, "DAILY_COUNT_EXCEEDED", {
                "limit": "DAILY_COUNT_MAX", "cap": policy.daily_count_max,
                "used": day.count_used,
            }
        if month.amount_used + amount > policy.monthly_max:
            return False, "MONTHLY_EXCEEDED", {
                "limit": "MONTHLY_MAX", "cap": str(policy.monthly_max),
                "used": str(month.amount_used), "requested": str(amount),
            }

        day.amount_used += amount
        day.count_used += 1
        day.save(update_fields=["amount_used", "count_used"])
        month.amount_used += amount
        month.count_used += 1
        month.save(update_fields=["amount_used", "count_used"])

        reservation = LimitReservation.objects.create(
            account_id=account.id, rail=rail, amount=amount, currency=currency,
            day_window=day.window, month_window=month.window,
        )

    return True, "", {
        "reservation_id": str(reservation.id),
        "daily_remaining": str(policy.daily_max - day.amount_used),
    }


def release_reservation(reservation_id) -> bool:
    """Give back reserved limit budget. Idempotent."""
    with transaction.atomic():
        reservation = LimitReservation.objects.select_for_update().filter(
            pk=reservation_id
        ).first()
        if reservation is None or reservation.released:
            return False

        for window in (reservation.day_window, reservation.month_window):
            usage = LimitUsage.objects.select_for_update().filter(
                account_id=reservation.account_id, window=window, rail=reservation.rail
            ).first()
            if usage:
                usage.amount_used = max(Decimal("0"), usage.amount_used - reservation.amount)
                usage.count_used = max(0, usage.count_used - 1)
                usage.save(update_fields=["amount_used", "count_used"])

        reservation.released = True
        reservation.save(update_fields=["released"])
    return True


def validate_transfer(*, account_id, user_id, beneficiary_id, amount: Decimal,
                      currency: str, rail: str) -> dict:
    """Synchronous validation called by the payments saga."""
    account = Account.objects.filter(pk=account_id, user_id=user_id).first()
    if account is None:
        return {"ok": False, "reason": "ACCOUNT_NOT_FOUND"}
    if account.status != Account.Status.ACTIVE:
        return {"ok": False, "reason": f"ACCOUNT_{account.status}"}
    if account.currency != currency:
        return {"ok": False, "reason": "CURRENCY_MISMATCH"}

    beneficiary = None
    if beneficiary_id:
        beneficiary = Beneficiary.objects.filter(pk=beneficiary_id, user_id=user_id).first()
        if beneficiary is None:
            return {"ok": False, "reason": "BENEFICIARY_NOT_FOUND"}
        if beneficiary.status == Beneficiary.Status.BLOCKED:
            return {"ok": False, "reason": "BENEFICIARY_BLOCKED"}

    ok, reason, detail = check_and_reserve(
        account=account, rail=rail, amount=Decimal(amount), currency=currency
    )
    if not ok:
        return {"ok": False, "reason": reason, "detail": detail}

    now = timezone.now()
    result = {
        "ok": True,
        "reservation_id": detail["reservation_id"],
        "account_status": account.status,
        "limits": {"daily_remaining": detail["daily_remaining"]},
    }
    if beneficiary:
        in_cooling_off = bool(
            beneficiary.cooling_off_until and beneficiary.cooling_off_until > now
        )
        age_hours = (now - beneficiary.created_at).total_seconds() / 3600
        result["beneficiary"] = {
            "id": str(beneficiary.id),
            "masked": beneficiary.masked,
            "type": beneficiary.beneficiary_type,
            "fingerprint": beneficiary.fingerprint,
            "country": beneficiary.country,
            "age_hours": round(age_hours, 2),
            "in_cooling_off": in_cooling_off,
        }
    return result
