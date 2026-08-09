"""Notification rendering, dedupe and content safety."""

from __future__ import annotations

import uuid

import pytest

from notification import services
from notification.models import Channel, Notification, Preference

pytestmark = pytest.mark.django_db

PAYMENT_PAYLOAD = {
    "user_id": str(uuid.uuid4()),
    "reference": "TXN-20260808-A7F3K2",
    "amount": {"amount": "150000.0000", "currency": "INR"},
    "beneficiary_masked": "****3456",
}


def notify(event_type, payload=None, event_id=None):
    payload = payload or PAYMENT_PAYLOAD
    return services.notify(
        event_id=event_id or uuid.uuid4(), event_type=event_type,
        user_id=payload["user_id"], payload=payload,
    )


class TestRendering:
    def test_settled_notification_includes_the_details(self):
        created = notify("payment.settled")
        assert created
        body = created[0].body
        assert "150000.0000 INR" in body
        assert "TXN-20260808-A7F3K2" in body
        assert "****3456" in body

    def test_multiple_channels_are_created(self):
        created = notify("payment.settled")
        assert {n.channel for n in created} == {Channel.IN_APP, Channel.EMAIL}

    def test_unknown_event_produces_nothing(self):
        assert notify("something.unhandled") == []

    def test_missing_user_produces_nothing(self):
        assert services.notify(
            event_id=uuid.uuid4(), event_type="payment.settled",
            user_id=None, payload={},
        ) == []


class TestContentSafety:
    def test_blocked_notification_leaks_no_fraud_detail(self):
        """Telling a fraudster which rule fired tells them how to evade it."""
        payload = {**PAYMENT_PAYLOAD,
                   "fraud": {"score": 92, "reason_codes": ["R006", "R004"]}}
        created = notify("payment.blocked", payload)
        body = " ".join(n.body + n.subject for n in created)
        for leak in ("R006", "R004", "92", "score", "blacklist"):
            assert leak not in body, f"notification leaked {leak!r}"
        assert "contact support" in body.lower()

    def test_blocked_notification_reassures_about_the_money(self):
        body = notify("payment.blocked")[0].body
        assert "no money has left your account" in body.lower()

    def test_review_notification_explains_the_hold(self):
        body = notify("payment.review_required")[0].body
        assert "hold" in body.lower()


class TestDeduplication:
    def test_redelivered_event_creates_no_second_message(self):
        """At-least-once delivery must not mean two emails."""
        event_id = uuid.uuid4()
        first = notify("payment.settled", event_id=event_id)
        second = notify("payment.settled", event_id=event_id)

        assert len(first) == 2
        assert second == []
        assert Notification.objects.count() == 2

    def test_different_events_do_create_separate_messages(self):
        notify("payment.settled")
        notify("payment.settled")
        assert Notification.objects.filter(channel=Channel.IN_APP).count() == 2


class TestPreferences:
    def test_a_disabled_channel_is_skipped(self):
        user_id = PAYMENT_PAYLOAD["user_id"]
        Preference.objects.create(
            user_id=user_id, category="TRANSFER_SETTLED",
            channel=Channel.EMAIL, enabled=False,
        )
        created = notify("payment.settled")
        assert {n.channel for n in created} == {Channel.IN_APP}


class TestDelivery:
    def test_delivery_marks_sent(self):
        notification = notify("payment.settled")[0]
        assert services.deliver(notification) is True
        notification.refresh_from_db()
        assert notification.status == Notification.Status.SENT
        assert notification.sent_at is not None

    def test_delivery_is_idempotent(self):
        notification = notify("payment.settled")[0]
        services.deliver(notification)
        first_sent_at = Notification.objects.get(pk=notification.pk).sent_at
        services.deliver(notification)
        assert Notification.objects.get(pk=notification.pk).sent_at == first_sent_at

    def test_retry_task_drains_the_queue(self):
        from notification.tasks import retry_failed_deliveries

        notify("payment.settled")
        result = retry_failed_deliveries()
        assert result["sent"] == 2
        assert not Notification.objects.filter(status=Notification.Status.QUEUED).exists()


class TestHandlerWiring:
    def test_every_template_has_a_subscribed_handler(self):
        """A template nobody subscribes to is dead code; a subscription with no
        template silently drops a customer-visible event."""
        from platform_common.events.dispatcher import handlers_for

        import notification.handlers  # noqa: F401

        for event_type in services.TEMPLATES:
            assert handlers_for(event_type), f"no handler for {event_type}"
