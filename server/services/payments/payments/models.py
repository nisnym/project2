"""Payment orders and the saga that drives them.

Funding and transfers are one model with a `txn_type` (ADR-003): they share a
lifecycle, a state machine, an idempotency store and a saga engine, so splitting
them would duplicate all four.

The saga is *explicit state in the database*, not control flow in a function.
Recovery reads `SagaStep`, not the transaction status -- after a crash we need
to know which steps completed so we know which ones to compensate.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models

from platform_common.db import MoneyField


class TxnType(models.TextChoices):
    FUNDING = "FUNDING", "Funding"
    TRANSFER = "TRANSFER", "Transfer"


class Rail(models.TextChoices):
    INTERNAL = "INTERNAL", "Internal (same bank)"
    DOMESTIC = "DOMESTIC", "Domestic"
    INTERNATIONAL = "INTERNATIONAL", "International"
    BANK_DEBIT = "BANK_DEBIT", "External bank debit"
    CARD = "CARD", "Debit card"
    WALLET = "WALLET", "Wallet"


class TxnStatus(models.TextChoices):
    INITIATED = "INITIATED", "Initiated"
    VALIDATED = "VALIDATED", "Validated"
    RESERVED = "RESERVED", "Funds reserved"
    SCREENING = "SCREENING", "Screening"
    APPROVED = "APPROVED", "Approved"
    UNDER_REVIEW = "UNDER_REVIEW", "Under review"
    POSTED = "POSTED", "Posted to ledger"
    DISPATCHED = "DISPATCHED", "Dispatched to rail"
    SETTLED = "SETTLED", "Settled"
    RETURNED = "RETURNED", "Returned by rail"
    REVERSED = "REVERSED", "Reversed"
    BLOCKED = "BLOCKED", "Blocked"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"
    EXPIRED = "EXPIRED", "Expired"
    FAILED = "FAILED", "Failed"
    COMPENSATION_PENDING = "COMPENSATION_PENDING", "Compensation pending"


# Terminal states never transition again.
TERMINAL_STATUSES = frozenset({
    TxnStatus.SETTLED, TxnStatus.BLOCKED, TxnStatus.REJECTED,
    TxnStatus.CANCELLED, TxnStatus.EXPIRED, TxnStatus.REVERSED,
})

# A transfer can only be cancelled before the instruction leaves the building.
CANCELLABLE_STATUSES = frozenset({
    TxnStatus.INITIATED, TxnStatus.VALIDATED, TxnStatus.RESERVED,
    TxnStatus.UNDER_REVIEW,
})


class Transaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=40, unique=True)

    user_id = models.UUIDField(db_index=True)
    account_id = models.UUIDField(db_index=True)

    txn_type = models.CharField(max_length=10, choices=TxnType.choices)
    rail = models.CharField(max_length=20, choices=Rail.choices)
    direction = models.CharField(max_length=6)  # DEBIT for transfers, CREDIT for funding

    amount = MoneyField()
    currency = models.CharField(max_length=3)
    fx_rate = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)
    dest_amount = MoneyField(null=True, blank=True)
    dest_currency = models.CharField(max_length=3, blank=True)

    beneficiary_id = models.UUIDField(null=True, blank=True)
    beneficiary_masked = models.CharField(max_length=40, blank=True)
    funding_source_id = models.UUIDField(null=True, blank=True)

    # ---- the other side of the transfer --------------------------------
    #
    # A transfer inside the bank has two owners, and each of them is entitled to
    # a record of it in their own history. The sender's row is the DEBIT leg;
    # the recipient gets a mirrored CREDIT row created at capture. Both carry
    # the same ``transfer_ref`` and point at each other, so "I sent it" and
    # "they received it" are provably the same event rather than two stories.
    #
    # ``beneficiary_id`` cannot serve this purpose: it identifies a payee record
    # in the *sender's* address book, which the recipient has never heard of.
    counterparty_user_id = models.UUIDField(null=True, blank=True, db_index=True)
    counterparty_account_id = models.UUIDField(null=True, blank=True)
    counterparty_masked = models.CharField(max_length=40, blank=True)
    related_transaction = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="mirror_of"
    )
    # The shared business reference shown on both sides. The sender's own
    # ``reference`` is the value; the mirror borrows it while keeping a distinct
    # ``reference`` of its own to satisfy the unique constraint.
    transfer_ref = models.CharField(max_length=40, blank=True, db_index=True)

    status = models.CharField(
        max_length=24, choices=TxnStatus.choices, default=TxnStatus.INITIATED, db_index=True
    )
    status_reason = models.CharField(max_length=120, blank=True)

    # References into other services. UUIDs, never foreign keys -- a FK across a
    # service boundary is a merged service pretending not to be.
    hold_id = models.UUIDField(null=True, blank=True)
    journal_entry_id = models.UUIDField(null=True, blank=True)
    fraud_decision_id = models.UUIDField(null=True, blank=True)
    fraud_case_id = models.UUIDField(null=True, blank=True)
    fraud_score = models.IntegerField(null=True, blank=True)
    limit_reservation_id = models.CharField(max_length=64, blank=True)
    rail_ref = models.CharField(max_length=60, blank=True)

    schedule_id = models.UUIDField(null=True, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=120)
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    # Bumped on every transition; consumers use it to ignore stale events.
    sequence = models.PositiveIntegerField(default=0)

    purpose_code = models.CharField(max_length=40, blank=True)
    remarks = models.CharField(max_length=140, blank=True)
    context = models.JSONField(default=dict)  # device fingerprint, ip country

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "payments_transaction"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user_id", "idempotency_key"], name="payments_uniq_idem_per_user"
            )
        ]
        indexes = [
            models.Index(fields=["account_id", "-created_at"], name="payments_acct_hist_idx"),
            models.Index(fields=["status", "created_at"], name="payments_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.reference} {self.amount} {self.currency} [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_cancellable(self) -> bool:
        return self.status in CANCELLABLE_STATUSES


class SagaStep(models.Model):
    """One step of the saga, with its compensation state.

    Exists so that recovery is data-driven: a sweeper can look at a stuck
    transaction and know exactly which steps completed and therefore which need
    undoing, without re-deriving it from the status.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"
        COMPENSATED = "COMPENSATED", "Compensated"
        COMPENSATION_FAILED = "COMPENSATION_FAILED", "Compensation failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction = models.ForeignKey(
        Transaction, on_delete=models.CASCADE, related_name="steps"
    )
    name = models.CharField(max_length=30)
    status = models.CharField(max_length=22, choices=Status.choices, default=Status.PENDING)
    attempt = models.PositiveIntegerField(default=1)
    request = models.JSONField(default=dict)
    response = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "payments_saga_step"
        ordering = ["started_at"]
        indexes = [models.Index(fields=["transaction", "name"], name="payments_step_idx")]

    def __str__(self) -> str:
        return f"{self.name} [{self.status}]"


