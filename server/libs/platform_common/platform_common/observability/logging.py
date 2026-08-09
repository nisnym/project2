"""JSON log formatter carrying the service name and correlation id.

Deliberately has no PII: log lines carry ids and masked references only. A
support engineer joins on ``correlation_id``; they never need the customer's
name in a log file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .context import get_actor, get_correlation_id

_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = "unknown", **kwargs):
        super().__init__(**kwargs)
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        actor = get_actor()
        if actor.get("id"):
            entry["actor"] = actor

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        # Anything passed as extra={...} rides along.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value

        return json.dumps(entry, default=str)
