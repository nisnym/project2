"""HTTP layer for account-svc."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import IsCustomer, IsService, IsStaff
from platform_common.auth.authentication import (
    JWTAuthentication,
    ServiceTokenAuthentication,
)
from platform_common.errors import NotFound, ValidationFailed

from . import services
from .models import Account, Beneficiary, LimitUsage
from .serializers import AddBeneficiarySerializer

# ---------------------------------------------------------------------------
# internal (service-to-service)
# ---------------------------------------------------------------------------


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes([IsService])
def validate_transfer(request):
    money = request.data.get("amount") or {}
    result = services.validate_transfer(
        account_id=request.data["account_id"],
        user_id=request.data["user_id"],
        beneficiary_id=request.data.get("beneficiary_id"),
        amount=Decimal(str(money.get("amount", "0"))),
        currency=str(money.get("currency", "INR")),
        rail=request.data["rail"],
    )
    return Response(result, status=200 if result.get("ok") else 200)


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes([IsService])
def release_limit(request):
    released = services.release_reservation(request.data["reservation_id"])
    return Response({"released": released})


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes([IsService])
def create_account(request):
    account = services.open_account(
        user_id=request.data["user_id"],
        currency=request.data.get("currency", "INR"),
        tier=request.data.get("tier", "STANDARD"),
        idempotency_key=request.headers.get("Idempotency-Key", ""),
    )
    return Response(
        {"account_id": str(account.id), "account_number": account.account_number,
         "ifsc": account.ifsc, "currency": account.currency, "tier": account.tier},
        status=201,
    )


# ---------------------------------------------------------------------------
# representations
# ---------------------------------------------------------------------------


def _account_json(account: Account, *, balance: dict | None = None) -> dict:
    return {
        "id": str(account.id),
        "account_number": account.account_number,
        "masked": account.masked,
        "ifsc": account.ifsc,
        "currency": account.currency,
        "account_type": account.account_type,
        "status": account.status,
        "tier": account.tier,
        "opened_at": account.opened_at.isoformat(),
        # Money crosses the wire as a string. A float would lose paise the
        # moment JavaScript touched it.
        "balance": balance or {
            "available": str(account.cached_balance),
            "held": "0.0000",
            "ledger_balance": str(account.cached_balance),
            "as_of": account.balance_as_of.isoformat() if account.balance_as_of else None,
            "stale": True,
        },
    }


def _beneficiary_json(beneficiary: Beneficiary) -> dict:
    return {
        "id": str(beneficiary.id),
        "nickname": beneficiary.nickname,
        "beneficiary_type": beneficiary.beneficiary_type,
        "masked": beneficiary.masked,
        "account_number": beneficiary.masked,   # never echo the full number back
        "bank_code": beneficiary.bank_code,
        "swift_bic": beneficiary.swift_bic,
        "country": beneficiary.country,
        "currency": beneficiary.currency,
        "status": beneficiary.status,
        "fingerprint": beneficiary.fingerprint,
        # Whether this payee points at a real account inside the bank. Only ever
        # true for INTERNAL; the receiving customer's identity is never echoed.
        "internal_verified": bool(beneficiary.internal_account_id),
        "created_at": beneficiary.created_at.isoformat(),
    }


def _amount_of(value) -> str:
    """Flatten ledger-svc's ``{"amount": ..., "currency": ...}`` to a string.

    The internal API speaks in Money objects because a cross-service amount
    without its currency is a bug waiting to happen. The customer API does not:
    the account already states its currency once, and repeating it on three
    fields would just invite the client to disagree with itself.
    """
    if isinstance(value, dict):
        return str(value.get("amount", "0"))
    return str(value if value is not None else "0")


def _live_balance(account: Account) -> dict:
    """Authoritative balance from ledger-svc, falling back to the read model.

    The cached copy is updated by an event, so it can lag. Showing a stale
    balance is acceptable; showing it *without saying so* is not, hence the
    ``stale`` flag rather than a silent fallback.
    """
    from platform_common.http import get_client

    stale = {
        "available": str(account.cached_balance),
        "held": "0.0000",
        "ledger_balance": str(account.cached_balance),
        "as_of": account.balance_as_of.isoformat() if account.balance_as_of else None,
        "stale": True,
    }
    try:
        data = get_client("ledger", scopes=("ledger:read",)).get(
            f"/internal/balances/{account.id}", timeout=2.0
        )
    except Exception:
        return stale

    return {
        "available": _amount_of(data.get("available")),
        "held": _amount_of(data.get("held")),
        "ledger_balance": _amount_of(data.get("ledger_balance")),
        "as_of": data.get("as_of"),
        # A ledger account is created lazily on first posting. Until then the
        # balance is genuinely zero, not unknown -- so it is not "stale".
        "stale": False,
    }


# ---------------------------------------------------------------------------
# customer API
# ---------------------------------------------------------------------------


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def list_accounts(request):
    accounts = Account.objects.filter(user_id=request.user.id).order_by("opened_at")
    return Response({
        "results": [_account_json(a, balance=_live_balance(a)) for a in accounts]
    })


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def account_detail(request, account_id):
    # Filtering by user_id rather than fetching then comparing: an ownership
    # check you cannot forget to write.
    account = Account.objects.filter(pk=account_id, user_id=request.user.id).first()
    if account is None:
        raise NotFound("Account not found.")

    policy = services.resolve_policy(account, "ANY")
    day = LimitUsage.objects.filter(
        account_id=account.id, window=services.day_window()
    ).first()
    month = LimitUsage.objects.filter(
        account_id=account.id, window=services.month_window()
    ).first()

    return Response({
        **_account_json(account, balance=_live_balance(account)),
        "limits": {
            "currency": policy.currency,
            "per_txn_max": str(policy.per_txn_max),
            "daily_max": str(policy.daily_max),
            "monthly_max": str(policy.monthly_max),
            "daily_count_max": policy.daily_count_max,
            "daily_used": str(day.amount_used if day else Decimal("0")),
            "daily_count_used": day.count_used if day else 0,
            "monthly_used": str(month.amount_used if month else Decimal("0")),
        },
    })


@api_view(["GET", "POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def beneficiaries(request):
    if request.method == "GET":
        query = Beneficiary.objects.filter(user_id=request.user.id)
        if request.query_params.get("status"):
            query = query.filter(status=request.query_params["status"])
        return Response({
            "results": [_beneficiary_json(b) for b in query.order_by("-created_at")[:200]]
        })

    payload = AddBeneficiarySerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    beneficiary = services.add_beneficiary(user_id=request.user.id, **payload.validated_data)
    return Response(_beneficiary_json(beneficiary), status=201)


@api_view(["GET", "DELETE"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def beneficiary_detail(request, beneficiary_id):
    beneficiary = Beneficiary.objects.filter(
        pk=beneficiary_id, user_id=request.user.id
    ).first()
    if beneficiary is None:
        raise NotFound("Beneficiary not found.")

    if request.method == "DELETE":
        # Blocked, not deleted: a removed payee still has to be explainable
        # months later when a transfer to it is investigated.
        services.block_beneficiary(beneficiary)
        return Response(_beneficiary_json(beneficiary))

    return Response(_beneficiary_json(beneficiary))


# ---------------------------------------------------------------------------
# admin API
# ---------------------------------------------------------------------------


@api_view(["GET", "PATCH"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def limit_policies(request):
    """The "configure transaction limits" administrator use case."""
    from .models import LimitPolicy

    if request.method == "GET":
        return Response({
            "results": [
                {
                    "id": str(p.id), "scope": p.scope, "scope_ref": p.scope_ref,
                    "rail": p.rail, "currency": p.currency,
                    "per_txn_max": str(p.per_txn_max), "daily_max": str(p.daily_max),
                    "monthly_max": str(p.monthly_max),
                    "daily_count_max": p.daily_count_max,
                    "version": p.version, "updated_by": p.updated_by,
                    "updated_at": p.updated_at.isoformat(),
                }
                for p in LimitPolicy.objects.order_by("scope", "scope_ref", "rail")
            ]
        })

    if getattr(request.user, "role", "") != "ADMIN":
        raise ValidationFailed("Only an administrator may change limits.")

    policy = LimitPolicy.objects.filter(pk=request.data.get("id")).first()
    if policy is None:
        raise NotFound("Limit policy not found.")

    money_fields = ("per_txn_max", "daily_max", "monthly_max")
    changed = []
    for field in (*money_fields, "daily_count_max"):
        if field not in request.data:
            continue
        raw = request.data[field]
        try:
            value = Decimal(str(raw)) if field in money_fields else int(raw)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationFailed(f"{field} is not a valid number.") from exc
        if value < 0:
            raise ValidationFailed(f"{field} cannot be negative.")
        setattr(policy, field, value)
        changed.append(field)

    if not changed:
        raise ValidationFailed("No limit fields supplied.")
    if policy.per_txn_max > policy.daily_max or policy.daily_max > policy.monthly_max:
        raise ValidationFailed(
            "Limits must satisfy per_txn_max <= daily_max <= monthly_max."
        )

    # Version bump makes the change visible to anyone caching the policy, and
    # gives the audit trail something to compare against.
    policy.version += 1
    policy.updated_by = str(request.user.id)
    policy.save(update_fields=[*changed, "version", "updated_by", "updated_at"])
    return Response({"id": str(policy.id), "version": policy.version, "changed": changed})
