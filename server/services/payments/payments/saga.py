"""The payment saga.

Five steps, each with a named compensating action:

    VALIDATE      account, beneficiary and limits      undo: release limit
    PLACE_HOLD    reserve funds in the ledger          undo: release hold
    SCREEN        fraud decision (sync, 50 ms)         undo: none
    CAPTURE       post the journal entry               undo: reversing entry
    DISPATCH      hand to the rail (async)             undo: reversing entry

**Reserve before screen** is deliberate. The hold is placed *before* fraud runs
so a concurrent transfer cannot spend the same funds while screening is in
flight (I4). The cost is hold churn on blocked transactions; the benefit is that
"approved but insufficient funds" cannot exist.

**REVIEW is not a failure.** When fraud returns REVIEW the hold and the limit
reservation are deliberately *retained* -- the money stays reserved while an
analyst looks at it. Only an analyst rejection or hold expiry unwinds them.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from platform_common.errors import ServiceUnavailable
from platform_common.events import EventEnvelope, publish
from platform_common.http import RemoteServiceError
from platform_common.observability.context import get_actor, get_correlation_id

from . import clients
from .models import Rail, SagaStep, Transaction, TxnStatus, TxnType

logger = logging.getLogger(__name__)


class HaltSaga(Exception):
    """A normal business outcome that ends the saga: BLOCK, REVIEW, no funds.

    Distinct from an exception, because these are expected answers rather than
    faults, and they choose their own terminal status and compensation depth.
    """

    def __init__(self, status: str, reason: str, *, compensate: bool = True,
                 detail: dict | None = None):
        self.status = status
        self.reason = reason
        self.compensate = compensate
        self.detail = detail or {}
        super().__init__(f"{status}: {reason}")


def make_reference(txn_type: str) -> str:
    prefix = "FND" if txn_type == TxnType.FUNDING else "TXN"
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(3).upper()}"


def set_status(txn: Transaction, status: str, reason: str = "") -> Transaction:
    """Advance the state machine and bump the sequence.

    The sequence is what lets downstream consumers ignore an event that would
    move their projection backwards.
    """
    txn.status = status
    txn.status_reason = reason[:120]
    txn.sequence += 1
    if status == TxnStatus.SETTLED and txn.settled_at is None:
        txn.settled_at = timezone.now()
    txn.save(update_fields=["status", "status_reason", "sequence", "settled_at", "updated_at"])
    return txn


def _step(txn: Transaction, name: str) -> SagaStep:
    return SagaStep.objects.create(transaction=txn, name=name, request={})


def _finish(step: SagaStep, status: str, response=None, error: str = "") -> None:
    step.status = status
    step.response = response or {}
    step.error = error[:2000]
    step.finished_at = timezone.now()
    step.save(update_fields=["status", "response", "error", "finished_at"])


# ---------------------------------------------------------------- saga steps


def do_validate(txn: Transaction) -> dict:
    if txn.txn_type == TxnType.FUNDING:
        # Funding credits the customer, so there is no balance or limit to check
        # on their side; the source is verified separately.
        return {"skipped": "funding"}

    try:
        result = clients.validate_transfer(
            account_id=txn.account_id, user_id=txn.user_id,
            beneficiary_id=txn.beneficiary_id, amount=txn.amount,
            currency=txn.currency, rail=txn.rail,
        )
    except RemoteServiceError as exc:
        raise HaltSaga(TxnStatus.REJECTED, exc.code, compensate=False, detail=exc.detail)

    if not result.get("ok", False):
        raise HaltSaga(
            TxnStatus.REJECTED, result.get("reason", "VALIDATION_FAILED"),
            compensate=False, detail=result,
        )

    txn.limit_reservation_id = result.get("reservation_id", "") or ""
    beneficiary = result.get("beneficiary") or {}
    txn.beneficiary_masked = beneficiary.get("masked", "")

    # Who actually receives the money, for an internal transfer. account-svc is
    # the only service that can answer this -- it owns both the payee record and
    # the account it points at -- so the answer is carried forward here rather
    # than re-derived at capture time.
    fields = ["limit_reservation_id", "beneficiary_masked"]
    if beneficiary.get("credit_account_id"):
        txn.counterparty_account_id = beneficiary["credit_account_id"]
        txn.counterparty_user_id = beneficiary.get("credit_user_id")
        txn.counterparty_masked = beneficiary.get("credit_account_masked", "")
        fields += ["counterparty_account_id", "counterparty_user_id",
                   "counterparty_masked"]

    # Carry the payee signals forward for SCREEN.
    #
    # These are facts only account-svc holds -- a fingerprint, how old the payee
    # is, where it is -- and a client cannot be trusted to supply them even if
    # it knew them. Without this the screening payload arrives with empty
    # beneficiary fields and every rule keyed on a payee silently never fires,
    # which reads exactly like a clean transaction: score 0, ALLOW.
    if beneficiary:
        context = dict(txn.context or {})
        context.update({
            "beneficiary_fingerprint": beneficiary.get("fingerprint", ""),
            "beneficiary_country": beneficiary.get("country", ""),
            "beneficiary_age_hours": beneficiary.get("age_hours", 0.0),
            "beneficiary_type": beneficiary.get("type", ""),
            # The payer's own masked number, so the recipient's copy of this
            # transfer can say who it came from.
            "debit_account_masked": (result.get("debit_account") or {}).get("masked", ""),
        })
        txn.context = context
        fields.append("context")

    txn.save(update_fields=fields)
    return result


def undo_validate(txn: Transaction) -> None:
    if txn.limit_reservation_id:
        clients.release_limit(txn.limit_reservation_id)


def do_place_hold(txn: Transaction) -> dict:
    if txn.txn_type == TxnType.FUNDING:
        # Money is arriving; there is nothing of the customer's to reserve.
        return {"skipped": "funding"}

    try:
        result = clients.place_hold(
            account_id=txn.account_id, amount=txn.amount,
            currency=txn.currency, txn_ref=txn.id,
        )
    except RemoteServiceError as exc:
        if exc.code == "INSUFFICIENT_FUNDS":
            raise HaltSaga(TxnStatus.REJECTED, "INSUFFICIENT_FUNDS", detail=exc.detail)
        raise

    txn.hold_id = result["hold_id"]
    txn.save(update_fields=["hold_id"])
    return result


def undo_place_hold(txn: Transaction) -> None:
    if txn.hold_id:
        clients.release_hold(hold_id=txn.hold_id, reason="saga compensated")


def do_screen(txn: Transaction) -> dict:
    payload = {
        "txn_ref": str(txn.id),
        "account_ref": str(txn.account_id),
        "user_ref": str(txn.user_id),
        "amount": str(txn.amount),
        "currency": txn.currency,
        "rail": txn.rail,
        "txn_type": txn.txn_type,
        "beneficiary_fingerprint": (txn.context or {}).get("beneficiary_fingerprint", ""),
        "beneficiary_country": (txn.context or {}).get("beneficiary_country", ""),
        "beneficiary_age_hours": (txn.context or {}).get("beneficiary_age_hours", 0.0),
        "device_fingerprint": (txn.context or {}).get("device_fingerprint", ""),
        "ip_country": (txn.context or {}).get("ip_country", ""),
    }

    try:
        result = clients.screen(payload)
    except (ServiceUnavailable, RemoteServiceError) as exc:
        # ADR-005: fail to REVIEW, never to ALLOW. A fraud outage becomes an
        # analyst backlog, not fraud losses. The hold stays in place.
        logger.error("fraud screening unavailable for %s: %s", txn.reference, exc)
        raise HaltSaga(
            TxnStatus.UNDER_REVIEW, "FRAUD_UNAVAILABLE", compensate=False,
            detail={"error": str(exc)},
        )

    txn.fraud_decision_id = result.get("decision_id")
    txn.fraud_score = result.get("score")
    txn.fraud_case_id = result.get("case_id")
    txn.save(update_fields=["fraud_decision_id", "fraud_score", "fraud_case_id"])

    decision = result.get("decision")
    if decision == "BLOCK":
        raise HaltSaga(TxnStatus.BLOCKED, "FRAUD_BLOCKED", detail=result)
    if decision == "REVIEW":
        # Hold and limit reservation retained on purpose: the money stays
        # reserved while an analyst decides.
        raise HaltSaga(TxnStatus.UNDER_REVIEW, "FRAUD_REVIEW", compensate=False, detail=result)
    return result


def _legs_for(txn: Transaction) -> list[dict]:
    """Which ledger accounts move, per rail. See LLD s8.4."""
    amount = {"amount": str(txn.amount), "currency": txn.currency}
    customer = f"CUST:{txn.account_id}"

    if txn.txn_type == TxnType.FUNDING:
        clearing = {
            Rail.BANK_DEBIT: "INTERNAL:CLEARING_FUNDING",
            Rail.CARD: "INTERNAL:CLEARING_CARD",
            Rail.WALLET: "INTERNAL:CLEARING_WALLET",
        }.get(txn.rail, "INTERNAL:CLEARING_FUNDING")
        return [
            {"ledger_account_code": clearing, "direction": "DEBIT", **amount},
            {"ledger_account_code": customer, "direction": "CREDIT", **amount},
        ]

    if txn.rail == Rail.INTERNAL:
        # The credit must name the *recipient's account*, resolved by account-svc
        # during VALIDATE. Using txn.beneficiary_id here -- the id of a payee row
        # in the sender's address book -- makes ledger-svc lazily create a
        # customer account nobody owns and post the money into it. Debits still
        # equal credits, so the invariant sweep stays green and the loss is
        # silent. Refusing to post is the only safe response to a missing id.
        if not txn.counterparty_account_id:
            raise HaltSaga(
                TxnStatus.REJECTED, "BENEFICIARY_ACCOUNT_UNRESOLVED",
                detail={"beneficiary_id": str(txn.beneficiary_id)},
            )
        return [
            {"ledger_account_code": customer, "direction": "DEBIT", **amount},
            {"ledger_account_code": f"CUST:{txn.counterparty_account_id}",
             "direction": "CREDIT", **amount},
        ]

    clearing = (
        "INTERNAL:CLEARING_INTL" if txn.rail == Rail.INTERNATIONAL
        else "INTERNAL:CLEARING_DOMESTIC"
    )
    return [
        {"ledger_account_code": customer, "direction": "DEBIT", **amount},
        {"ledger_account_code": clearing, "direction": "CREDIT", **amount},
    ]


def do_capture(txn: Transaction) -> dict:
    legs = _legs_for(txn)
    narrative = f"{txn.txn_type.title()} {txn.reference}"

    if txn.txn_type == TxnType.FUNDING or not txn.hold_id:
        result = clients.ledger_client().post(
            "/internal/journal-entries",
            json={"entry_type": txn.txn_type, "txn_ref": str(txn.id),
                  "narrative": narrative, "legs": legs},
            idempotency_key=f"capture:{txn.id}",
        )
    else:
        result = clients.capture_hold(
            hold_id=txn.hold_id, txn_ref=txn.id, legs=legs,
            narrative=narrative, entry_type=txn.txn_type,
        )

    txn.journal_entry_id = result["journal_entry_id"]
    txn.save(update_fields=["journal_entry_id"])

    # The money is now in the recipient's account. Give them the record of it in
    # the same breath -- an internal transfer that only the sender can see is
    # half a transfer.
    mirror_incoming_transfer(txn)
    return result


def mirror_incoming_transfer(txn: Transaction) -> Transaction | None:
    """Write the recipient's CREDIT leg of an internal transfer.

    Only for INTERNAL: on every other rail the recipient banks somewhere else,
    and inventing a row for them would be inventing a fact we do not have.

    Idempotent on ``(counterparty_user_id, "recv:<txn id>")`` so a saga replay,
    a retried capture, or a resumed review cannot pay someone twice on paper.
    """
    if txn.rail != Rail.INTERNAL or txn.txn_type != TxnType.TRANSFER:
        return None
    if not (txn.counterparty_user_id and txn.counterparty_account_id):
        logger.error("internal transfer %s captured with no counterparty", txn.reference)
        return None

    with transaction.atomic():
        mirror, created = Transaction.objects.get_or_create(
            user_id=txn.counterparty_user_id,
            idempotency_key=f"recv:{txn.id}",
            defaults={
                # Its own unique reference, but the shared transfer_ref is what
                # both customers see and quote to support.
                "reference": f"{txn.reference}-R",
                "transfer_ref": txn.transfer_ref or txn.reference,
                "account_id": txn.counterparty_account_id,
                "txn_type": TxnType.TRANSFER,
                "rail": Rail.INTERNAL,
                "direction": "CREDIT",
                "amount": txn.amount,
                "currency": txn.currency,
                "status": TxnStatus.SETTLED,
                "settled_at": timezone.now(),
                "journal_entry_id": txn.journal_entry_id,
                "counterparty_user_id": txn.user_id,
                "counterparty_account_id": txn.account_id,
                "counterparty_masked": (txn.context or {}).get("debit_account_masked", ""),
                "correlation_id": txn.correlation_id,
                "remarks": txn.remarks,
                "related_transaction": txn,
            },
        )
        if not created:
            return mirror

        txn.related_transaction = mirror
        txn.save(update_fields=["related_transaction"])

        # Addressed to the recipient, so notification-svc tells the right person.
        publish(
            EventEnvelope(
                event_type="payment.received",
                aggregate_type="transaction",
                aggregate_id=str(mirror.id),
                sequence=0,
                producer="payments",
                correlation_id=txn.correlation_id or get_correlation_id() or "",
                payload={
                    "transaction_id": str(mirror.id),
                    "reference": mirror.transfer_ref,
                    "user_id": str(mirror.user_id),
                    "account_id": str(mirror.account_id),
                    "amount": {"amount": str(mirror.amount), "currency": mirror.currency},
                    "direction": "CREDIT",
                    "status": mirror.status,
                    "from_masked": mirror.counterparty_masked,
                    "remarks": mirror.remarks,
                    "related_transaction_id": str(txn.id),
                },
            )
        )

    logger.info("mirrored %s to recipient %s", txn.reference, mirror.user_id)
    return mirror


def undo_capture(txn: Transaction) -> None:
    """Money already moved -- unwind it with a reversing entry, never an edit."""
    if txn.journal_entry_id:
        clients.reverse_entry(
            journal_entry_id=txn.journal_entry_id, txn_ref=txn.id,
            reason=txn.status_reason or "saga compensated",
        )
    _reverse_mirror(txn)


def _reverse_mirror(txn: Transaction) -> None:
    """Keep the recipient's copy honest when the sender's leg is unwound.

    The reversing journal entry takes the money back out of their account, so
    leaving their row reading SETTLED would show a credit they no longer have.
    """
    mirror = Transaction.objects.filter(related_transaction=txn).first()
    if mirror is None or mirror.status == TxnStatus.REVERSED:
        return
    with transaction.atomic():
        set_status(mirror, TxnStatus.REVERSED,
                   txn.status_reason or "sending leg reversed")
        _emit(mirror, "payment.returned",
              {"return_reason": mirror.status_reason, "funds_returned": False})


def do_dispatch(txn: Transaction) -> dict:
    """Hand to the rail.

    INTERNAL transfers have no rail: both sides are ours, so capture *is*
    settlement. That is why an internal transfer is genuinely instant and an
    international one legitimately is not.
    """
    if txn.rail == Rail.INTERNAL or txn.txn_type == TxnType.FUNDING:
        return {"settled_immediately": True}

    from django_q.tasks import async_task

    transaction.on_commit(
        lambda: async_task("payments.tasks.dispatch_to_rail", str(txn.id),
                           q_options={"save": False})
    )
    return {"queued": True}


@dataclass(frozen=True)
class Step:
    name: str
    do: callable
    undo: callable | None


STEPS: list[Step] = [
    Step("VALIDATE", do_validate, undo_validate),
    Step("PLACE_HOLD", do_place_hold, undo_place_hold),
    Step("SCREEN", do_screen, None),
    Step("CAPTURE", do_capture, undo_capture),
    Step("DISPATCH", do_dispatch, undo_capture),
]

STEP_INDEX = {step.name: index for index, step in enumerate(STEPS)}


# -------------------------------------------------------------- saga engine


def run_saga(txn: Transaction, *, from_step: str = "VALIDATE") -> Transaction:
    """Drive the transaction through the remaining steps.

    ``from_step`` lets an analyst approval resume at CAPTURE without re-running
    validation or re-placing a hold that is still in place.
    """
    start = STEP_INDEX[from_step]

    for step in STEPS[start:]:
        record = _step(txn, step.name)
        try:
            result = step.do(txn)
        except HaltSaga as halt:
            # A normal outcome, not a fault.
            _finish(record, SagaStep.Status.DONE, {"halted": halt.reason})
            if halt.compensate:
                compensate(txn, upto=step.name, reason=halt.reason)
            with transaction.atomic():
                set_status(txn, halt.status, halt.reason)
                _emit_terminal(txn, halt.detail)
            return txn
        except Exception as exc:
            logger.exception("saga step %s failed for %s", step.name, txn.reference)
            _finish(record, SagaStep.Status.FAILED, error=repr(exc))
            compensate(txn, upto=step.name, reason="STEP_ERROR")
            with transaction.atomic():
                if txn.status != TxnStatus.COMPENSATION_PENDING:
                    set_status(txn, TxnStatus.FAILED, f"{step.name}: {exc}")
                _emit_terminal(txn, {"failed_step": step.name, "error": str(exc)})
            return txn

        _finish(record, SagaStep.Status.DONE, result if isinstance(result, dict) else {})
        _advance(txn, step.name)

    return txn


def _advance(txn: Transaction, completed_step: str) -> None:
    """Move the status forward as each step completes.

    The status change and its event must commit together -- publish() asserts
    on this, because an event published outside the transaction that produced it
    is a dual write and dual writes lose events.
    """
    with transaction.atomic():
        _advance_locked(txn, completed_step)


def _advance_locked(txn: Transaction, completed_step: str) -> None:
    if completed_step == "VALIDATE":
        set_status(txn, TxnStatus.VALIDATED)
    elif completed_step == "PLACE_HOLD":
        set_status(txn, TxnStatus.RESERVED)
    elif completed_step == "SCREEN":
        set_status(txn, TxnStatus.APPROVED)
        _emit(txn, "payment.approved")
    elif completed_step == "CAPTURE":
        set_status(txn, TxnStatus.POSTED)
    elif completed_step == "DISPATCH":
        if txn.rail == Rail.INTERNAL or txn.txn_type == TxnType.FUNDING:
            set_status(txn, TxnStatus.SETTLED)
            _emit(txn, "payment.settled")
        else:
            set_status(txn, TxnStatus.DISPATCHED)
            _emit(txn, "payment.dispatched")


# A step needs undoing if it completed, or if a previous attempt to undo it
# failed. Omitting the latter would make retry_compensation a no-op and strand
# the customer's money in a reserved state forever.
NEEDS_COMPENSATION = (
    SagaStep.Status.DONE,
    SagaStep.Status.COMPENSATION_FAILED,
)


def compensate(txn: Transaction, *, upto: str, reason: str) -> bool:
    """Undo completed steps in reverse order. Returns True if all undos succeeded.

    Each undo is idempotent, so a partially-completed compensation can simply be
    re-run. If any undo fails the transaction is parked in
    COMPENSATION_PENDING and a sweeper retries it -- money is never left
    reserved because a release call happened to fail.
    """
    boundary = STEP_INDEX[upto]
    failed = False

    for step in reversed(STEPS[:boundary]):
        if step.undo is None:
            continue
        record = (
            txn.steps.filter(name=step.name, status__in=NEEDS_COMPENSATION)
            .order_by("-started_at").first()
        )
        if record is None:
            continue
        try:
            step.undo(txn)
            record.status = SagaStep.Status.COMPENSATED
            record.error = ""
            record.save(update_fields=["status", "error"])
        except Exception as exc:
            logger.exception("compensation %s failed for %s", step.name, txn.reference)
            record.status = SagaStep.Status.COMPENSATION_FAILED
            record.error = repr(exc)[:2000]
            record.save(update_fields=["status", "error"])
            failed = True

    if failed:
        with transaction.atomic():
            set_status(txn, TxnStatus.COMPENSATION_PENDING, reason)
        from django_q.tasks import async_task

        transaction.on_commit(
            lambda: async_task("payments.tasks.retry_compensation", str(txn.id),
                               q_options={"save": False})
        )
    return not failed


# ------------------------------------------------------------------- events


def _payload(txn: Transaction, extra: dict | None = None) -> dict:
    payload = {
        "transaction_id": str(txn.id),
        "reference": txn.reference,
        "user_id": str(txn.user_id),
        "account_id": str(txn.account_id),
        "txn_type": txn.txn_type,
        "rail": txn.rail,
        "amount": {"amount": str(txn.amount), "currency": txn.currency},
        "status": txn.status,
        "beneficiary_masked": txn.beneficiary_masked,
        "schedule_id": str(txn.schedule_id) if txn.schedule_id else None,
    }
    if txn.fraud_score is not None:
        payload["fraud"] = {
            "decision_id": str(txn.fraud_decision_id) if txn.fraud_decision_id else None,
            "score": txn.fraud_score,
            "case_id": str(txn.fraud_case_id) if txn.fraud_case_id else None,
        }
    if extra:
        payload.update(extra)
    return payload


def _emit(txn: Transaction, event_type: str, extra: dict | None = None) -> None:
    publish(
        EventEnvelope(
            event_type=event_type,
            aggregate_type="transaction",
            aggregate_id=str(txn.id),
            sequence=txn.sequence,
            producer="payments",
            correlation_id=txn.correlation_id or get_correlation_id() or "",
            actor=get_actor(),
            payload=_payload(txn, extra),
        )
    )


TERMINAL_EVENT = {
    TxnStatus.BLOCKED: "payment.blocked",
    TxnStatus.REJECTED: "payment.failed",
    TxnStatus.UNDER_REVIEW: "payment.review_required",
    TxnStatus.FAILED: "payment.failed",
    TxnStatus.CANCELLED: "payment.cancelled",
    TxnStatus.EXPIRED: "payment.failed",
    TxnStatus.COMPENSATION_PENDING: "payment.failed",
}


def _emit_terminal(txn: Transaction, detail: dict | None = None) -> None:
    event_type = TERMINAL_EVENT.get(txn.status)
    if event_type:
        _emit(txn, event_type, {"reason": txn.status_reason, "detail": detail or {}})
