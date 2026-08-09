"""Outbound calls to other services. The only module that knows a peer's URL.

Every call goes through ServiceClient, which supplies the timeout, the service
token, correlation propagation, the circuit breaker, and -- most importantly --
the retry-safety rule: a non-idempotent call is never retried after a timeout
unless it carries an Idempotency-Key.

Timeouts are per-peer and deliberate:
  fraud    0.5s  -- a 50 ms budget; waiting longer helps nobody
  ledger   3.0s  -- money must not be abandoned over a slow disk
  account  2.0s  -- validation, cheap
"""

from __future__ import annotations

import logging
from decimal import Decimal

from platform_common.http import RemoteServiceError, get_client

logger = logging.getLogger(__name__)


def account_client():
    return get_client("account", timeout=2.0, scopes=("account:read", "account:write"))


def ledger_client():
    return get_client("ledger", timeout=3.0, scopes=("ledger:read", "ledger:write"))


def fraud_client():
    # One retry only, and a hard 500 ms ceiling: the whole point of the budget
    # is that a slow fraud service degrades to REVIEW rather than to a slow API.
    return get_client("fraud", timeout=0.5, retries=1, scopes=("fraud:screen",))


# ------------------------------------------------------------------ account


def validate_transfer(*, account_id, user_id, beneficiary_id, amount: Decimal,
                      currency: str, rail: str) -> dict:
    return account_client().post(
        "/internal/validate-transfer",
        json={
            "account_id": str(account_id),
            "user_id": str(user_id),
            "beneficiary_id": str(beneficiary_id) if beneficiary_id else None,
            "amount": {"amount": str(amount), "currency": currency},
            "rail": rail,
        },
    )


def release_limit(reservation_id: str) -> dict:
    return account_client().post(
        "/internal/limits/release", json={"reservation_id": reservation_id}
    )


# ------------------------------------------------------------------- ledger


def place_hold(*, account_id, amount: Decimal, currency: str, txn_ref, ttl_minutes: int = 30) -> dict:
    return ledger_client().post(
        "/internal/holds",
        json={
            "account_ref": str(account_id),
            "amount": {"amount": str(amount), "currency": currency},
            "txn_ref": str(txn_ref),
            "ttl_minutes": ttl_minutes,
        },
        # Derived from the transaction id, so a retry from any cause -- including
        # a lost response -- can never place a second hold.
        idempotency_key=f"hold:{txn_ref}",
    )


def capture_hold(*, hold_id, txn_ref, legs: list[dict], narrative: str = "",
                 entry_type: str = "TRANSFER") -> dict:
    return ledger_client().post(
        f"/internal/holds/{hold_id}/capture",
        json={"entry_type": entry_type, "narrative": narrative, "legs": legs},
        idempotency_key=f"capture:{txn_ref}",
    )


def release_hold(*, hold_id, reason: str = "released") -> dict:
    return ledger_client().post(
        f"/internal/holds/{hold_id}/release",
        json={"reason": reason},
        idempotency_key=f"release:{hold_id}",
    )


def reverse_entry(*, journal_entry_id, txn_ref, reason: str) -> dict:
    return ledger_client().post(
        f"/internal/journal-entries/{journal_entry_id}/reverse",
        json={"reason": reason},
        idempotency_key=f"reverse:{txn_ref}",
    )


def get_balance(account_id) -> dict:
    return ledger_client().get(f"/internal/balances/{account_id}")


# -------------------------------------------------------------------- fraud


def screen(payload: dict) -> dict:
    return fraud_client().post("/internal/screen", json=payload)


__all__ = [
    "validate_transfer", "release_limit",
    "place_hold", "capture_hold", "release_hold", "reverse_entry", "get_balance",
    "screen", "RemoteServiceError",
]
