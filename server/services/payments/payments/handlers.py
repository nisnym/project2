"""Inbound event handlers. Idempotent and order-tolerant.

These are the async resumption points: an analyst's verdict arrives minutes or
hours after the transaction was parked, and continues the saga.
"""

from __future__ import annotations

import logging

from platform_common.events import subscribe
from platform_common.events.envelope import EventEnvelope

from . import services

logger = logging.getLogger(__name__)


@subscribe("fraud.case_approved")
def on_case_approved(env: EventEnvelope) -> None:
    """Analyst released a held transfer -> resume the saga from CAPTURE."""
    txn_ref = env.payload.get("txn_ref")
    if not txn_ref:
        return
    services.resume_after_approval(txn_ref, analyst_id=env.payload.get("analyst_id", ""))


@subscribe("fraud.case_rejected")
def on_case_rejected(env: EventEnvelope) -> None:
    """Analyst confirmed the block -> release the hold and mark BLOCKED."""
    txn_ref = env.payload.get("txn_ref")
    if not txn_ref:
        return
    services.resume_after_rejection(txn_ref, analyst_id=env.payload.get("analyst_id", ""))


@subscribe("account.frozen")
def on_account_frozen(env: EventEnvelope) -> None:
    """A frozen account must not have in-flight transfers complete."""
    from .models import Transaction, TxnStatus

    account_id = env.payload.get("account_id")
    stuck = Transaction.objects.filter(
        account_id=account_id,
        status__in=[TxnStatus.INITIATED, TxnStatus.VALIDATED, TxnStatus.RESERVED],
    )
    for txn in stuck:
        logger.warning("account %s frozen; cancelling in-flight %s", account_id, txn.reference)
        services.cancel(txn.id, user_id=txn.user_id, reason="ACCOUNT_FROZEN")
