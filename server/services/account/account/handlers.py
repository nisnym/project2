"""Inbound event handlers. Idempotent and order-tolerant.

account-svc keeps a *read model* of each account's balance. ledger-svc remains
the only authority (I1); this is a cached copy so a list of accounts does not
need one synchronous ledger call per row, and so a customer still sees their
last known balance when ledger-svc is unreachable rather than a flat zero.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from django.db import transaction
from django.utils.dateparse import parse_datetime

from platform_common.events import subscribe
from platform_common.events.envelope import EventEnvelope

from .models import Account

logger = logging.getLogger(__name__)


@subscribe("ledger.posted")
def on_ledger_posted(env: EventEnvelope) -> None:
    """Refresh the cached balance of every customer account the entry touched.

    Ordering matters and cannot be delegated to ``guard_sequence``: that guard
    keys on the envelope's aggregate, which for this event is the journal entry
    -- a different one every time -- so it would never compare two entries
    against each other. The balance is instead advanced only by an entry posted
    *later* than the one already applied, which is the property that actually
    matters when two postings for one account arrive out of order.
    """
    posted_at = _parse(env.payload.get("posted_at"))
    if posted_at is None:
        logger.warning("ledger.posted %s has no usable posted_at", env.event_id)
        return

    for posting in env.payload.get("postings", []):
        account_ref = posting.get("account_ref")
        if not account_ref:
            continue  # an internal clearing/nostro leg; no customer to update

        try:
            balance_after = Decimal(str(posting["balance_after"]))
        except (KeyError, TypeError, ArithmeticError):
            logger.warning("posting for %s has no usable balance_after", account_ref)
            continue

        _apply(account_ref, balance_after, posted_at)


def _apply(account_ref: str, balance: Decimal, posted_at: datetime) -> None:
    with transaction.atomic():
        account = (
            Account.objects.select_for_update().filter(pk=account_ref).first()
        )
        if account is None:
            # A ledger account whose customer lives in another service's data,
            # or a test fixture. Not an error worth paging anyone over.
            logger.debug("ledger.posted for unknown account %s", account_ref)
            return

        if account.balance_as_of and account.balance_as_of >= posted_at:
            logger.debug(
                "ignoring stale balance for %s: %s <= applied %s",
                account_ref, posted_at, account.balance_as_of,
            )
            return

        account.cached_balance = balance
        account.balance_as_of = posted_at
        account.save(update_fields=["cached_balance", "balance_as_of"])


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return parse_datetime(str(value))
    except (TypeError, ValueError):
        return None
