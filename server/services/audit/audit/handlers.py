"""audit-svc subscribes to every event in the estate.

It is the only service with a wildcard subscription, and the only one that
records rather than reacts.
"""

from __future__ import annotations

import logging

from platform_common.events import subscribe
from platform_common.events.envelope import EventEnvelope

from . import services

logger = logging.getLogger(__name__)


@subscribe("*")
def record_everything(envelope: EventEnvelope) -> None:
    """Append any event to the hash chain.

    Idempotent by way of the unique ``event_id``: a redelivery returns None and
    changes nothing, so the chain is never forked by a duplicate.
    """
    row = services.append(envelope)
    if row is not None:
        logger.debug("audited %s as row %s", envelope.event_type, row.id)
