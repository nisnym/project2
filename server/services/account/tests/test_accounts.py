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

    def test_new_payee_starts_in_cooling_off(self):
        """An anti-fraud control: account-takeover fraud usually strikes right
        after a new payee is added."""
        beneficiary = services.add_beneficiary(
            user_id=uuid.uuid4(), nickname="New", beneficiary_type="DOMESTIC",
            account_number="1234512345", cooling_off_hours=24,
        )
        assert beneficiary.cooling_off_until is not None


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
            account_number="9988776655", cooling_off_hours=0,
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

    def test_cooling_off_is_reported_to_fraud(self):
        """account-svc doesn't block on it; it reports it so the fraud engine
        can weigh it as one signal among several."""
        user = uuid.uuid4()
        account = services.open_account(user_id=user)
        payee = services.add_beneficiary(
            user_id=user, nickname="New", beneficiary_type="DOMESTIC",
            account_number="5544332211", cooling_off_hours=24,
        )
        result = services.validate_transfer(
            account_id=account.id, user_id=user, beneficiary_id=payee.id,
            amount=Decimal("5000"), currency="INR", rail="DOMESTIC",
        )
        assert result["ok"]
        assert result["beneficiary"]["in_cooling_off"] is True
