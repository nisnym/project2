"""Payment rail adapters.

Every external dependency sits behind an interface with a simulated
implementation, so nothing in the domain knows whether a rail is real. Swapping
in a genuine ACH/SWIFT connector means writing one class.

Simulated outcomes are **deterministic by transaction id**, so a demo behaves
the same way every time and tests are not flaky.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RailAck:
    reference: str
    accepted: bool = True
    message: str = ""


class RailAdapter(Protocol):
    name: str

    def submit(self, txn) -> RailAck: ...


class SimulatedRail:
    """Accepts the instruction and settles asynchronously.

    A real rail confirms settlement over a callback hours or days later; the
    simulator does the same thing on a Q2 delay so the asynchronous path is
    genuinely exercised rather than short-circuited.
    """

    def __init__(self, name: str, prefix: str, settle_after_seconds: int = 5):
        self.name = name
        self.prefix = prefix
        self.settle_after_seconds = settle_after_seconds

    def _deterministic_outcome(self, txn) -> bool:
        """Same transaction id -> same outcome, every run."""
        digest = hashlib.sha256(str(txn.id).encode()).hexdigest()
        # ~4% return rate, enough to exercise the compensation path on demand.
        return int(digest[:2], 16) >= 10

    def submit(self, txn) -> RailAck:
        from django_q.tasks import async_task

        reference = f"{self.prefix}-{str(txn.id)[:8].upper()}"
        will_settle = self._deterministic_outcome(txn)
        logger.info("rail %s accepted %s as %s", self.name, txn.reference, reference)

        async_task(
            "payments.rails.deliver_outcome",
            str(txn.id), reference, will_settle,
            q_options={"save": False},
        )
        return RailAck(reference=reference)


def deliver_outcome(txn_id: str, reference: str, settled: bool) -> str:
    """Simulated rail callback."""
    from . import services

    if settled:
        services.settle_from_rail(txn_id, rail_ref=reference)
        return "settled"
    services.return_from_rail(txn_id, reason="BENEFICIARY_ACCOUNT_CLOSED")
    return "returned"


ADAPTERS = {
    "DOMESTIC": SimulatedRail("NEFT", "NEFT"),
    "INTERNATIONAL": SimulatedRail("SWIFT", "SWIFT", settle_after_seconds=20),
    "BANK_DEBIT": SimulatedRail("ACH", "ACH"),
    "CARD": SimulatedRail("CARD", "CARD"),
    "WALLET": SimulatedRail("WALLET", "WLT"),
}


def get_adapter(rail: str) -> RailAdapter:
    adapter = ADAPTERS.get(rail)
    if adapter is None:
        raise ValueError(f"no rail adapter for {rail!r}")
    return adapter
