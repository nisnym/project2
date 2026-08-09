"""The event envelope: the one shape every event has on every hop.

Design notes:
  * ``event_id`` is the idempotency key for the entire pipeline. It is generated
    once by the producer and never regenerated, so a redelivery is detectable at
    the subscriber's inbox.
  * ``canonical()`` must be byte-stable: it feeds the HMAC signature and the
    audit hash chain. Sorted keys, no whitespace, Decimals as strings.
  * ``from_dict()`` deliberately *ignores unknown fields* so a producer can add
    an optional field without breaking older consumers (forward compatibility).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

__all__ = ["EventEnvelope", "utcnow_iso", "canonical_json"]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default(obj: Any) -> Any:
    # Money must not round-trip through a binary float.
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    raise TypeError(f"{type(obj).__name__} is not JSON-serialisable in an event payload")


def canonical_json(data: Any) -> str:
    """Deterministic JSON. Used for signing and hashing -- must never vary."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_default)


@dataclass(frozen=True)
class EventEnvelope:
    event_type: str
    aggregate_type: str
    aggregate_id: str
    sequence: int
    payload: dict
    producer: str
    correlation_id: str
    actor: dict = field(default_factory=lambda: {"type": "system", "id": None})
    causation_id: str | None = None
    event_version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=utcnow_iso)

    def __post_init__(self) -> None:
        if not self.event_type or "." not in self.event_type:
            raise ValueError(
                f"event_type must be 'noun.verb-past-tense', got {self.event_type!r}"
            )
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        # Fail at construction rather than at delivery time: an event that cannot
        # be serialised is far cheaper to find here than in a relay worker.
        canonical_json(self.payload)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EventEnvelope":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    def child(self, **overrides) -> "EventEnvelope":
        """Derive a new event caused by this one, preserving the correlation."""
        base = {
            "producer": self.producer,
            "correlation_id": self.correlation_id,
            "causation_id": self.event_id,
            "actor": self.actor,
        }
        base.update(overrides)
        return EventEnvelope(**base)

    def __str__(self) -> str:
        return f"<{self.event_type} {self.aggregate_type}:{self.aggregate_id} seq={self.sequence}>"
