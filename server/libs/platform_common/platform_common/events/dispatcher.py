"""Handler registry and the ordering guard.

Services register handlers in their ``handlers.py``::

    @subscribe("payment.settled")
    def on_settled(env: EventEnvelope) -> None:
        ...

``handlers.py`` is imported from the app's ``AppConfig.ready()`` so registration
happens exactly once per process.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from django.db import transaction

from ..models import ProjectionCursor
from .envelope import EventEnvelope

logger = logging.getLogger(__name__)

__all__ = ["subscribe", "handlers_for", "registered_event_types", "guard_sequence"]

_HANDLERS: dict[str, list[Callable[[EventEnvelope], None]]] = {}


def subscribe(*event_types: str):
    """Register a handler for one or more event types."""

    def decorator(fn: Callable[[EventEnvelope], None]):
        for event_type in event_types:
            bucket = _HANDLERS.setdefault(event_type, [])
            # Guard against double registration when a module is imported twice
            # (autoreloader, or ready() firing more than once).
            if not any(
                h.__module__ == fn.__module__ and h.__qualname__ == fn.__qualname__
                for h in bucket
            ):
                bucket.append(fn)
        return fn

    return decorator


def handlers_for(event_type: str) -> list[Callable[[EventEnvelope], None]]:
    """Handlers registered for this exact type, plus any wildcard handlers.

    ``@subscribe("*")`` catches everything -- audit-svc is the only service that
    should use it. Exact handlers run first so a specific projection is updated
    before the generic recorder sees it.
    """
    handlers = list(_HANDLERS.get(event_type, []))
    for wildcard_handler in _HANDLERS.get("*", []):
        if wildcard_handler not in handlers:
            handlers.append(wildcard_handler)
    return handlers


def registered_event_types() -> list[str]:
    return sorted(_HANDLERS)


def guard_sequence(projection: str, envelope: EventEnvelope) -> bool:
    """Return True if this event should be applied to ``projection``.

    Parallel workers mean events for one aggregate can arrive out of order. A
    projection must not be moved backwards by a late-arriving older event, so we
    track the highest sequence applied and skip anything at or below it.

    Call this from handlers that mutate a projection. Handlers that only append
    (audit) or that are naturally idempotent (notification, guarded by a unique
    constraint) do not need it.
    """
    aggregate_id = str(envelope.aggregate_id)
    cursor, _ = ProjectionCursor.objects.select_for_update().get_or_create(
        projection=projection,
        aggregate_id=aggregate_id,
        defaults={"last_sequence": -1},
    )
    if envelope.sequence <= cursor.last_sequence:
        logger.info(
            "skipping stale %s for %s: seq %s <= applied %s",
            envelope.event_type, aggregate_id, envelope.sequence, cursor.last_sequence,
        )
        return False
    cursor.last_sequence = envelope.sequence
    cursor.save(update_fields=["last_sequence", "updated_at"])
    return True


def require_atomic(fn):
    """Decorator for handlers that must run inside a transaction.

    dispatch_one already wraps handlers in atomic(); this catches a handler that
    gets called from somewhere else.
    """

    def wrapper(envelope: EventEnvelope):
        if not transaction.get_connection().in_atomic_block:
            raise RuntimeError(f"{fn.__qualname__} must run inside transaction.atomic()")
        return fn(envelope)

    wrapper.__module__ = fn.__module__
    wrapper.__qualname__ = fn.__qualname__
    wrapper.__name__ = fn.__name__
    return wrapper
