"""Accounts, beneficiaries and limit reservations."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from account import services
from account.models import Account, Beneficiary, LimitReservation, LimitUsage

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_outbound_events(monkeypatch):
    monkeypatch.setattr(
        "platform_common.events.publisher._enqueue_relay", lambda outbox_id: None
    )


class TestAccountOpening:
    def test_generates_a_luhn_valid_number(self):
        account = services.open_account(user_id=uuid.uuid4())
        digits = [int(d) for d in account.account_number]
        total, parity = 0, len(digits) % 2
        for index, digit in enumerate(digits):
            if index % 2 == parity:
                digit *= 2
                if digit > 9:
                    digit -= 9
            total += digit
        assert total % 10 == 0, "check digit is wrong"

    def test_account_numbers_are_unique(self):
        numbers = {services.open_account(user_id=uuid.uuid4()).account_number
                   for _ in range(25)}
        assert len(numbers) == 25

    def test_is_idempotent_with_a_key(self):
        """A dropped connection tells you nothing about whether the server
        acted. Without this, a retry opens a second account."""
        user = uuid.uuid4()
        first = services.open_account(user_id=user, idempotency_key="onboarding:app-1")
        second = services.open_account(user_id=user, idempotency_key="onboarding:app-1")

        assert first.id == second.id
        assert Account.objects.filter(user_id=user).count() == 1

    def test_different_keys_open_different_accounts(self):
        user = uuid.uuid4()
        services.open_account(user_id=user, idempotency_key="onboarding:app-1")
        services.open_account(user_id=user, idempotency_key="onboarding:app-2")
        assert Account.objects.filter(user_id=user).count() == 2

    def test_without_a_key_every_call_opens_an_account(self):
        """Explicit: the guarantee comes from the key, not from the user id."""
        user = uuid.uuid4()
        services.open_account(user_id=user)
        services.open_account(user_id=user)
        assert Account.objects.filter(user_id=user).count() == 2


class TestBeneficiaries:
    def test_fingerprint_is_stable_and_type_sensitive(self):
        make = Beneficiary.make_fingerprint
        assert make("DOMESTIC", "123", "HDFC") == make("DOMESTIC", "123", "HDFC")
        assert make("DOMESTIC", "123", "HDFC") != make("INTERNATIONAL", "123", "HDFC")

    def test_adding_the_same_payee_twice_is_idempotent(self):
        user = uuid.uuid4()
        for _ in range(2):
            services.add_beneficiary(
                user_id=user, nickname="Ravi", beneficiary_type="DOMESTIC",
                account_number="9988776655", bank_code="HDFC0001",
            )
        assert Beneficiary.objects.filter(user_id=user).count() == 1

    def test_a_new_payee_is_usable_immediately(self):
        """No cooling-off window. Payee novelty is a fraud signal that screening
        weighs per transfer, not a blanket delay imposed on the customer."""
        beneficiary = services.add_beneficiary(
            user_id=uuid.uuid4(), nickname="New", beneficiary_type="DOMESTIC",
            account_number="1234512345", bank_code="HDFC0001234",
        )
        assert beneficiary.status == Beneficiary.Status.ACTIVE
        assert not hasattr(beneficiary, "cooling_off_until")


class TestLimits:
    def _account(self, tier="STANDARD"):
        return services.open_account(user_id=uuid.uuid4(), tier=tier)

    def test_per_transaction_cap_is_enforced(self):
        account = self._account()
        ok, reason, _ = services.check_and_reserve(
            account=account, rail="DOMESTIC", amount=Decimal("999999"), currency="INR"
        )
        assert not ok and reason == "PER_TXN_EXCEEDED"

    def test_a_reservation_consumes_daily_budget(self):
        account = self._account()
        services.check_and_reserve(account=account, rail="DOMESTIC",
                                   amount=Decimal("100000"), currency="INR")
        usage = LimitUsage.objects.get(account_id=account.id, window__startswith="DAY")
        assert usage.amount_used == Decimal("100000.0000")
        assert usage.count_used == 1

    def test_reservations_accumulate_until_the_cap(self):
        """Reserving rather than reading is what stops two transfers from
        jointly breaching a limit each passes individually."""
        account = self._account()   # STANDARD: daily_max 500000
        for _ in range(5):
            ok, _, _ = services.check_and_reserve(
                account=account, rail="DOMESTIC", amount=Decimal("100000"), currency="INR"
            )
            assert ok
        ok, reason, _ = services.check_and_reserve(
            account=account, rail="DOMESTIC", amount=Decimal("1"), currency="INR"
        )
        assert not ok and reason == "DAILY_EXCEEDED"

    def test_releasing_returns_the_budget(self):
        account = self._account()
        ok, _, detail = services.check_and_reserve(
            account=account, rail="DOMESTIC", amount=Decimal("100000"), currency="INR"
        )
        assert services.release_reservation(detail["reservation_id"]) is True

        usage = LimitUsage.objects.get(account_id=account.id, window__startswith="DAY")
        assert usage.amount_used == Decimal("0.0000")
        assert usage.count_used == 0

    def test_release_is_idempotent(self):
        account = self._account()
        _, _, detail = services.check_and_reserve(
            account=account, rail="DOMESTIC", amount=Decimal("100000"), currency="INR"
        )
        services.release_reservation(detail["reservation_id"])
        assert services.release_reservation(detail["reservation_id"]) is False
        assert LimitUsage.objects.get(
            account_id=account.id, window__startswith="DAY"
        ).amount_used == Decimal("0.0000")

    def test_tier_governs_the_cap(self):
        basic = self._account("BASIC")        # per_txn 50000
        premium = self._account("PREMIUM")    # per_txn 1000000
        assert not services.check_and_reserve(
            account=basic, rail="DOMESTIC", amount=Decimal("60000"), currency="INR")[0]
        assert services.check_and_reserve(
            account=premium, rail="DOMESTIC", amount=Decimal("60000"), currency="INR")[0]


class TestValidateTransfer:
    def test_a_valid_transfer_passes_and_reserves(self):
        user = uuid.uuid4()
        account = services.open_account(user_id=user)
        payee = services.add_beneficiary(
            user_id=user, nickname="Ravi", beneficiary_type="DOMESTIC",
            account_number="9988776655", bank_code="HDFC0001234",
        )
        result = services.validate_transfer(
            account_id=account.id, user_id=user, beneficiary_id=payee.id,
            amount=Decimal("5000"), currency="INR", rail="DOMESTIC",
        )
        assert result["ok"]
        assert LimitReservation.objects.filter(pk=result["reservation_id"]).exists()
        assert result["beneficiary"]["fingerprint"] == payee.fingerprint

    def test_a_frozen_account_is_refused(self):
        user = uuid.uuid4()
        account = services.open_account(user_id=user)
        Account.objects.filter(pk=account.pk).update(status=Account.Status.FROZEN)
        account.refresh_from_db()
        result = services.validate_transfer(
            account_id=account.id, user_id=user, beneficiary_id=None,
            amount=Decimal("100"), currency="INR", rail="DOMESTIC",
        )
        assert not result["ok"] and result["reason"] == "ACCOUNT_FROZEN"

    def test_another_users_account_is_not_found(self):
        account = services.open_account(user_id=uuid.uuid4())
        result = services.validate_transfer(
            account_id=account.id, user_id=uuid.uuid4(), beneficiary_id=None,
            amount=Decimal("100"), currency="INR", rail="DOMESTIC",
        )
        assert not result["ok"] and result["reason"] == "ACCOUNT_NOT_FOUND"

    def test_payee_age_is_reported_to_fraud(self):
        """account-svc doesn't block on payee novelty; it reports how old the
        payee is so the fraud engine can weigh it as one signal among several."""
        user = uuid.uuid4()
        account = services.open_account(user_id=user)
        payee = services.add_beneficiary(
            user_id=user, nickname="New", beneficiary_type="DOMESTIC",
            account_number="5544332211", bank_code="HDFC0001234",
        )
        result = services.validate_transfer(
            account_id=account.id, user_id=user, beneficiary_id=payee.id,
            amount=Decimal("5000"), currency="INR", rail="DOMESTIC",
        )
        assert result["ok"]
        assert result["beneficiary"]["age_hours"] < 1
        assert "in_cooling_off" not in result["beneficiary"]


class TestInternalPayeeResolution:
    """An INTERNAL payee must point at a real account inside this bank.

    Before resolution existed, the payee's account number was never checked
    against anything, and the payments saga went on to tell the ledger to credit
    `CUST:<beneficiary_id>` -- the id of the payee row itself. The ledger created
    that account on demand, the entry balanced, and the recipient never saw a
    paisa.
    """

    @pytest.fixture
    def recipient(self):
        return services.open_account(user_id=uuid.uuid4())

    def test_an_internal_payee_resolves_to_the_real_account(self, recipient):
        payee = services.add_beneficiary(
            user_id=uuid.uuid4(), nickname="Ravi",
            beneficiary_type=Beneficiary.Type.INTERNAL,
            account_number=recipient.account_number,
        )

        assert payee.internal_account_id == recipient.id
        assert payee.internal_user_id == recipient.user_id
        assert payee.resolved_at is not None

    def test_an_unknown_account_number_is_refused(self):
        from platform_common.errors import ValidationFailed

        with pytest.raises(ValidationFailed) as caught:
            services.add_beneficiary(
                user_id=uuid.uuid4(), nickname="Nobody",
                beneficiary_type=Beneficiary.Type.INTERNAL,
                account_number="5021999999999",
            )
        assert caught.value.code == "INTERNAL_PAYEE_UNRESOLVED"

    def test_a_closed_account_is_indistinguishable_from_a_missing_one(self):
        """Telling the two apart, over a range of numbers, is an account
        enumeration oracle."""
        from platform_common.errors import ValidationFailed

        closed = services.open_account(user_id=uuid.uuid4())
        closed.status = Account.Status.CLOSED
        closed.save(update_fields=["status"])

        with pytest.raises(ValidationFailed) as caught:
            services.add_beneficiary(
                user_id=uuid.uuid4(), nickname="Gone",
                beneficiary_type=Beneficiary.Type.INTERNAL,
                account_number=closed.account_number,
            )
        assert caught.value.code == "INTERNAL_PAYEE_UNRESOLVED"

    def test_your_own_account_is_not_a_payee(self, recipient):
        from platform_common.errors import ValidationFailed

        with pytest.raises(ValidationFailed) as caught:
            services.add_beneficiary(
                user_id=recipient.user_id, nickname="Me",
                beneficiary_type=Beneficiary.Type.INTERNAL,
                account_number=recipient.account_number,
            )
        assert caught.value.code == "SELF_PAYEE"

    def test_external_payees_are_not_resolved(self):
        payee = services.add_beneficiary(
            user_id=uuid.uuid4(), nickname="Ravi at HDFC",
            beneficiary_type=Beneficiary.Type.DOMESTIC,
            account_number="9988776655", bank_code="HDFC0001234",
        )
        assert payee.internal_account_id is None


class TestValidateTransferNamesTheCreditAccount:
    """The saga refuses to post an internal transfer without this, so it is the
    single most load-bearing field in the validation response."""

    @pytest.fixture
    def pair(self):
        sender = services.open_account(user_id=uuid.uuid4())
        recipient = services.open_account(user_id=uuid.uuid4())
        payee = services.add_beneficiary(
            user_id=sender.user_id, nickname="Ravi",
            beneficiary_type=Beneficiary.Type.INTERNAL,
            account_number=recipient.account_number,
        )
        return sender, recipient, payee

    def test_returns_the_recipients_account_and_owner(self, pair):
        sender, recipient, payee = pair
        result = services.validate_transfer(
            account_id=sender.id, user_id=sender.user_id, beneficiary_id=payee.id,
            amount=Decimal("5000"), currency="INR", rail="INTERNAL",
        )

        assert result["ok"] is True
        assert result["beneficiary"]["credit_account_id"] == str(recipient.id)
        assert result["beneficiary"]["credit_user_id"] == str(recipient.user_id)
        assert result["beneficiary"]["credit_account_masked"] == recipient.masked
        # And the payer's own number, so the recipient's copy can name them.
        assert result["debit_account"]["masked"] == sender.masked

    def test_a_frozen_destination_stops_the_transfer(self, pair):
        sender, recipient, payee = pair
        recipient.status = Account.Status.FROZEN
        recipient.save(update_fields=["status"])

        result = services.validate_transfer(
            account_id=sender.id, user_id=sender.user_id, beneficiary_id=payee.id,
            amount=Decimal("5000"), currency="INR", rail="INTERNAL",
        )
        assert result == {"ok": False, "reason": "BENEFICIARY_ACCOUNT_FROZEN"}

    def test_a_frozen_destination_does_not_consume_limit_budget(self, pair):
        """The rejection happens before the reservation, so a customer whose
        payee is frozen does not silently burn their daily allowance."""
        sender, recipient, payee = pair
        recipient.status = Account.Status.FROZEN
        recipient.save(update_fields=["status"])

        services.validate_transfer(
            account_id=sender.id, user_id=sender.user_id, beneficiary_id=payee.id,
            amount=Decimal("5000"), currency="INR", rail="INTERNAL",
        )
        assert LimitReservation.objects.count() == 0

    def test_a_legacy_payee_is_resolved_and_backfilled(self, pair):
        """Payees added before resolution existed must not have their transfers
        refused -- they were set up in good faith."""
        sender, recipient, payee = pair
        Beneficiary.objects.filter(pk=payee.pk).update(
            internal_account_id=None, internal_user_id=None, resolved_at=None
        )

        result = services.validate_transfer(
            account_id=sender.id, user_id=sender.user_id, beneficiary_id=payee.id,
            amount=Decimal("5000"), currency="INR", rail="INTERNAL",
        )

        assert result["beneficiary"]["credit_account_id"] == str(recipient.id)
        payee.refresh_from_db()
        assert payee.internal_account_id == recipient.id

    def test_an_external_rail_names_no_credit_account(self):
        sender = services.open_account(user_id=uuid.uuid4())
        payee = services.add_beneficiary(
            user_id=sender.user_id, nickname="Ravi at HDFC",
            beneficiary_type=Beneficiary.Type.DOMESTIC,
            account_number="9988776655", bank_code="HDFC0001234",
        )
        result = services.validate_transfer(
            account_id=sender.id, user_id=sender.user_id, beneficiary_id=payee.id,
            amount=Decimal("5000"), currency="INR", rail="DOMESTIC",
        )
        assert result["beneficiary"]["credit_account_id"] is None


class TestBalanceProjection:
    """The cached balance is a read model fed by ledger.posted.

    It existed as a field, was documented as "updated by an event", and nothing
    ever wrote it -- because account/handlers.py was an empty stub while
    `ledger.posted` was routed here. Every account therefore reported a cached
    balance of zero, which only stayed invisible because the API falls back to a
    live ledger call. The moment ledger-svc was unreachable, every customer saw
    a flat zero instead of their last known balance.
    """

    @staticmethod
    def _posted(account, balance, *, posted_at, other_leg=True):
        from platform_common.events.envelope import EventEnvelope

        postings = [{
            "ledger_account_id": str(uuid.uuid4()),
            "account_ref": str(account.id),
            "direction": "CREDIT",
            "amount": {"amount": "100.0000", "currency": "INR"},
            "balance_after": balance,
        }]
        if other_leg:
            # An internal clearing leg: no account_ref, must be skipped rather
            # than crash the handler.
            postings.append({
                "ledger_account_id": str(uuid.uuid4()),
                "account_ref": None,
                "direction": "DEBIT",
                "amount": {"amount": "100.0000", "currency": "INR"},
                "balance_after": "-100.0000",
            })
        return EventEnvelope(
            event_type="ledger.posted", aggregate_type="journal_entry",
            aggregate_id=str(uuid.uuid4()), sequence=0, producer="ledger",
            correlation_id="corr-1",
            payload={"posted_at": posted_at.isoformat(), "postings": postings},
        )

    def test_a_posting_updates_the_cached_balance(self):
        from django.utils import timezone

        from account.handlers import on_ledger_posted

        account = services.open_account(user_id=uuid.uuid4())
        now = timezone.now()

        on_ledger_posted(self._posted(account, "5000.0000", posted_at=now))

        account.refresh_from_db()
        assert account.cached_balance == Decimal("5000.0000")
        assert account.balance_as_of == now

    def test_a_later_posting_wins(self):
        from datetime import timedelta

        from django.utils import timezone

        from account.handlers import on_ledger_posted

        account = services.open_account(user_id=uuid.uuid4())
        first = timezone.now()

        on_ledger_posted(self._posted(account, "5000.0000", posted_at=first))
        on_ledger_posted(
            self._posted(account, "7500.0000", posted_at=first + timedelta(seconds=5))
        )

        account.refresh_from_db()
        assert account.cached_balance == Decimal("7500.0000")

    def test_a_late_arriving_older_posting_is_ignored(self):
        """Parallel workers mean postings for one account can arrive out of
        order. A stale one must not walk the balance backwards."""
        from datetime import timedelta

        from django.utils import timezone

        from account.handlers import on_ledger_posted

        account = services.open_account(user_id=uuid.uuid4())
        now = timezone.now()

        on_ledger_posted(self._posted(account, "7500.0000", posted_at=now))
        on_ledger_posted(
            self._posted(account, "5000.0000", posted_at=now - timedelta(seconds=5))
        )

        account.refresh_from_db()
        assert account.cached_balance == Decimal("7500.0000")

    def test_redelivery_is_harmless(self):
        from django.utils import timezone

        from account.handlers import on_ledger_posted

        account = services.open_account(user_id=uuid.uuid4())
        event = self._posted(account, "5000.0000", posted_at=timezone.now())

        on_ledger_posted(event)
        on_ledger_posted(event)

        account.refresh_from_db()
        assert account.cached_balance == Decimal("5000.0000")

    def test_an_unknown_account_ref_is_skipped_quietly(self):
        from django.utils import timezone

        from account.handlers import on_ledger_posted

        ghost = services.open_account(user_id=uuid.uuid4())
        event = self._posted(ghost, "5000.0000", posted_at=timezone.now())
        Account.objects.filter(pk=ghost.pk).delete()

        on_ledger_posted(event)   # must not raise


class TestBlockingAPayee:
    def test_blocking_publishes_an_event(self):
        """The clearest signal of an account takeover is the customer blocking a
        payee they did not add. This path used to change the status silently."""
        from platform_common.models import OutboxEvent

        payee = services.add_beneficiary(
            user_id=uuid.uuid4(), nickname="Ravi", beneficiary_type="DOMESTIC",
            account_number="9988776655", bank_code="HDFC0001234",
        )
        services.block_beneficiary(payee)

        payee.refresh_from_db()
        assert payee.status == Beneficiary.Status.BLOCKED
        assert OutboxEvent.objects.filter(event_type="beneficiary.blocked").exists()

    def test_blocking_twice_publishes_once(self):
        from platform_common.models import OutboxEvent

        payee = services.add_beneficiary(
            user_id=uuid.uuid4(), nickname="Ravi", beneficiary_type="DOMESTIC",
            account_number="9988776655", bank_code="HDFC0001234",
        )
        services.block_beneficiary(payee)
        services.block_beneficiary(payee)

        assert OutboxEvent.objects.filter(
            event_type="beneficiary.blocked"
        ).values("event_id").distinct().count() == 1
