"""notification-svc reacts to the events customers care about."""

from __future__ import annotations

import logging

from platform_common.events import subscribe
from platform_common.events.envelope import EventEnvelope

from . import services

logger = logging.getLogger(__name__)

WATCHED = tuple(services.TEMPLATES)


@subscribe(*WATCHED)
def on_notifiable_event(env: EventEnvelope) -> None:
    """Render and queue. Delivery itself happens on this service's own queue,
    so a slow email provider never holds the publisher's relay open."""
    user_id = env.payload.get("user_id")
    created = services.notify(
        event_id=env.event_id, event_type=env.event_type, user_id=user_id,
        payload=env.payload, correlation_id=env.correlation_id,
    )
    for notification in created:
        services.deliver(notification)
