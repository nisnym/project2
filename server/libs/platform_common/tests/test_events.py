"""The event backbone.

These tests encode the guarantees the whole architecture rests on:
no lost events, no phantom events, at-least-once delivery, effect-once
processing, and tolerance of out-of-order arrival.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.db import transaction
from django.test import Client

from platform_common.events import EventEnvelope, publish, subscribe
from platform_common.events.dispatcher import _HANDLERS, guard_sequence, handlers_for
from platform_common.events.publisher import PublishedOutsideTransaction, subscribers_for
from platform_common.events.tasks import (
    BACKOFF_SECONDS,
    dispatch_one,
    relay_one,
    sweep_inbox,
    sweep_outbox,
)
from platform_common.events.transport import (
    PermanentDeliveryError,
    sign_body,
    verify_signature,
)
from platform_common.models import InboxEvent, InboxStatus, OutboxEvent, OutboxStatus

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean_handler_registry():
    """Handlers are process-global; leaking them between tests causes
    order-dependent failures that are miserable to debug."""
    snapshot = {k: list(v) for k, v in _HANDLERS.items()}
    yield
    _HANDLERS.clear()
    _HANDLERS.update(snapshot)


# ---------------------------------------------------------------- envelope


class TestEnvelope:
    def test_rejects_a_command_shaped_name(self, envelope_factory):
        # Events are facts that already happened. A name without a dot is
        # almost always someone trying to send a command.
        with pytest.raises(ValueError, match="past-tense"):
            envelope_factory(event_type="dosomething")

    def test_rejects_unserialisable_payload_at_construction(self, envelope_factory):
        # Far cheaper to find here than in a relay worker at 3am.
        with pytest.raises(TypeError):
            envelope_factory(payload={"when": object()})

    def test_canonical_form_is_stable(self, envelope_factory):
        env = envelope_factory()
        assert env.canonical() == env.canonical()

    def test_canonical_form_is_key_order_independent(self, envelope_factory):
        # The HMAC and the audit hash both depend on this.
        env = envelope_factory(payload={"b": 2, "a": 1})
        other = EventEnvelope.from_dict(json.loads(json.dumps(env.to_dict())))
        assert other.canonical() == env.canonical()

    def test_unknown_fields_are_ignored(self, envelope_factory):
        # Forward compatibility: a newer producer may add fields.
        data = envelope_factory().to_dict()
        data["field_from_the_future"] = "surprise"
        assert EventEnvelope.from_dict(data).event_type == "payment.settled"

    def test_child_preserves_correlation_and_records_causation(self, envelope_factory):
        parent = envelope_factory()
        child = parent.child(
            event_type="notification.sent", aggregate_type="notification",
            aggregate_id="n1", sequence=0, payload={},
        )
        assert child.correlation_id == parent.correlation_id
        assert child.causation_id == parent.event_id
        assert child.event_id != parent.event_id


# --------------------------------------------------------------- publisher


class TestPublish:
    # transaction=True: the default django_db fixture wraps each test in a
    # transaction, which would make in_atomic_block always true and the guard
    # untestable.
    @pytest.mark.django_db(transaction=True)
    def test_refuses_to_publish_outside_a_transaction(self, envelope_factory):
        # A publish outside a transaction is a dual write, and dual writes lose
        # events. We assert rather than trust code review.
        with pytest.raises(PublishedOutsideTransaction):
            publish(envelope_factory())

    def test_creates_one_row_per_subscriber(self, envelope_factory):
        env = envelope_factory(event_type="payment.settled")
        with transaction.atomic():
            rows = publish(env)
        expected = set(subscribers_for("payment.settled"))
        assert {r.subscriber for r in rows} == expected
        assert OutboxEvent.objects.filter(event_id=env.event_id).count() == len(expected)

    def test_never_delivers_an_event_back_to_its_producer(self):
        # payments produces payment.settled; it must not consume its own event.
        assert "payments" not in subscribers_for("payment.settled")

    def test_audit_receives_everything_via_wildcard(self):
        for event_type in ("payment.settled", "account.opened", "kyc.completed"):
            assert "audit" in subscribers_for(event_type)

    def test_rollback_produces_no_outbox_rows(self, envelope_factory):
        # The "no phantom events" guarantee.
        env = envelope_factory()
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                publish(env)
                raise RuntimeError("business rule failed")
        assert not OutboxEvent.objects.filter(event_id=env.event_id).exists()

    def test_publishing_the_same_event_twice_is_idempotent(self, envelope_factory):
        env = envelope_factory()
        with transaction.atomic():
            publish(env)
        with transaction.atomic():
            publish(env)
        per_subscriber = OutboxEvent.objects.filter(
            event_id=env.event_id, subscriber="audit"
        ).count()
        assert per_subscriber == 1

    def test_unrouted_event_is_not_an_error(self, envelope_factory):
        with transaction.atomic():
            rows = publish(envelope_factory(event_type="nobody.listens"))
        # audit's wildcard still catches it; the point is it does not raise.
        assert all(r.subscriber == "audit" for r in rows)


# ------------------------------------------------------------------- relay


class TestRelay:
    def test_successful_delivery_marks_sent(self, envelope_factory, captured_deliveries):
        with transaction.atomic():
            rows = publish(envelope_factory())
        row = rows[0]
        assert relay_one(str(row.id)) == "sent"
        row.refresh_from_db()
        assert row.status == OutboxStatus.SENT
        assert row.sent_at is not None

    def test_transient_failure_schedules_a_backoff(
        self, envelope_factory, failing_deliveries
    ):
        with transaction.atomic():
            rows = publish(envelope_factory())
        row = rows[0]
        assert relay_one(str(row.id)) == "retry"
        row.refresh_from_db()
        assert row.status == OutboxStatus.PENDING
        assert row.attempts == 1
        assert row.next_attempt_at is not None
        assert "is down" in row.last_error

    def test_exhausting_the_ladder_marks_dead(self, envelope_factory, failing_deliveries):
        with transaction.atomic():
            rows = publish(envelope_factory())
        row = rows[0]
        for _ in range(len(BACKOFF_SECONDS)):
            row.refresh_from_db()
            row.next_attempt_at = row.created_at  # skip the wait
            row.save(update_fields=["next_attempt_at"])
            relay_one(str(row.id))
        row.refresh_from_db()
        assert row.status == OutboxStatus.DEAD
        assert row.attempts == len(BACKOFF_SECONDS)

    def test_permanent_failure_dies_immediately(self, envelope_factory, monkeypatch):
        # Retrying a payload the peer will never accept just delays the DEAD
        # signal to ops.
        def reject(subscriber, envelope_dict, **kwargs):
            raise PermanentDeliveryError("malformed")

        monkeypatch.setattr("platform_common.events.tasks.deliver", reject)
        with transaction.atomic():
            rows = publish(envelope_factory())
        assert relay_one(str(rows[0].id)) == "dead"
        rows[0].refresh_from_db()
        assert rows[0].status == OutboxStatus.DEAD
        assert rows[0].attempts == 1

    def test_relaying_an_already_sent_row_is_a_noop(
        self, envelope_factory, captured_deliveries
    ):
        # The on_commit task and the sweeper can both target one row.
        with transaction.atomic():
            rows = publish(envelope_factory())
        relay_one(str(rows[0].id))
        assert relay_one(str(rows[0].id)) == "skipped"
        assert len(captured_deliveries) == 1

    def test_a_down_subscriber_does_not_stall_the_others(
        self, envelope_factory, monkeypatch
    ):
        """The reason fan-out happens at publish time, not delivery time."""
        from platform_common.events.transport import TransientDeliveryError

        def selective(subscriber, envelope_dict, **kwargs):
            if subscriber == "notification":
                raise TransientDeliveryError("notification is down")
            return 202

        monkeypatch.setattr("platform_common.events.tasks.deliver", selective)
        with transaction.atomic():
            publish(envelope_factory(event_type="payment.settled"))
        sweep_outbox()

        assert (
            OutboxEvent.objects.get(subscriber="audit").status == OutboxStatus.SENT
        )
        assert (
            OutboxEvent.objects.get(subscriber="notification").status
            == OutboxStatus.PENDING
        )

    def test_sweeper_recovers_a_lost_on_commit_task(
        self, envelope_factory, captured_deliveries
    ):
        """Simulates the process dying between COMMIT and enqueue."""
        env = envelope_factory()
        with transaction.atomic():
            publish(env)
        captured_deliveries.clear()  # pretend the fast-path task never ran

        result = sweep_outbox()
        assert result["processed"] >= 1
        assert len(captured_deliveries) >= 1
        assert not OutboxEvent.objects.filter(status=OutboxStatus.PENDING).exists()

    def test_sweeper_respects_backoff(self, envelope_factory, failing_deliveries):
        with transaction.atomic():
            publish(envelope_factory())
        sweep_outbox()
        failing_deliveries.clear()
        # next_attempt_at is now in the future, so a second sweep must skip it.
        assert sweep_outbox()["processed"] == 0
        assert failing_deliveries == []


# ------------------------------------------------------------------- inbox


class TestIngestEndpoint:
    def _post(self, client, envelope, *, sign=True, timestamp=None):
        from platform_common.events.envelope import canonical_json
        import time

        body = canonical_json(envelope.to_dict()).encode()
        timestamp = timestamp or str(int(time.time()))
        headers = {}
        if sign:
            headers["HTTP_X_SIGNATURE"] = sign_body(body, timestamp)
            headers["HTTP_X_SIGNATURE_TIMESTAMP"] = timestamp
        return client.post(
            "/internal/events", data=body, content_type="application/json", **headers
        )

    def test_accepts_a_signed_event(self, envelope_factory):
        response = self._post(Client(), envelope_factory())
        assert response.status_code == 202
        assert InboxEvent.objects.count() == 1

    def test_duplicate_delivery_returns_200_and_changes_nothing(self, envelope_factory):
        """At-least-once delivery becomes effect-once processing here."""
        client, env = Client(), envelope_factory()
        assert self._post(client, env).status_code == 202
        assert self._post(client, env).status_code == 200
        assert InboxEvent.objects.count() == 1

    def test_rejects_an_unsigned_request(self, envelope_factory):
        response = self._post(Client(), envelope_factory(), sign=False)
        assert response.status_code == 401
        assert InboxEvent.objects.count() == 0

    def test_rejects_a_tampered_body(self, envelope_factory):
        from platform_common.events.envelope import canonical_json
        import time

        env = envelope_factory()
        timestamp = str(int(time.time()))
        signature = sign_body(canonical_json(env.to_dict()).encode(), timestamp)
        tampered = env.to_dict()
        tampered["payload"]["amount"]["amount"] = "999999.0000"

        response = Client().post(
            "/internal/events",
            data=canonical_json(tampered),
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SIGNATURE_TIMESTAMP=timestamp,
        )
        assert response.status_code == 401

    def test_rejects_a_replayed_old_request(self, envelope_factory):
        import time

        stale = str(int(time.time()) - 3600)
        assert self._post(Client(), envelope_factory(), timestamp=stale).status_code == 401

    def test_malformed_envelope_returns_400_so_the_publisher_gives_up(self):
        response = Client().post(
            "/internal/events", data=b"{not json", content_type="application/json",
            HTTP_X_SIGNATURE=sign_body(b"{not json", "1"), HTTP_X_SIGNATURE_TIMESTAMP="1",
        )
        # 401 for skew or 400 for parse -- either way, not a retry loop.
        assert response.status_code in (400, 401)


class TestSignature:
    def test_round_trip(self):
        import time

        body, timestamp = b'{"a":1}', str(int(time.time()))
        assert verify_signature(body, sign_body(body, timestamp), timestamp)

    def test_wrong_signature_fails(self):
        import time

        timestamp = str(int(time.time()))
        assert not verify_signature(b'{"a":1}', "deadbeef", timestamp)

    def test_missing_signature_fails(self):
        import time

        assert not verify_signature(b"{}", "", str(int(time.time())))


# -------------------------------------------------------------- dispatcher


class TestDispatch:
    def _accept(self, envelope) -> InboxEvent:
        return InboxEvent.objects.create(
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            aggregate_id=str(envelope.aggregate_id),
            sequence=envelope.sequence,
            producer=envelope.producer,
            correlation_id=envelope.correlation_id,
            envelope=envelope.to_dict(),
        )

    def test_runs_registered_handlers(self, envelope_factory):
        seen = []

        @subscribe("payment.settled")
        def handler(env):
            seen.append(env.event_id)

        env = envelope_factory()
        self._accept(env)
        assert dispatch_one(str(env.event_id)) == "processed"
        assert seen == [env.event_id]

    def test_reprocessing_is_a_noop(self, envelope_factory):
        calls = []

        @subscribe("payment.settled")
        def handler(env):
            calls.append(1)

        env = envelope_factory()
        self._accept(env)
        dispatch_one(str(env.event_id))
        assert dispatch_one(str(env.event_id)) == "already-processed"
        assert len(calls) == 1

    def test_wildcard_handler_receives_every_type(self, envelope_factory):
        seen = []

        @subscribe("*")
        def record_all(env):
            seen.append(env.event_type)

        for event_type in ("payment.settled", "account.opened"):
            env = envelope_factory(event_type=event_type)
            self._accept(env)
            dispatch_one(str(env.event_id))
        assert seen == ["payment.settled", "account.opened"]

    def test_missing_handler_is_flagged_not_silently_dropped(self, envelope_factory):
        env = envelope_factory(event_type="nobody.handles")
        self._accept(env)
        assert dispatch_one(str(env.event_id)) == "no-handler"
        assert InboxEvent.objects.get(pk=env.event_id).status == InboxStatus.SKIPPED

    def test_handler_failure_leaves_the_row_retryable(self, envelope_factory):
        @subscribe("payment.settled")
        def broken(env):
            raise ValueError("handler bug")

        env = envelope_factory()
        self._accept(env)
        with pytest.raises(ValueError):
            dispatch_one(str(env.event_id))

        row = InboxEvent.objects.get(pk=env.event_id)
        assert row.status == InboxStatus.RECEIVED   # retryable, not lost
        assert row.attempts == 1
        assert "handler bug" in row.last_error

    def test_handler_side_effects_roll_back_on_failure(self, envelope_factory):
        """No window where the side effect happened but the row says otherwise."""
        from platform_common.models import ProjectionCursor

        @subscribe("payment.settled")
        def half_broken(env):
            ProjectionCursor.objects.create(
                projection="test", aggregate_id="x", last_sequence=1
            )
            raise ValueError("fails after writing")

        env = envelope_factory()
        self._accept(env)
        with pytest.raises(ValueError):
            dispatch_one(str(env.event_id))
        assert not ProjectionCursor.objects.filter(projection="test").exists()

    def test_repeated_failure_eventually_stops_retrying(self, envelope_factory):
        from platform_common.events.tasks import MAX_INBOX_ATTEMPTS

        @subscribe("payment.settled")
        def broken(env):
            raise ValueError("permanent bug")

        env = envelope_factory()
        row = self._accept(env)
        row.attempts = MAX_INBOX_ATTEMPTS - 1
        row.save()

        with pytest.raises(ValueError):
            dispatch_one(str(env.event_id))
        assert InboxEvent.objects.get(pk=env.event_id).status == InboxStatus.FAILED

    def test_sweep_inbox_redispatches_stalled_rows(self, envelope_factory, monkeypatch):
        from datetime import timedelta

        from django.utils import timezone

        seen = []

        @subscribe("payment.settled")
        def handler(env):
            seen.append(env.event_id)

        env = envelope_factory()
        row = self._accept(env)
        # Pretend it was accepted a minute ago and never dispatched.
        InboxEvent.objects.filter(pk=row.pk).update(
            received_at=timezone.now() - timedelta(minutes=1)
        )
        assert sweep_inbox()["processed"] == 1
        assert seen == [env.event_id]


class TestSequenceGuard:
    def test_applies_increasing_sequences(self, envelope_factory):
        aggregate = str(uuid.uuid4())
        with transaction.atomic():
            assert guard_sequence("bal", envelope_factory(aggregate_id=aggregate, sequence=1))
            assert guard_sequence("bal", envelope_factory(aggregate_id=aggregate, sequence=2))

    def test_skips_a_late_arriving_older_event(self, envelope_factory):
        """Parallel workers mean order is not guaranteed; a projection must not
        be moved backwards."""
        aggregate = str(uuid.uuid4())
        with transaction.atomic():
            guard_sequence("bal", envelope_factory(aggregate_id=aggregate, sequence=5))
            assert not guard_sequence(
                "bal", envelope_factory(aggregate_id=aggregate, sequence=3)
            )

    def test_skips_an_exact_replay(self, envelope_factory):
        aggregate = str(uuid.uuid4())
        with transaction.atomic():
            guard_sequence("bal", envelope_factory(aggregate_id=aggregate, sequence=5))
            assert not guard_sequence(
                "bal", envelope_factory(aggregate_id=aggregate, sequence=5)
            )

    def test_aggregates_are_tracked_independently(self, envelope_factory):
        with transaction.atomic():
            guard_sequence("bal", envelope_factory(aggregate_id="a", sequence=9))
            assert guard_sequence("bal", envelope_factory(aggregate_id="b", sequence=1))

    def test_projections_are_tracked_independently(self, envelope_factory):
        aggregate = str(uuid.uuid4())
        with transaction.atomic():
            guard_sequence("p1", envelope_factory(aggregate_id=aggregate, sequence=9))
            assert guard_sequence(
                "p2", envelope_factory(aggregate_id=aggregate, sequence=1)
            )
