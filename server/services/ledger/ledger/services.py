"""Ledger domain logic -- the most safety-critical module in the system.

Every function that touches a balance follows the same three rules:

  1. Validate *before* opening a transaction, so an invalid entry costs nothing.
  2. Lock every affected balance in ascending primary-key order. This is the
     only reliable deadlock prevention when two transfers touch the same pair of
     accounts in opposite directions at the same moment.
  3. Check idempotency inside the transaction, so a concurrent duplicate loses
     the race on a unique constraint rather than double-posting.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from platform_common.errors import Conflict, DomainError, InsufficientFunds, NotFound
from platform_common.events import EventEnvelope, publish
from platform_common.observability.context import get_correlation_id

from .models import (
    AccountKind,
    Balance,
    Direction,
    EntryType,
    Hold,
    HoldStatus,
    JournalEntry,
    LedgerAccount,
    Posting,
)

logger = logging.getLogger(__name__)

DEFAULT_HOLD_TTL_MINUTES = 30


class UnbalancedEntry(DomainError):
    status_code = 422
    default_code = "UNBALANCED_ENTRY"
    default_detail = "Debits and credits do not balance."


class InvalidPosting(DomainError):
    status_code = 422
    default_code = "INVALID_POSTING"


class HoldNotActive(Conflict):
    default_code = "HOLD_NOT_ACTIVE"


@dataclass(frozen=True)
class Leg:
    """One side of a journal entry, as supplied by a caller."""

    ledger_account_id: str
    direction: str
    amount: Decimal
    currency: str


def _reference(prefix: str = "JE") -> str:
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(4).upper()}"


# ---------------------------------------------------------------- accounts


def get_or_create_account(
    *, code: str, kind: str, currency: str, account_ref=None, normal_side=None
) -> LedgerAccount:
    if normal_side is None:
        # A customer deposit is a liability of the bank: it increases on the
        # credit side. Internal clearing/nostro/FX accounts are asset-like.
        normal_side = (
            Direction.CREDIT if kind == AccountKind.CUSTOMER else Direction.DEBIT
        )
    with transaction.atomic():
        account, created = LedgerAccount.objects.get_or_create(
            code=code,
            defaults={
                "kind": kind,
                "currency": currency,
                "account_ref": account_ref,
                "normal_side": normal_side,
            },
        )
        if created:
            Balance.objects.create(ledger_account=account)
    return account


def customer_account(account_ref, currency: str) -> LedgerAccount:
    account = LedgerAccount.objects.filter(
        account_ref=account_ref, currency=currency
    ).first()
    if account is None:
        raise NotFound(
            f"no ledger account for {account_ref} in {currency}",
            detail={"account_ref": str(account_ref), "currency": currency},
        )
    return account


# ----------------------------------------------------------------- posting


def _validate(legs: list[Leg]) -> tuple[Decimal, str]:
    if len(legs) < 2:
        raise InvalidPosting("a journal entry needs at least two legs")

    for leg in legs:
        if leg.amount is None or Decimal(leg.amount) <= 0:
            raise InvalidPosting(
                "postings must be strictly positive; direction carries the sign",
                detail={"amount": str(leg.amount)},
            )
        if leg.direction not in (Direction.DEBIT, Direction.CREDIT):
            raise InvalidPosting(f"unknown direction {leg.direction!r}")

    currencies = {leg.currency for leg in legs}
    if len(currencies) > 1:
        # Cross-currency must go through explicit FX_POSITION legs so that every
        # entry balances *within* a currency; otherwise the daily invariant
        # check is meaningless.
        raise InvalidPosting(
            "an entry cannot mix currencies; route FX through FX_POSITION accounts",
            detail={"currencies": sorted(currencies)},
        )

    debits = sum((Decimal(l.amount) for l in legs if l.direction == Direction.DEBIT), Decimal("0"))
    credits = sum((Decimal(l.amount) for l in legs if l.direction == Direction.CREDIT), Decimal("0"))
    if debits != credits:
        raise UnbalancedEntry(
            f"debits {debits} != credits {credits}",
            detail={"debits": str(debits), "credits": str(credits)},
        )
    if debits == 0:
        raise InvalidPosting("an entry must move a non-zero amount")

    return debits, currencies.pop()


def _signed_delta(balance: Balance, leg: Leg) -> Decimal:
    """How this leg moves the account's balance.

    A posting on the account's normal side increases it; the opposite side
    decreases it. Customer accounts are CREDIT-normal, so a credit increases the
    customer's money and a debit decreases it -- which is what a customer
    expects to see.
    """
    amount = Decimal(leg.amount)
    return amount if leg.direction == balance.ledger_account.normal_side else -amount


def post_entry(
    *,
    entry_type: str,
    legs: list[Leg],
    idempotency_key: str,
    txn_ref=None,
    narrative: str = "",
    reverses: JournalEntry | None = None,
    correlation_id: str | None = None,
    allow_negative: bool = False,
) -> JournalEntry:
    """Write one balanced journal entry and move the balances.

    ``allow_negative`` exists only for reversals: unwinding an entry can
    legitimately push an internal clearing account negative, and refusing would
    strand a customer's refund.
    """
    total, currency = _validate(legs)
    correlation_id = correlation_id or get_correlation_id() or ""

    with transaction.atomic():
        existing = JournalEntry.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            logger.info("journal entry %s replayed, returning original", idempotency_key)
            return existing

        # Lock in ascending account id. Two transfers A->B and B->A running
        # concurrently would deadlock under any other ordering.
        account_ids = sorted({str(leg.ledger_account_id) for leg in legs})
        balances = {
            str(balance.ledger_account_id): balance
            for balance in Balance.objects.select_for_update()
            .select_related("ledger_account")
            .filter(ledger_account_id__in=account_ids)
            .order_by("ledger_account_id")
        }
        missing = set(account_ids) - set(balances)
        if missing:
            raise NotFound(f"unknown ledger account(s): {sorted(missing)}")

        for leg in legs:
            account = balances[str(leg.ledger_account_id)].ledger_account
            if account.currency != leg.currency:
                raise InvalidPosting(
                    f"account {account.code} is {account.currency}, leg is {leg.currency}"
                )
            if not account.is_active:
                raise InvalidPosting(f"ledger account {account.code} is inactive")

        entry = JournalEntry.objects.create(
            reference=_reference(),
            entry_type=entry_type,
            txn_ref=txn_ref,
            idempotency_key=idempotency_key,
            reverses=reverses,
            correlation_id=correlation_id,
            narrative=narrative[:255],
            posted_at=timezone.now(),
        )

        postings = []
        for leg in legs:
            balance = balances[str(leg.ledger_account_id)]
            balance.ledger_balance += _signed_delta(balance, leg)

            if (
                not allow_negative
                and balance.ledger_account.kind == AccountKind.CUSTOMER
                and balance.ledger_balance < 0
            ):
                # Rolls back the whole entry -- a partially applied journal entry
                # would break I1.
                raise InsufficientFunds(
                    "posting would overdraw a customer account",
                    detail={
                        "ledger_account": balance.ledger_account.code,
                        "shortfall": str(-balance.ledger_balance),
                        "currency": leg.currency,
                    },
                )

            balance.version += 1
            balance.save(update_fields=["ledger_balance", "version", "updated_at"])

            postings.append(
                Posting(
                    journal_entry=entry,
                    ledger_account_id=leg.ledger_account_id,
                    direction=leg.direction,
                    amount=Decimal(leg.amount),
                    currency=leg.currency,
                    balance_after=balance.ledger_balance,
                )
            )

        Posting.objects.bulk_create(postings)

        publish(
            EventEnvelope(
                event_type="ledger.posted",
                aggregate_type="journal_entry",
                aggregate_id=str(entry.id),
                sequence=0,
                producer="ledger",
                correlation_id=correlation_id,
                payload={
                    "journal_entry_id": str(entry.id),
                    "reference": entry.reference,
                    "entry_type": entry.entry_type,
                    "txn_ref": str(entry.txn_ref) if entry.txn_ref else None,
                    "posted_at": entry.posted_at.isoformat(),
                    "total": {"amount": str(total), "currency": currency},
                    "postings": [
                        {
                            "ledger_account_id": str(p.ledger_account_id),
                            "account_ref": str(balances[str(p.ledger_account_id)].ledger_account.account_ref)
                            if balances[str(p.ledger_account_id)].ledger_account.account_ref
                            else None,
                            "direction": p.direction,
                            "amount": {"amount": str(p.amount), "currency": p.currency},
                            "balance_after": str(p.balance_after),
                        }
                        for p in postings
                    ],
                },
            )
        )

    logger.info("posted %s %s %s", entry.reference, total, currency)
    return entry


def reverse_entry(entry_id, *, reason: str, idempotency_key: str) -> JournalEntry:
    """Unwind an entry by posting its mirror image.

    Never an edit and never a delete: the original stays, and the correction is
    a new linked entry (I5). That is what makes the history reconstructable.
    """
    original = (
        JournalEntry.objects.filter(pk=entry_id).prefetch_related("postings").first()
    )
    if original is None:
        raise NotFound(f"journal entry {entry_id} not found")

    existing = original.reversals.first()
    if existing:
        return existing

    flip = {Direction.DEBIT: Direction.CREDIT, Direction.CREDIT: Direction.DEBIT}
    mirrored = [
        Leg(
            ledger_account_id=str(p.ledger_account_id),
            direction=flip[p.direction],
            amount=p.amount,
            currency=p.currency,
        )
        for p in original.postings.all()
    ]

    entry = post_entry(
        entry_type=EntryType.REVERSAL,
        legs=mirrored,
        idempotency_key=idempotency_key,
        txn_ref=original.txn_ref,
        narrative=f"Reversal of {original.reference}: {reason}"[:255],
        reverses=original,
        correlation_id=original.correlation_id,
        # A reversal must always be possible; the money is being put back.
        allow_negative=True,
    )

    with transaction.atomic():
        publish(
            EventEnvelope(
                event_type="ledger.reversed",
                aggregate_type="journal_entry",
                aggregate_id=str(entry.id),
                sequence=0,
                producer="ledger",
                correlation_id=entry.correlation_id,
                payload={
                    "journal_entry_id": str(entry.id),
                    "reverses_id": str(original.id),
                    "reverses_reference": original.reference,
                    "txn_ref": str(original.txn_ref) if original.txn_ref else None,
                    "reason": reason,
                },
            )
        )
    return entry


# ------------------------------------------------------------------- holds


def place_hold(
    *,
    ledger_account_id,
    amount: Decimal,
    currency: str,
    txn_ref,
    idempotency_key: str,
    ttl_minutes: int = DEFAULT_HOLD_TTL_MINUTES,
) -> Hold:
    amount = Decimal(amount)
    if amount <= 0:
        raise InvalidPosting("hold amount must be positive")

    with transaction.atomic():
        existing = Hold.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

        balance = (
            Balance.objects.select_for_update()
            .select_related("ledger_account")
            .filter(ledger_account_id=ledger_account_id)
            .first()
        )
        if balance is None:
            raise NotFound(f"unknown ledger account {ledger_account_id}")
        if balance.ledger_account.currency != currency:
            raise InvalidPosting(
                f"account is {balance.ledger_account.currency}, hold is {currency}"
            )

        if balance.available < amount:
            raise InsufficientFunds(
                "available balance is lower than the requested amount",
                detail={
                    "available": str(balance.available),
                    "requested": str(amount),
                    "currency": currency,
                },
            )

        balance.held += amount
        balance.version += 1
        balance.save(update_fields=["held", "version", "updated_at"])

        try:
            hold = Hold.objects.create(
                ledger_account_id=ledger_account_id,
                amount=amount,
                currency=currency,
                txn_ref=txn_ref,
                idempotency_key=idempotency_key,
                expires_at=timezone.now() + timedelta(minutes=ttl_minutes),
            )
        except IntegrityError:
            # Concurrent duplicate lost the race; return the winner's hold and
            # let this transaction's held increment roll back.
            raise Conflict("a hold with this idempotency key already exists")

    return hold


def capture_hold(hold_id, *, legs: list[Leg], idempotency_key: str,
                 entry_type: str = EntryType.TRANSFER, narrative: str = "") -> JournalEntry:
    """Convert a reservation into an actual movement of money."""
    with transaction.atomic():
        hold = Hold.objects.select_for_update().filter(pk=hold_id).first()
        if hold is None:
            raise NotFound(f"hold {hold_id} not found")
        if hold.status == HoldStatus.CAPTURED:
            return hold.captured_by
        if hold.status != HoldStatus.ACTIVE:
            raise HoldNotActive(
                f"hold is {hold.status}", detail={"status": hold.status}
            )

        balance = (
            Balance.objects.select_for_update()
            .filter(ledger_account_id=hold.ledger_account_id)
            .first()
        )
        # Release the reservation first, then move the money. Doing it the other
        # way round would briefly double-count the amount and could trip the
        # overdraft guard on a customer's own funds.
        balance.held -= hold.amount
        balance.version += 1
        balance.save(update_fields=["held", "version", "updated_at"])

        entry = post_entry(
            entry_type=entry_type,
            legs=legs,
            idempotency_key=idempotency_key,
            txn_ref=hold.txn_ref,
            narrative=narrative,
        )

        hold.status = HoldStatus.CAPTURED
        hold.captured_by = entry
        hold.resolved_at = timezone.now()
        hold.save(update_fields=["status", "captured_by", "resolved_at"])

    return entry


def release_hold(hold_id, *, reason: str = "released") -> Hold:
    """Return reserved funds without moving any money. Idempotent."""
    with transaction.atomic():
        hold = Hold.objects.select_for_update().filter(pk=hold_id).first()
        if hold is None:
            raise NotFound(f"hold {hold_id} not found")
        if hold.status in (HoldStatus.RELEASED, HoldStatus.EXPIRED):
            return hold
        if hold.status == HoldStatus.CAPTURED:
            raise HoldNotActive(
                "hold was already captured; reverse the journal entry instead",
                detail={"journal_entry_id": str(hold.captured_by_id)},
            )

        balance = (
            Balance.objects.select_for_update()
            .filter(ledger_account_id=hold.ledger_account_id)
            .first()
        )
        balance.held -= hold.amount
        balance.version += 1
        balance.save(update_fields=["held", "version", "updated_at"])

        hold.status = (
            HoldStatus.EXPIRED if reason == "expired" else HoldStatus.RELEASED
        )
        hold.resolved_at = timezone.now()
        hold.save(update_fields=["status", "resolved_at"])

    return hold


def expire_holds(limit: int = 500) -> dict:
    """Safety net: release holds whose saga died mid-flight.

    Without this, a payments-svc crash between PLACE_HOLD and CAPTURE would
    strand the customer's money indefinitely.
    """
    due = list(
        Hold.objects.filter(status=HoldStatus.ACTIVE, expires_at__lte=timezone.now())
        .values_list("id", flat=True)[:limit]
    )
    released = 0
    for hold_id in due:
        try:
            release_hold(hold_id, reason="expired")
            released += 1
        except Exception:
            logger.exception("could not expire hold %s", hold_id)
    if released:
        logger.warning("expired %s stale hold(s)", released)
    return {"found": len(due), "expired": released}


# -------------------------------------------------------------- invariants


def check_invariants() -> list:
    """I1 and the balance/postings agreement, per currency.

    A failure here is an existential event for a bank, so it publishes a
    dedicated event that pages ops rather than merely logging.
    """
    from .models import InvariantCheck

    results = []
    currencies = LedgerAccount.objects.values_list("currency", flat=True).distinct()

    for currency in currencies:
        debits = Posting.objects.filter(
            currency=currency, direction=Direction.DEBIT
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        credits = Posting.objects.filter(
            currency=currency, direction=Direction.CREDIT
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")

        ok = debits == credits
        check = InvariantCheck.objects.create(
            check_name="DEBITS_EQUAL_CREDITS",
            currency=currency,
            ok=ok,
            detail={
                "debits": str(debits),
                "credits": str(credits),
                "delta": str(debits - credits),
            },
        )
        results.append(check)

        if not ok:
            logger.critical(
                "LEDGER INVARIANT BREACHED for %s: debits %s != credits %s",
                currency, debits, credits,
            )
            with transaction.atomic():
                publish(
                    EventEnvelope(
                        event_type="ledger.invariant_breached",
                        aggregate_type="ledger",
                        aggregate_id=currency,
                        sequence=0,
                        producer="ledger",
                        correlation_id=get_correlation_id() or "",
                        payload={
                            "check": "DEBITS_EQUAL_CREDITS",
                            "currency": currency,
                            **check.detail,
                        },
                    )
                )

    # Every balance must equal the sum of its own postings.
    drifted = []
    for balance in Balance.objects.select_related("ledger_account").iterator():
        account = balance.ledger_account
        aggregate = Posting.objects.filter(ledger_account=account).aggregate(
            debits=Sum("amount", filter=models_q(Direction.DEBIT)),
            credits=Sum("amount", filter=models_q(Direction.CREDIT)),
        )
        debits = aggregate["debits"] or Decimal("0")
        credits = aggregate["credits"] or Decimal("0")
        expected = (
            credits - debits if account.normal_side == Direction.CREDIT else debits - credits
        )
        if expected != balance.ledger_balance:
            drifted.append(
                {
                    "account": account.code,
                    "balance": str(balance.ledger_balance),
                    "from_postings": str(expected),
                }
            )

    results.append(
        InvariantCheck.objects.create(
            check_name="BALANCE_MATCHES_POSTINGS",
            ok=not drifted,
            detail={"drifted": drifted},
        )
    )
    if drifted:
        logger.critical("balances disagree with postings: %s", drifted)

    return results


def models_q(direction: str):
    from django.db.models import Q

    return Q(direction=direction)
