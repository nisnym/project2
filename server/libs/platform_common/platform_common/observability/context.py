"""Correlation id carried through a request, its events, and its log lines.

A contextvar rather than thread-local: it is correct under threads *and* under
async, and each Q2 worker process gets its own. One id reconstructs a whole
transfer across ten services -- retrofitting this later is miserable, so it goes
in from the first commit.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_actor: ContextVar[dict | None] = ContextVar("actor", default=None)

__all__ = [
    "get_correlation_id",
    "set_correlation_id",
    "new_correlation_id",
    "get_actor",
    "set_actor",
    "correlation_scope",
]


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def new_correlation_id() -> str:
    value = str(uuid.uuid4())
    _correlation_id.set(value)
    return value


def get_actor() -> dict:
    return _actor.get() or {"type": "system", "id": None}


def set_actor(actor: dict | None) -> None:
    _actor.set(actor)


class correlation_scope:
    """Context manager for background work that has no inbound request.

    Used by Q2 tasks so a scheduled sweep still produces traceable logs and
    events instead of a blank correlation id.
    """

    def __init__(self, correlation_id: str | None = None, actor: dict | None = None):
        self.correlation_id = correlation_id or str(uuid.uuid4())
        self.actor = actor or {"type": "system", "id": None}
        self._tokens = ()

    def __enter__(self) -> str:
        self._tokens = (
            _correlation_id.set(self.correlation_id),
            _actor.set(self.actor),
        )
        return self.correlation_id

    def __exit__(self, *exc_info) -> None:
        cid_token, actor_token = self._tokens
        _correlation_id.reset(cid_token)
        _actor.reset(actor_token)
