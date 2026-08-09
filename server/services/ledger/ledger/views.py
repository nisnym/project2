"""HTTP layer for ledger-svc: authorise, parse, delegate, respond.

Internal only -- ledger-svc has no gateway route (I1/ADR-002). Customers never
address the ledger directly; only payments-svc, holding a service token with
ledger:write scope, may move money.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import HasScope, IsService
from platform_common.auth.authentication import ServiceTokenAuthentication
from platform_common.errors import ValidationFailed

from . import services
from .models import AccountKind, Balance, JournalEntry, LedgerAccount
from .services import Leg

WRITE = [IsService, HasScope.of("ledger:write")]
READ = [IsService, HasScope.of("ledger:read", "ledger:write")]


def _money(data, field="amount"):
    block = data.get(field) or {}
    if not isinstance(block, dict) or "amount" not in block or "currency" not in block:
        raise ValidationFailed(f"{field} must be {{amount, currency}}")
    return Decimal(str(block["amount"])), str(block["currency"])


def _resolve(code: str, currency: str) -> LedgerAccount:
    """Find or lazily create the ledger account behind a code.

    Internal accounts (clearing, nostro, fees) are created on first use so a
    new rail does not need a migration; customer accounts are created when the
    account is opened, but tolerating creation here keeps the demo path simple.
    """
    account = LedgerAccount.objects.filter(code=code).first()
    if account:
        return account

    if code.startswith("CUST:"):
        ref = code.split(":", 1)[1]
        return services.get_or_create_account(
            code=code, kind=AccountKind.CUSTOMER, currency=currency, account_ref=ref
        )
    kind = AccountKind.CLEARING
    for candidate in AccountKind:
        if candidate.value in code:
            kind = candidate
            break
    return services.get_or_create_account(code=code, kind=kind, currency=currency)


def _legs(raw: list) -> list[Leg]:
    legs = []
    for item in raw or []:
        currency = str(item.get("currency"))
        account = _resolve(str(item["ledger_account_code"]), currency)
        legs.append(
            Leg(
                ledger_account_id=str(account.id),
                direction=str(item["direction"]),
                amount=Decimal(str(item["amount"])),
                currency=currency,
            )
        )
    if not legs:
        raise ValidationFailed("legs must not be empty")
    return legs


def _entry_response(entry: JournalEntry) -> dict:
    return {
        "journal_entry_id": str(entry.id),
        "reference": entry.reference,
        "entry_type": entry.entry_type,
        "posted_at": entry.posted_at.isoformat(),
        "postings": [
            {
                "ledger_account_id": str(p.ledger_account_id),
                "direction": p.direction,
                "amount": {"amount": str(p.amount), "currency": p.currency},
                "balance_after": str(p.balance_after),
            }
            for p in entry.postings.all()
        ],
    }


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes(WRITE)
def place_hold(request):
    amount, currency = _money(request.data)
    account = _resolve(f"CUST:{request.data['account_ref']}", currency)
    hold = services.place_hold(
        ledger_account_id=account.id,
        amount=amount,
        currency=currency,
        txn_ref=request.data["txn_ref"],
        idempotency_key=request.headers.get("Idempotency-Key")
        or f"hold:{request.data['txn_ref']}",
        ttl_minutes=int(request.data.get("ttl_minutes", 30)),
    )
    balance = Balance.objects.get(ledger_account_id=account.id)
    return Response(
        {
            "hold_id": str(hold.id),
            "status": hold.status,
            "expires_at": hold.expires_at.isoformat(),
            "available_after": {"amount": str(balance.available), "currency": currency},
        },
        status=201,
    )


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes(WRITE)
def capture_hold(request, hold_id):
    entry = services.capture_hold(
        hold_id,
        legs=_legs(request.data.get("legs")),
        idempotency_key=request.headers.get("Idempotency-Key") or f"capture:{hold_id}",
        entry_type=request.data.get("entry_type", "TRANSFER"),
        narrative=request.data.get("narrative", ""),
    )
    return Response(_entry_response(entry), status=201)


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes(WRITE)
def release_hold(request, hold_id):
    hold = services.release_hold(hold_id, reason=request.data.get("reason", "released"))
    return Response({"hold_id": str(hold.id), "status": hold.status})


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes(WRITE)
def post_journal_entry(request):
    entry = services.post_entry(
        entry_type=request.data.get("entry_type", "ADJUSTMENT"),
        legs=_legs(request.data.get("legs")),
        idempotency_key=request.headers.get("Idempotency-Key")
        or f"entry:{request.data.get('txn_ref')}",
        txn_ref=request.data.get("txn_ref"),
        narrative=request.data.get("narrative", ""),
    )
    return Response(_entry_response(entry), status=201)


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes(WRITE)
def reverse_journal_entry(request, entry_id):
    entry = services.reverse_entry(
        entry_id,
        reason=request.data.get("reason", "reversal"),
        idempotency_key=request.headers.get("Idempotency-Key") or f"reverse:{entry_id}",
    )
    return Response(_entry_response(entry), status=201)


@api_view(["GET"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes(READ)
def get_balance(request, account_ref):
    account = LedgerAccount.objects.filter(account_ref=account_ref).first()
    if account is None:
        return Response(
            {"account_ref": str(account_ref), "available": {"amount": "0.0000", "currency": "INR"},
             "ledger_balance": {"amount": "0.0000", "currency": "INR"},
             "held": {"amount": "0.0000", "currency": "INR"}, "exists": False}
        )
    balance = Balance.objects.get(ledger_account=account)
    money = lambda value: {"amount": str(value), "currency": account.currency}  # noqa: E731
    return Response(
        {
            "account_ref": str(account_ref),
            "available": money(balance.available),
            "ledger_balance": money(balance.ledger_balance),
            "held": money(balance.held),
            "as_of": balance.updated_at.isoformat(),
            "exists": True,
        }
    )


@api_view(["GET"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes(READ)
def trial_balance(request):
    checks = services.check_invariants()
    return Response(
        {
            "checks": [
                {"name": c.check_name, "currency": c.currency, "ok": c.ok, "detail": c.detail}
                for c in checks
            ],
            "all_ok": all(c.ok for c in checks),
        }
    )
