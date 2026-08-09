"""Fixtures for ledger tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from ledger.models import AccountKind, Balance, Direction
from ledger.services import Leg, get_or_create_account, post_entry


@pytest.fixture(autouse=True)
def no_outbound_events(monkeypatch):
    """The ledger publishes on every posting; tests assert on balances, not on
    delivery. Delivery itself is covered by platform_common's suite."""
    monkeypatch.setattr(
        "platform_common.events.publisher._enqueue_relay", lambda outbox_id: None
    )


@pytest.fixture
def account_factory(db):
    def make(*, kind=AccountKind.CUSTOMER, currency="INR", code=None, ref=None):
        ref = ref or (uuid.uuid4() if kind == AccountKind.CUSTOMER else None)
        code = code or (
            f"CUST:{ref}" if kind == AccountKind.CUSTOMER
            else f"INTERNAL:{kind}:{uuid.uuid4().hex[:6]}"
        )
        return get_or_create_account(
            code=code, kind=kind, currency=currency, account_ref=ref
        )

    return make


@pytest.fixture
def funded_account(account_factory):
    """A customer account with money in it, created the only legitimate way --
    by posting a balanced entry against an internal clearing account."""

    def make(amount="500000.0000", currency="INR"):
        customer = account_factory(currency=currency)
        clearing = account_factory(kind=AccountKind.CLEARING, currency=currency)
        post_entry(
            entry_type="FUNDING",
            legs=[
                Leg(str(clearing.id), Direction.DEBIT, Decimal(amount), currency),
                Leg(str(customer.id), Direction.CREDIT, Decimal(amount), currency),
            ],
            idempotency_key=f"seed:{uuid.uuid4()}",
            narrative="test seed",
        )
        return customer

    return make


@pytest.fixture
def balance_of():
    def get(account):
        return Balance.objects.get(ledger_account=account)

    return get
