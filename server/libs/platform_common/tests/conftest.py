"""Test harness for platform_common.

Django setup is driven by pytest-django from DJANGO_SETTINGS_MODULE in
pytest.ini (tests/settings.py) -- calling settings.configure() here instead
leaves pytest-django's test-case machinery uninitialised.

Q_CLUSTER["sync"] is on in those settings, so async_task runs inline and no
worker process is needed (verified behaviour, LLD s16.1).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def envelope_factory():
    """Build envelopes without repeating the boilerplate in every test."""
    import uuid

    from platform_common.events import EventEnvelope

    def make(**overrides):
        defaults = {
            "event_type": "payment.settled",
            "aggregate_type": "transaction",
            "aggregate_id": str(uuid.uuid4()),
            "sequence": 1,
            "producer": "payments",
            "correlation_id": str(uuid.uuid4()),
            "payload": {"amount": {"amount": "100.0000", "currency": "INR"}},
        }
        defaults.update(overrides)
        return EventEnvelope(**defaults)

    return make


@pytest.fixture
def captured_deliveries(monkeypatch):
    """Intercept the HTTP hop so tests assert on envelopes, not on network."""
    sent: list[tuple[str, dict]] = []

    def fake_deliver(subscriber, envelope_dict, **kwargs):
        sent.append((subscriber, envelope_dict))
        return 202

    monkeypatch.setattr("platform_common.events.tasks.deliver", fake_deliver)
    return sent


@pytest.fixture
def failing_deliveries(monkeypatch):
    """Make every delivery fail transiently, to exercise the retry ladder."""
    from platform_common.events.transport import TransientDeliveryError

    attempts: list[str] = []

    def fake_deliver(subscriber, envelope_dict, **kwargs):
        attempts.append(subscriber)
        raise TransientDeliveryError(f"{subscriber} is down")

    monkeypatch.setattr("platform_common.events.tasks.deliver", fake_deliver)
    return attempts