class IdempotencyRecord(models.Model):
    """Stores the original response so a retry replays it byte for byte.

    Required on every money-mutating POST. A missing key is a 400, not a
    courtesy default -- this is the control that makes a double-click harmless.
    """

    class State(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETE = "COMPLETE", "Complete"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.UUIDField()
    key = models.CharField(max_length=120)
    endpoint = models.CharField(max_length=80)
    # Same key with a different body is a client bug, and returning the original
    # response would hide it. 409 instead.
    request_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=12, choices=State.choices, default=State.IN_PROGRESS)
    status_code = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    transaction_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "payments_idempotency"
        constraints = [
            models.UniqueConstraint(
                fields=["user_id", "key", "endpoint"], name="payments_uniq_idem_record"
            )
        ]


class TransferSchedule(models.Model):
    """Future-dated and recurring transfers.

    One Q2 schedule sweeps this table every minute; there is deliberately not a
    django_q Schedule row per customer standing order.
    """

    class Frequency(models.TextChoices):
        ONCE = "ONCE", "Once"
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.UUIDField(db_index=True)
    account_id = models.UUIDField()
    beneficiary_id = models.UUIDField()

    amount = MoneyField()
    currency = models.CharField(max_length=3)
    rail = models.CharField(max_length=20, choices=Rail.choices)
    remarks = models.CharField(max_length=140, blank=True)

    frequency = models.CharField(max_length=10, choices=Frequency.choices)
    next_run_at = models.DateTimeField(db_index=True)
    end_date = models.DateField(null=True, blank=True)
    max_runs = models.PositiveIntegerField(null=True, blank=True)
    runs_completed = models.PositiveIntegerField(default=0)
    consecutive_failures = models.PositiveIntegerField(default=0)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments_transfer_schedule"
        indexes = [
            models.Index(fields=["status", "next_run_at"], name="payments_sched_due_idx"),
        ]


class FundingSource(models.Model):
    """Where money is funded *from*. Never stores a PAN -- only a vault token."""

    class SourceType(models.TextChoices):
        EXTERNAL_BANK = "EXTERNAL_BANK", "External bank account"
        DEBIT_CARD = "DEBIT_CARD", "Debit card"
        WALLET = "WALLET", "Wallet"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.UUIDField(db_index=True)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    display_name = models.CharField(max_length=60)  # "HDFC ****4821"
    token = models.CharField(max_length=200)        # PSP/vault token, never the PAN
    currency = models.CharField(max_length=3, default="INR")
    verified = models.BooleanField(default=False)
    verification_method = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments_funding_source"

    def __str__(self) -> str:
        return f"{self.display_name} ({self.source_type})"
