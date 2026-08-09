"""The payment saga: every branch, and every compensation.

The compensation paths are the ones that matter. A saga that works when
everything succeeds is easy; the reason this is the highest-risk service is what
happens when step 4 of 5 fails after money has already been reserved.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from payments import services
from payments.models import SagaStep, Transaction, TxnStatus, TxnType
from payments.saga import run_saga
from platform_common.errors import IllegalStateTransition, NotFound

pytestmark = pytest.mark.django_db


class TestHappyPath:
    def test_domestic_transfer_reaches_dispatched(self, peers, transfer):
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.DISPATCHED
        assert txn.hold_id and txn.journal_entry_id
        assert peers.names() == [
            "validate_transfer", "place_hold", "screen", "capture_hold"
        ]

    def test_internal_transfer_settles_immediately(self, peers, transfer):
        """Both sides are ours, so capture *is* settlement. This is why an
        internal transfer is genuinely instant."""
        txn = services.submit(transfer(rail="INTERNAL"))
        assert txn.status == TxnStatus.SETTLED
        assert txn.settled_at is not None

    def test_steps_run_in_order_and_are_recorded(self, peers, transfer):
        txn = services.submit(transfer())
        names = list(txn.steps.order_by("started_at").values_list("name", flat=True))
        assert names == ["VALIDATE", "PLACE_HOLD", "SCREEN", "CAPTURE", "DISPATCH"]
        assert all(s == SagaStep.Status.DONE
                   for s in txn.steps.values_list("status", flat=True))

    def test_hold_is_placed_before_screening(self, peers, transfer):
        """Reserve-then-screen: a concurrent transfer must not be able to spend
        the same funds while screening is in flight."""
        services.submit(transfer())
        assert peers.names().index("place_hold") < peers.names().index("screen")

    def test_sequence_advances_on_every_transition(self, peers, transfer):
        txn = services.submit(transfer())
        assert txn.sequence >= 4   # VALIDATED, RESERVED, APPROVED, POSTED, DISPATCHED


class TestFraudOutcomes:
    def test_block_releases_everything(self, peers, transfer):
        peers.fraud_decision = "BLOCK"
        txn = services.submit(transfer())

        assert txn.status == TxnStatus.BLOCKED
        # Both compensations ran, in reverse order.
        assert peers.count("release_hold") == 1
        assert peers.count("release_limit") == 1
        assert peers.count("capture_hold") == 0   # money never moved

    def test_review_retains_the_hold(self, peers, transfer):
        """REVIEW is not a failure: the money stays reserved while an analyst
        looks at it. Releasing here would let the customer spend it first."""
        peers.fraud_decision = "REVIEW"
        txn = services.submit(transfer())

        assert txn.status == TxnStatus.UNDER_REVIEW
        assert txn.hold_id is not None
        assert peers.count("release_hold") == 0
        assert peers.count("release_limit") == 0

    def test_fraud_unavailable_fails_to_review_not_allow(self, peers, transfer):
        """ADR-005. A fraud outage becomes an analyst backlog, never losses."""
        peers.fraud_available = False
        txn = services.submit(transfer())

        assert txn.status == TxnStatus.UNDER_REVIEW
        assert txn.status_reason == "FRAUD_UNAVAILABLE"
        assert peers.count("capture_hold") == 0   # crucially, NOT approved
        assert txn.hold_id is not None            # funds still reserved

    def test_analyst_approval_resumes_from_capture(self, peers, transfer):
        peers.fraud_decision = "REVIEW"
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.UNDER_REVIEW

        resumed = services.resume_after_approval(txn.id, analyst_id="analyst-1")

        assert resumed.status == TxnStatus.DISPATCHED
        # Resumed from CAPTURE: no second validation, no second hold.
        assert peers.count("validate_transfer") == 1
        assert peers.count("place_hold") == 1
        assert peers.count("capture_hold") == 1

    def test_analyst_rejection_releases_the_hold(self, peers, transfer):
        peers.fraud_decision = "REVIEW"
        txn = services.submit(transfer())

        rejected = services.resume_after_rejection(txn.id, analyst_id="analyst-1")

        assert rejected.status == TxnStatus.BLOCKED
        assert peers.count("release_hold") == 1
        assert peers.count("capture_hold") == 0

    def test_duplicate_approval_is_ignored(self, peers, transfer):
        """Events are at-least-once; a redelivered approval must not capture twice."""
        peers.fraud_decision = "REVIEW"
        txn = services.submit(transfer())
        services.resume_after_approval(txn.id, analyst_id="a")
        services.resume_after_approval(txn.id, analyst_id="a")
        assert peers.count("capture_hold") == 1

    def test_approval_after_rejection_is_ignored(self, peers, transfer):
        """Out-of-order delivery must not resurrect a blocked transfer."""
        peers.fraud_decision = "REVIEW"
        txn = services.submit(transfer())
        services.resume_after_rejection(txn.id, analyst_id="a")
        result = services.resume_after_approval(txn.id, analyst_id="b")
        assert result.status == TxnStatus.BLOCKED
        assert peers.count("capture_hold") == 0


class TestValidationAndFunds:
    def test_limit_breach_rejects_without_placing_a_hold(self, peers, transfer):
        peers.validate_ok = False
        txn = services.submit(transfer())

        assert txn.status == TxnStatus.REJECTED
        assert txn.status_reason == "LIMIT_EXCEEDED"
        assert peers.count("place_hold") == 0   # nothing to compensate

    def test_insufficient_funds_releases_the_limit_reservation(self, peers, transfer):
        peers.sufficient_funds = False
        txn = services.submit(transfer())

        assert txn.status == TxnStatus.REJECTED
        assert txn.status_reason == "INSUFFICIENT_FUNDS"
        assert peers.count("release_limit") == 1   # validate is undone
        assert peers.count("screen") == 0          # never got that far


class TestCompensation:
    def test_capture_failure_unwinds_hold_and_limit(self, peers, transfer):
        """The dangerous case: money reserved, then the ledger goes down."""
        peers.capture_fails = True
        txn = services.submit(transfer())

        assert txn.status == TxnStatus.FAILED
        assert peers.count("release_hold") == 1
        assert peers.count("release_limit") == 1

    def test_failed_compensation_parks_for_retry(self, peers, transfer):
        """Money must never stay reserved because a release call failed."""
        peers.capture_fails = True
        peers.release_hold_fails = True
        txn = services.submit(transfer())

        assert txn.status == TxnStatus.COMPENSATION_PENDING
        step = txn.steps.get(name="PLACE_HOLD")
        assert step.status == SagaStep.Status.COMPENSATION_FAILED

    def test_retry_task_drains_pending_compensation(self, peers, transfer):
        from payments.tasks import retry_compensation

        peers.capture_fails = True
        peers.release_hold_fails = True
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.COMPENSATION_PENDING

        peers.release_hold_fails = False          # ledger recovers
        retry_compensation(str(txn.id))

        txn.refresh_from_db()
        assert txn.status != TxnStatus.COMPENSATION_PENDING
        assert peers.count("release_hold") == 2   # failed once, then succeeded


class TestRailOutcomes:
    def test_settlement_completes_the_transfer(self, peers, transfer):
        txn = services.submit(transfer())
        settled = services.settle_from_rail(txn.id, rail_ref="NEFT-123")
        assert settled.status == TxnStatus.SETTLED
        assert settled.rail_ref == "NEFT-123"

    def test_return_posts_a_reversing_entry(self, peers, transfer):
        """Never an edit, never a delete -- a new linked entry (I5, I8)."""
        txn = services.submit(transfer())
        returned = services.return_from_rail(txn.id, reason="BENEFICIARY_ACCOUNT_CLOSED")

        assert returned.status == TxnStatus.REVERSED
        assert peers.count("reverse_entry") == 1

    def test_return_is_idempotent(self, peers, transfer):
        txn = services.submit(transfer())
        services.return_from_rail(txn.id, reason="X")
        services.return_from_rail(txn.id, reason="X")
        assert peers.count("reverse_entry") == 1

    def test_settlement_of_an_unknown_transaction_is_safe(self, peers):
        assert services.settle_from_rail(uuid.uuid4()) is None


class TestCancellation:
    def test_cancel_before_dispatch_releases_the_hold(self, peers, transfer):
        peers.fraud_decision = "REVIEW"
        txn = services.submit(transfer())

        cancelled = services.cancel(txn.id, user_id=txn.user_id)

        assert cancelled.status == TxnStatus.CANCELLED
        assert peers.count("release_hold") == 1

    def test_cannot_cancel_once_dispatched(self, peers, transfer):
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.DISPATCHED
        with pytest.raises(IllegalStateTransition, match="no longer be cancelled"):
            services.cancel(txn.id, user_id=txn.user_id)

    def test_another_user_cannot_cancel_your_transfer(self, peers, transfer):
        """404 not 403: confirming existence would leak that the id is real."""
        peers.fraud_decision = "REVIEW"
        txn = services.submit(transfer())
        with pytest.raises(NotFound):
            services.cancel(txn.id, user_id=uuid.uuid4())


class TestFunding:
    def test_funding_credits_without_a_hold(self, peers):
        """Money is arriving; there is nothing of the customer's to reserve."""
        txn = services.create_funding(
            user_id=uuid.uuid4(), account_id=uuid.uuid4(),
            funding_source_id=uuid.uuid4(), amount=Decimal("50000"),
            currency="INR", rail="BANK_DEBIT", idempotency_key=str(uuid.uuid4()),
        )
        txn = services.submit(txn)

        assert txn.status == TxnStatus.SETTLED
        assert peers.count("place_hold") == 0
        assert peers.count("post_journal_entry") == 1

    def test_funding_is_screened_too(self, peers):
        """A stolen card funding an account before an immediate outbound
        transfer is a classic pattern; screening only the outbound leg misses it."""
        txn = services.create_funding(
            user_id=uuid.uuid4(), account_id=uuid.uuid4(),
            funding_source_id=uuid.uuid4(), amount=Decimal("50000"),
            currency="INR", rail="CARD", idempotency_key=str(uuid.uuid4()),
        )
        services.submit(txn)
        assert peers.count("screen") == 1

    def test_blocked_funding_moves_no_money(self, peers):
        peers.fraud_decision = "BLOCK"
        txn = services.create_funding(
            user_id=uuid.uuid4(), account_id=uuid.uuid4(),
            funding_source_id=uuid.uuid4(), amount=Decimal("50000"),
            currency="INR", rail="CARD", idempotency_key=str(uuid.uuid4()),
        )
        txn = services.submit(txn)
        assert txn.status == TxnStatus.BLOCKED
        assert peers.count("post_journal_entry") == 0


