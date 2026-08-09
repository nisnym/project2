"""Publishing side of the event backbone.

``publish()`` MUST be called inside the same ``transaction.atomic()`` block as
the state change it describes. That is what makes the pair atomic: either both
the change and the outbox rows commit, or neither does. Publishing outside a
transaction is a dual write, and dual writes lose events -- so we assert on it
rather than trusting a code review to catch it.
"""

from __future__ import annotations

import functools
import logging

from django.conf import settings
from django.db import IntegrityError, transaction

from ..models import OutboxEvent
from .envelope import EventEnvelope

logger = logging.getLogger(__name__)

__all__ = ["publish", "subscribers_for", "PublishedOutsideTransaction"]


class PublishedOutsideTransaction(RuntimeError):
    """publish() was called without an enclosing atomic block."""


@functools.lru_cache(maxsize=1)
def _subscription_table() -> dict[str, list[str]]:
    """``{event_type: [subscriber, ...]}`` from settings.

    ``"*"`` as an event_type subscribes a service to everything -- audit-svc is
    the only service that should use it.
    """
    return dict(getattr(settings, "EVENT_SUBSCRIPTIONS", {}))


def subscribers_for(event_type: str) -> list[str]:
    table = _subscription_table()
    subs: list[str] = list(table.get(event_type, []))
    for wildcard_sub in table.get("*", []):
        if wildcard_sub not in subs:
            subs.append(wildcard_sub)
    # Never deliver an event back to the service that produced it: that would
    # re-enter the same aggregate through its own handler.
    me = getattr(settings, "SERVICE_NAME", None)
    return [s for s in subs if s != me]


def publish(envelope: EventEnvelope) -> list[OutboxEvent]:
    """Write one outbox row per subscriber, and schedule immediate delivery.

    Returns the rows created (empty if nobody subscribes -- which is legitimate,
    not an error, and we log it so an unrouted event is visible).
    """
    if not transaction.get_connection().in_atomic_block:
        raise PublishedOutsideTransaction(
            f"publish({envelope.event_type}) called outside transaction.atomic(). "
            "The outbox row must commit with the state change or the event is a dual write."
        )

    subscribers = subscribers_for(envelope.event_type)
    if not subscribers:
        logger.warning(
            "event %s has no subscribers; nothing will be delivered", envelope.event_type
        )
        return []

    data = envelope.to_dict()
    rows = [
        OutboxEvent(
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            aggregate_id=str(envelope.aggregate_id),
            subscriber=subscriber,
            envelope=data,
        )
        for subscriber in subscribers
    ]

    try:
        # ignore_conflicts covers the case where the caller's transaction is
        # retried after a serialisation failure and publish() runs twice for the
        # same event_id -- the unique constraint absorbs it.
        OutboxEvent.objects.bulk_create(rows, ignore_conflicts=True)
    except IntegrityError:
        logger.warning("outbox rows for %s already exist; skipping", envelope.event_id)
        return []

    # Fast path. The sweeper is the durable path; this is what makes delivery
    # feel instant. Registered on_commit so it never fires for a rolled-back
    # transaction (verified in the Q2 spike).
    for row in rows:
        transaction.on_commit(functools.partial(_enqueue_relay, str(row.id)))

    logger.info(
        "published %s seq=%s to %s", envelope.event_type, envelope.sequence, subscribers
    )
    return rows


def _enqueue_relay(outbox_id: str) -> None:
    """Enqueue the delivery task. Never raise: a failure here must not surface as
    an error to the caller whose transaction has already committed. The sweeper
    will pick the row up regardless."""
    from django_q.tasks import async_task

    try:
        async_task(
            "platform_common.events.tasks.relay_one",
            outbox_id,
            q_options={"save": False, "timeout": 30},
        )
    except Exception:
        logger.exception(
            "could not enqueue relay for outbox=%s; sweeper will retry", outbox_id
        )
