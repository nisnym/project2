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


def resolve_internal_account(account_number: str, *, owner_user_id) -> Account:
    """Find the in-bank account behind a payee's account number.

    Failing here is the point. An INTERNAL payee whose number matches no open
    account cannot be paid, and saying so now turns a transfer that would
    otherwise post into a phantom ledger account into a form error.

    The error deliberately does not distinguish "no such account" from "closed"
    or "frozen": either answer, repeated over a number range, is an account
    enumeration oracle.
    """
    account = Account.objects.filter(account_number=account_number).first()
    if account is None or account.status != Account.Status.ACTIVE:
        raise ValidationFailed(
            "No open IND Bank account matches that account number.",
            code="INTERNAL_PAYEE_UNRESOLVED",
        )
    if str(account.user_id) == str(owner_user_id):
        # Paying yourself is a transfer between your own accounts, which is a
        # different flow with different limits -- not a payee.
        raise ValidationFailed(
            "That is your own account. Use a transfer between your accounts instead.",
            code="SELF_PAYEE",
        )
    return account


def add_beneficiary(*, user_id, nickname, beneficiary_type, account_number,
                    bank_code="", swift_bic="", country="IN",
                    currency="INR") -> Beneficiary:
    """Add a payee, immediately usable.

    There is no cooling-off window. A brand-new payee is still one of the
    strongest fraud signals there is, but it is applied by screening every
    transfer rather than by refusing to route them for a day -- so a legitimate
    customer paying a new landlord is not made to wait, and an attacker paying a
    mule account is scored on exactly the same evidence.
    """
    internal_account = None
    if beneficiary_type == Beneficiary.Type.INTERNAL:
        internal_account = resolve_internal_account(account_number, owner_user_id=user_id)
        if internal_account.currency != currency:
            raise ValidationFailed(
                f"That account is held in {internal_account.currency}.",
                detail={"account_currency": internal_account.currency,
                        "requested": currency},
            )

    fingerprint = Beneficiary.make_fingerprint(beneficiary_type, account_number, bank_code)
    with transaction.atomic():
        beneficiary, created = Beneficiary.objects.get_or_create(
            user_id=user_id, fingerprint=fingerprint,
            defaults={
                "nickname": nickname, "beneficiary_type": beneficiary_type,
                "account_number": account_number, "bank_code": bank_code,
                "swift_bic": swift_bic, "country": country, "currency": currency,
                "status": Beneficiary.Status.ACTIVE,
                "internal_account_id": internal_account.id if internal_account else None,
                "internal_user_id": internal_account.user_id if internal_account else None,
                "resolved_at": timezone.now() if internal_account else None,
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
                        # fraud-svc records the fingerprint on this event; that
                        # record is what later makes the payee "not new".
                        "internal": bool(internal_account),
                    },
                )
            )
    return beneficiary


def block_beneficiary(beneficiary: Beneficiary) -> Beneficiary:
    """Stop a payee being paid, and say so out loud.

    The event matters as much as the status change: blocking a payee is what a
    customer does the moment they realise one was added without their consent,
    and fraud-svc and notification-svc both need to hear about it. Emitting
    nothing -- as this path used to -- makes the single clearest signal of an
    account takeover invisible to every service built to spot one.
    """
    if beneficiary.status == Beneficiary.Status.BLOCKED:
        return beneficiary

    with transaction.atomic():
        beneficiary.status = Beneficiary.Status.BLOCKED
        beneficiary.save(update_fields=["status"])
        publish(
            EventEnvelope(
                event_type="beneficiary.blocked",
                aggregate_type="beneficiary",
                aggregate_id=str(beneficiary.id),
                sequence=0,
                producer="account",
                correlation_id=get_correlation_id() or "",
                payload={
                    "beneficiary_id": str(beneficiary.id),
                    "user_id": str(beneficiary.user_id),
                    "beneficiary_type": beneficiary.beneficiary_type,
                    "fingerprint": beneficiary.fingerprint,
                    "country": beneficiary.country,
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
    credit_account = None
    if beneficiary_id:
        beneficiary = Beneficiary.objects.filter(pk=beneficiary_id, user_id=user_id).first()
        if beneficiary is None:
            return {"ok": False, "reason": "BENEFICIARY_NOT_FOUND"}
        if beneficiary.status == Beneficiary.Status.BLOCKED:
            return {"ok": False, "reason": "BENEFICIARY_BLOCKED"}

        if rail == "INTERNAL":
            credit_account, failure = _internal_credit_account(beneficiary)
            if failure:
                return {"ok": False, "reason": failure}

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
        # How new the payee is stays a fraud signal even though it no longer
        # restricts anything: fraud-svc scores on it, account-svc does not act
        # on it.
        age_hours = (now - beneficiary.created_at).total_seconds() / 3600
        result["beneficiary"] = {
            "id": str(beneficiary.id),
            "masked": beneficiary.masked,
            "type": beneficiary.beneficiary_type,
            "fingerprint": beneficiary.fingerprint,
            "country": beneficiary.country,
            "age_hours": round(age_hours, 2),
            # Present only for INTERNAL. The saga credits this account, and
            # refuses to post at all if it is missing -- see saga._legs_for.
            "credit_account_id": str(credit_account.id) if credit_account else None,
            "credit_user_id": str(credit_account.user_id) if credit_account else None,
            "credit_account_masked": credit_account.masked if credit_account else "",
        }
    # The payer's own account, so the receiving side can name who paid them
    # without account-svc having to be asked a second time.
    result["debit_account"] = {
        "id": str(account.id),
        "masked": account.masked,
        "currency": account.currency,
    }
    return result


def _internal_credit_account(beneficiary: Beneficiary) -> tuple[Account | None, str]:
    """The account an internal transfer credits, re-checked at transfer time.

    Resolution is cached on the payee, but the *state* of the destination is
    not: an account frozen or closed since the payee was added must stop the
    transfer. Returns ``(account, failure_reason)``.
    """
    account = None
    if beneficiary.internal_account_id:
        account = Account.objects.filter(pk=beneficiary.internal_account_id).first()
    else:
        # A payee added before internal resolution existed. Resolve it now and
        # backfill, rather than refusing a transfer the customer set up in good
        # faith.
        account = Account.objects.filter(
            account_number=beneficiary.account_number
        ).first()
        if account is not None:
            Beneficiary.objects.filter(pk=beneficiary.pk).update(
                internal_account_id=account.id,
                internal_user_id=account.user_id,
                resolved_at=timezone.now(),
            )

    if account is None:
        return None, "BENEFICIARY_ACCOUNT_UNRESOLVED"
    if account.status != Account.Status.ACTIVE:
        return None, f"BENEFICIARY_ACCOUNT_{account.status}"
    if account.currency != beneficiary.currency:
        return None, "BENEFICIARY_CURRENCY_MISMATCH"
    return account, ""