class TestValidationRules:
    def test_zero_amount_is_rejected(self, peers):
        from platform_common.errors import ValidationFailed

        with pytest.raises(ValidationFailed):
            services.create_transfer(
                user_id=uuid.uuid4(), account_id=uuid.uuid4(),
                beneficiary_id=uuid.uuid4(), amount=Decimal("0"),
                currency="INR", rail="DOMESTIC", idempotency_key=str(uuid.uuid4()),
            )

    def test_negative_amount_is_rejected(self, peers):
        from platform_common.errors import ValidationFailed

        with pytest.raises(ValidationFailed):
            services.create_transfer(
                user_id=uuid.uuid4(), account_id=uuid.uuid4(),
                beneficiary_id=uuid.uuid4(), amount=Decimal("-100"),
                currency="INR", rail="DOMESTIC", idempotency_key=str(uuid.uuid4()),
            )


@pytest.mark.django_db(transaction=True)
class TestTransactionBoundaries:
    """Regression guard for a bug the ordinary tests could not catch.

    pytest-django's default `django_db` fixture wraps each test in a
    transaction, so `in_atomic_block` is always True and publish()'s
    "outside a transaction" assertion can never fire. Every saga path below
    therefore ran green while emitting events outside a transaction -- a dual
    write that loses events in production.

    transaction=True removes the wrapper, so these exercise the real boundaries.
    """

    def test_happy_path_emits_inside_transactions(self, peers, transfer):
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.DISPATCHED

    def test_blocked_path_emits_inside_transactions(self, peers, transfer):
        peers.fraud_decision = "BLOCK"
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.BLOCKED

    def test_review_path_emits_inside_transactions(self, peers, transfer):
        peers.fraud_decision = "REVIEW"
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.UNDER_REVIEW

    def test_rejected_path_emits_inside_transactions(self, peers, transfer):
        peers.validate_ok = False
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.REJECTED

    def test_failure_path_emits_inside_transactions(self, peers, transfer):
        peers.capture_fails = True
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.FAILED

    def test_compensation_pending_path(self, peers, transfer):
        peers.capture_fails = True
        peers.release_hold_fails = True
        txn = services.submit(transfer())
        assert txn.status == TxnStatus.COMPENSATION_PENDING

    def test_settlement_and_return_emit_inside_transactions(self, peers, transfer):
        txn = services.submit(transfer())
        services.settle_from_rail(txn.id, rail_ref="NEFT-1")
        other = services.submit(transfer())
        services.return_from_rail(other.id, reason="CLOSED")
        assert Transaction.objects.get(pk=other.id).status == TxnStatus.REVERSED

    def test_funding_path_emits_inside_transactions(self, peers):
        txn = services.create_funding(
            user_id=uuid.uuid4(), account_id=uuid.uuid4(),
            funding_source_id=uuid.uuid4(), amount=Decimal("50000"),
            currency="INR", rail="BANK_DEBIT", idempotency_key=str(uuid.uuid4()),
        )
        assert services.submit(txn).status == TxnStatus.SETTLED


