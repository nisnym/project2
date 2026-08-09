"""Double-entry ledger. The only place in the estate where balances change.

Invariants this schema exists to protect:

  I1  Money is never created or destroyed: every JournalEntry's debits equal its
      credits, within a single currency.
  I4  A customer balance can never go negative, even under concurrent transfers.
  I5  Nothing is ever edited or deleted; a correction is a new, linked entry.

I5 is additionally enforced at the database when running on PostgreSQL:
ledger_role has UPDATE and DELETE revoked on the postings and journal tables
(scripts/init_databases.py --with-roles). SQLite has no role system, so on
SQLite that guarantee is application-level only -- Django generates no
change/delete permissions for these models and nothing in the codebase mutates
a posting. Worth knowing which of the two you are running.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models

from platform_common.db import MoneyField

# Money is stored as exact integer minor units at 4dp, never as DecimalField:
# on SQLite a DecimalField gets REAL (float) affinity and silently loses
# precision. 4dp because FX and per-unit fees need sub-minor-unit precision;
# rounding to the currency's minor unit happens only at the rail boundary
# (platform_common.money.Money.for_rail). See platform_common/db/fields.py.


class Direction(models.TextChoices):
    DEBIT = "DEBIT", "Debit"
    CREDIT = "CREDIT", "Credit"


class AccountKind(models.TextChoices):
    CUSTOMER = "CUSTOMER", "Customer"
    CLEARING = "CLEARING", "Clearing"
    SUSPENSE = "SUSPENSE", "Suspense"
    NOSTRO = "NOSTRO", "Nostro"
    FEE = "FEE", "Fee income"
    FX_POSITION = "FX_POSITION", "FX position"


class EntryType(models.TextChoices):
    TRANSFER = "TRANSFER", "Transfer"
    FUNDING = "FUNDING", "Funding"
    FEE = "FEE", "Fee"
    FX = "FX", "FX"
    REVERSAL = "REVERSAL", "Reversal"
    ADJUSTMENT = "ADJUSTMENT", "Adjustment"


class HoldStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    CAPTURED = "CAPTURED", "Captured"
    RELEASED = "RELEASED", "Released"
    EXPIRED = "EXPIRED", "Expired"


class LedgerAccount(models.Model):
    """A node in the double-entry graph.

    ``account_ref`` points at an account-svc account for CUSTOMER accounts and
    is null for the bank's own internal accounts (clearing, nostro, fees, FX).
    Internal accounts are what make funding and international transfers balance:
    money arriving from outside has to be credited *from* somewhere.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=120, unique=True)
    account_ref = models.UUIDField(null=True, blank=True, db_index=True)
    kind = models.CharField(max_length=20, choices=AccountKind.choices)
    currency = models.CharField(max_length=3)
    # Asset/expense accounts increase on the debit side; liability/income on the
    # credit side. A customer deposit is a liability of the bank, so a customer
    # account's normal side is CREDIT.
    normal_side = models.CharField(max_length=6, choices=Direction.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_account"
        constraints = [
            models.UniqueConstraint(
                fields=["account_ref", "currency"],
                condition=models.Q(account_ref__isnull=False),
                name="ledger_uniq_customer_account_currency",
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.currency})"


class Balance(models.Model):
    """Denormalised running balance, updated in the same transaction as the
    postings that move it. Reconstructable from postings at any time; a nightly
    job checks that it still agrees."""

    ledger_account = models.OneToOneField(
        LedgerAccount, on_delete=models.PROTECT, primary_key=True,
        related_name="balance",
    )
    # Settled money.
    ledger_balance = MoneyField(default=Decimal("0"))
    # Reserved by active holds; not yet moved.
    held = MoneyField(default=Decimal("0"))
    version = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ledger_balance"
        constraints = [
            # Belt and braces alongside the application-level guard: even a bug
            # in services.py cannot persist a negative hold.
            models.CheckConstraint(
                condition=models.Q(held__gte=Decimal("0")), name="ledger_held_non_negative"
            ),
        ]

    @property
    def available(self) -> Decimal:
        """What a new transfer may actually spend."""
        return self.ledger_balance - self.held

    def __str__(self) -> str:
        return f"{self.ledger_account.code}: {self.ledger_balance} (held {self.held})"


class JournalEntry(models.Model):
    """One balanced set of postings. Immutable once written."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=40, unique=True)
    entry_type = models.CharField(max_length=20, choices=EntryType.choices)
    txn_ref = models.UUIDField(db_index=True, null=True, blank=True)

    # Replay protection at the ledger itself. payments-svc may legitimately
    # retry a capture after a lost response; this makes that a no-op.
    idempotency_key = models.CharField(max_length=120, unique=True)

    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversals"
    )
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    narrative = models.CharField(max_length=255, blank=True)
    posted_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_journal_entry"
        # No change/delete permissions are ever generated; the DB role has them
        # revoked too.
        default_permissions = ("add", "view")
        ordering = ["-posted_at"]

    def __str__(self) -> str:
        return f"{self.reference} ({self.entry_type})"


class Posting(models.Model):
    """One leg of a journal entry.

    ``amount`` is always strictly positive; ``direction`` carries the sign. A
    signed amount would let a debit of -100 masquerade as a credit and quietly
    break the balance check.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    journal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, related_name="postings"
    )
    ledger_account = models.ForeignKey(
        LedgerAccount, on_delete=models.PROTECT, related_name="postings"
    )
    direction = models.CharField(max_length=6, choices=Direction.choices)
    amount = MoneyField()
    currency = models.CharField(max_length=3)
    # Point-in-time snapshot so a statement can be produced without replaying
    # the whole history.
    balance_after = MoneyField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_posting"
        default_permissions = ("add", "view")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=Decimal("0")),
                name="ledger_posting_amount_positive",
            )
        ]
        indexes = [
            models.Index(fields=["ledger_account", "-created_at"], name="ledger_posting_acct_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.direction} {self.amount} {self.currency}"


class Hold(models.Model):
    """A reservation against available balance, before the money actually moves.

    Reserve-then-capture is what stops two concurrent transfers spending the
    same funds while one of them is being fraud-screened (I4). The TTL is a
    crash safety net: if payments-svc dies between PLACE_HOLD and CAPTURE, the
    customer's money is released rather than stranded.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ledger_account = models.ForeignKey(
        LedgerAccount, on_delete=models.PROTECT, related_name="holds"
    )
    amount = MoneyField()
    currency = models.CharField(max_length=3)
    txn_ref = models.UUIDField(db_index=True)
    idempotency_key = models.CharField(max_length=120, unique=True)
    status = models.CharField(
        max_length=10, choices=HoldStatus.choices, default=HoldStatus.ACTIVE, db_index=True
    )
    expires_at = models.DateTimeField(db_index=True)
    captured_by = models.ForeignKey(
        JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="captured_holds"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ledger_hold"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=Decimal("0")), name="ledger_hold_amount_positive"
            )
        ]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="ledger_hold_expiry_idx"),
        ]

    def __str__(self) -> str:
        return f"hold {self.amount} {self.currency} [{self.status}]"


class InvariantCheck(models.Model):
    """Result of each invariant sweep, so 'we check it' is itself auditable."""

    checked_at = models.DateTimeField(auto_now_add=True)
    check_name = models.CharField(max_length=60)
    currency = models.CharField(max_length=3, blank=True)
    ok = models.BooleanField()
    detail = models.JSONField(default=dict)

    class Meta:
        db_table = "ledger_invariant_check"
        ordering = ["-checked_at"]
