"""Fixtures for fraud tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from fraud.models import ListEntry, Rule, RuleMode, RuleStat, Threshold
from fraud.services import ScreenRequest, refresh_caches


@pytest.fixture(autouse=True)
def no_outbound_events(monkeypatch):
    """Screening publishes events; these tests assert on decisions."""
    monkeypatch.setattr(
        "platform_common.events.publisher._enqueue_relay", lambda outbox_id: None
    )


@pytest.fixture(autouse=True)
def no_async_profile_update(monkeypatch):
    """The profile update is deliberately off the hot path. Tests that care
    about it call services.update_profile() directly."""
    monkeypatch.setattr("fraud.services._schedule_profile_update", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def clear_rule_cache():
    """Rules live in a process-wide TTL cache; a stale cache makes tests
    order-dependent.

    Clears the dict directly rather than calling refresh_caches(), which
    repopulates from the database -- that would force a DB connection on the
    pure DSL tests, which deliberately have none.
    """
    from fraud.services import _CACHE

    _CACHE.clear()
    yield
    _CACHE.clear()


@pytest.fixture
def thresholds(db):
    Threshold.objects.all().delete()
    threshold = Threshold.objects.create(
        allow_below=40, block_at_or_above=75, version=1, is_active=True
    )
    refresh_caches()
    return threshold


@pytest.fixture
def rule_factory(db):
    def make(*, code=None, condition=None, weight=30, hard_block=False,
             mode=RuleMode.ACTIVE, reason_code=None, category="AMOUNT"):
        code = code or f"R{uuid.uuid4().hex[:6].upper()}"
        rule = Rule.objects.create(
            code=code,
            name=code,
            condition=condition or {"fact": "amount", "op": "gt", "value": "100000"},
            weight=weight,
            hard_block=hard_block,
            mode=mode,
            reason_code=reason_code or code,
            category=category,
        )
        RuleStat.objects.create(rule=rule)
        refresh_caches()
        return rule

    return make


@pytest.fixture
def screen_request():
    def make(**overrides):
        defaults = dict(
            txn_ref=str(uuid.uuid4()),
            account_ref=str(uuid.uuid4()),
            amount=Decimal("1000.0000"),
            currency="INR",
            rail="INTERNAL",
        )
        defaults.update(overrides)
        return ScreenRequest(**defaults)

    return make


@pytest.fixture
def blacklisted(db):
    def add(fingerprint):
        ListEntry.objects.create(
            list_type=ListEntry.ListType.BLACKLIST_BENEFICIARY,
            value=fingerprint.upper(),
            reason="test",
        )
        refresh_caches()

    return add