class TestScreeningPayload:
    """The fraud engine only knows what the saga tells it.

    These exist because of a real bug: the saga read the payee's fingerprint,
    age and cooling-off status out of `txn.context` -- where only a caller could
    have put them -- while account-svc was already returning them from VALIDATE
    and the saga was discarding them. Every payment made through the public API
    therefore reached screening with empty payee fields, so every rule keyed on
    a payee never fired and the transaction scored 0 and was ALLOWed. It looked
    exactly like a clean transfer.
    """

    def _screen_payload(self, peers):
        # The fake records the payload flattened into kwargs, so the recorded
        # kwargs dict *is* the payload.
        for name, kwargs in peers.calls:
            if name == "screen":
                return kwargs
        raise AssertionError("screen was never called")

    def test_payee_signals_reach_the_fraud_engine(self, peers, transfer):
        peers.beneficiary_fingerprint = "sha256:abc123"
        peers.beneficiary_country = "DE"
        peers.beneficiary_age_hours = 0.5
        peers.beneficiary_in_cooling_off = True

        services.submit(transfer())
        payload = self._screen_payload(peers)

        assert payload["beneficiary_fingerprint"] == "sha256:abc123"
        assert payload["beneficiary_country"] == "DE"
        assert payload["beneficiary_age_hours"] == 0.5
        assert payload["beneficiary_in_cooling_off"] is True

    def test_signals_come_from_account_svc_not_from_the_caller(self, peers, transfer):
        """A client must not be able to talk its way past a rule.

        The caller-supplied context is overwritten by what account-svc reports,
        so claiming a brand-new payee is a year old changes nothing.
        """
        peers.beneficiary_in_cooling_off = True
        peers.beneficiary_age_hours = 0.25

        txn = transfer()
        txn.context = {
            "beneficiary_in_cooling_off": False,     # a lie
            "beneficiary_age_hours": 9000.0,         # also a lie
            "device_fingerprint": "web:real-device",
        }
        txn.save(update_fields=["context"])

        services.submit(txn)
        payload = self._screen_payload(peers)

        assert payload["beneficiary_in_cooling_off"] is True
        assert payload["beneficiary_age_hours"] == 0.25
        # Genuinely client-side signals still survive.
        assert payload["device_fingerprint"] == "web:real-device"

    def test_client_context_is_preserved_for_device_and_ip(self, peers, transfer):
        txn = transfer()
        txn.context = {"device_fingerprint": "web:abc", "ip_country": "SG"}
        txn.save(update_fields=["context"])

        services.submit(txn)
        payload = self._screen_payload(peers)

        assert payload["device_fingerprint"] == "web:abc"
        assert payload["ip_country"] == "SG"

    def test_funding_has_no_payee_and_still_screens(self, peers):
        """Funding skips VALIDATE, so it must not depend on payee signals."""
        from decimal import Decimal

        txn = services.create_funding(
            user_id=uuid.uuid4(), account_id=uuid.uuid4(),
            funding_source_id=uuid.uuid4(), amount=Decimal("5000"),
            currency="INR", rail="BANK_DEBIT", idempotency_key=str(uuid.uuid4()),
        )
        services.submit(txn)
        payload = self._screen_payload(peers)

        assert payload["txn_type"] == "FUNDING"
        assert payload["beneficiary_fingerprint"] == ""
