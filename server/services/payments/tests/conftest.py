"""Fixtures for payments tests.

The peers (account, ledger, fraud) are faked at the `clients` boundary. That is
the right seam: it exercises the whole saga including compensation, without
needing three servers running, and it lets a test make any peer fail on demand.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from platform_common.errors import ServiceUnavailable
from platform_common.http import RemoteServiceError


class FakePeers:
    """Stand-in for account-svc, ledger-svc and fraud-svc.

    Every call is recorded so a test can assert not just the outcome but that
    the *right compensations ran* -- which is the part that is easy to get wrong.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        # knobs
        self.validate_ok = True
        self.validate_reason = "LIMIT_EXCEEDED"
        self.sufficient_funds = True
        self.fraud_decision = "ALLOW"
        self.fraud_score = 10
        self.fraud_available = True
        self.capture_fails = False
        self.release_hold_fails = False
        self.hold_id = None
        self.journal_entry_id = None
        # Payee signals. account-svc is the only holder of these, and the saga
        # must carry them into the screening payload -- so the fake returns
        # them exactly as the real service does. An earlier version of this
        # fixture omitted them, and the omission hid a bug where every
        # payee-dependent fraud rule silently never fired.
        self.beneficiary_fingerprint = "sha256:demo-payee-fingerprint"
        self.beneficiary_country = "IN"
        self.beneficiary_age_hours = 720.0
        self.beneficiary_in_cooling_off = False

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def count(self, name: str) -> int:
        return self.names().count(name)

    # -- account ------------------------------------------------------
    def validate_transfer(self, **kwargs):
        self._record("validate_transfer", **kwargs)
        if not self.validate_ok:
            return {"ok": False, "reason": self.validate_reason}
        return {
            "ok": True,
            "reservation_id": f"res-{uuid.uuid4()}",
            "beneficiary": {
                "id": str(kwargs.get("beneficiary_id") or uuid.uuid4()),
                "masked": "****3456",
                "type": "DOMESTIC",
                "fingerprint": self.beneficiary_fingerprint,
                "country": self.beneficiary_country,
                "age_hours": self.beneficiary_age_hours,
                "in_cooling_off": self.beneficiary_in_cooling_off,
            },
        }

    def release_limit(self, reservation_id):
        self._record("release_limit", reservation_id=reservation_id)
        return {"released": True}

    # -- ledger -------------------------------------------------------
    def place_hold(self, **kwargs):
        self._record("place_hold", **kwargs)
        if not self.sufficient_funds:
            raise RemoteServiceError(
                "ledger", 422,
                {"error": {"code": "INSUFFICIENT_FUNDS",
                           "detail": {"available": "10.0000", "requested": "5000.0000"}}},
            )
        self.hold_id = str(uuid.uuid4())
        return {"hold_id": self.hold_id, "status": "ACTIVE"}

    def capture_hold(self, **kwargs):
        self._record("capture_hold", **kwargs)
        if self.capture_fails:
            raise ServiceUnavailable("ledger is down")
        self.journal_entry_id = str(uuid.uuid4())
        return {"journal_entry_id": self.journal_entry_id, "reference": "JE-TEST"}

    def release_hold(self, **kwargs):
        self._record("release_hold", **kwargs)
        if self.release_hold_fails:
            raise ServiceUnavailable("ledger is down")
        return {"released": True}

    def reverse_entry(self, **kwargs):
        self._record("reverse_entry", **kwargs)
        return {"journal_entry_id": str(uuid.uuid4()), "reverses": True}

    # -- fraud --------------------------------------------------------
    def screen(self, payload):
        self._record("screen", **payload)
        if not self.fraud_available:
            raise ServiceUnavailable("fraud is down")
        result = {
            "decision_id": str(uuid.uuid4()),
            "decision": self.fraud_decision,
            "score": self.fraud_score,
            "reason_codes": [],
            "latency_ms": 8,
            "case_id": None,
        }
        if self.fraud_decision in ("REVIEW", "BLOCK"):
            result["case_id"] = str(uuid.uuid4())
        return result


@pytest.fixture
def peers(monkeypatch):
    fake = FakePeers()
    from payments import clients, saga

    monkeypatch.setattr(clients, "validate_transfer", fake.validate_transfer)
    monkeypatch.setattr(clients, "release_limit", fake.release_limit)
    monkeypatch.setattr(clients, "place_hold", fake.place_hold)
    monkeypatch.setattr(clients, "capture_hold", fake.capture_hold)
    monkeypatch.setattr(clients, "release_hold", fake.release_hold)
    monkeypatch.setattr(clients, "reverse_entry", fake.reverse_entry)
    monkeypatch.setattr(clients, "screen", fake.screen)
    monkeypatch.setattr(saga, "clients", clients)

    # Funding posts a journal entry directly rather than capturing a hold.
    class _LedgerClient:
        @staticmethod
        def post(path, json=None, idempotency_key=None):
            fake._record("post_journal_entry", path=path)
            fake.journal_entry_id = str(uuid.uuid4())
            return {"journal_entry_id": fake.journal_entry_id, "reference": "JE-TEST"}

    monkeypatch.setattr(clients, "ledger_client", lambda: _LedgerClient)
    return fake


@pytest.fixture(autouse=True)
def no_outbound_events(monkeypatch):
    monkeypatch.setattr(
        "platform_common.events.publisher._enqueue_relay", lambda outbox_id: None
    )


@pytest.fixture(autouse=True)
def no_rail_dispatch(monkeypatch):
    """Rail dispatch is async; tests that care drive it explicitly."""
    monkeypatch.setattr("django_q.tasks.async_task", lambda *a, **k: "fake-task-id")


@pytest.fixture
def transfer():
    from payments import services

    def make(**overrides):
        defaults = dict(
            user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            beneficiary_id=uuid.uuid4(),
            amount=Decimal("5000.0000"),
            currency="INR",
            rail="DOMESTIC",
            idempotency_key=str(uuid.uuid4()),
        )
        defaults.update(overrides)
        return services.create_transfer(**defaults)

    return make
