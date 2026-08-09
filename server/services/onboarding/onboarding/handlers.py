"""onboarding-svc resumes its workflow when KYC finishes."""

from __future__ import annotations

import logging

from platform_common.events import subscribe
from platform_common.events.envelope import EventEnvelope

from . import services

logger = logging.getLogger(__name__)


@subscribe("kyc.completed")
def on_kyc_completed(env: EventEnvelope) -> None:
    """The async resumption point for UC1."""
    payload = env.payload
    services.on_kyc_completed(
        application_id=payload["application_id"],
        kyc_status=payload.get("status", "FAILED"),
        risk_rating=payload.get("risk_rating", ""),
        identity_score=payload.get("identity_score"),
        sanctions_hit=bool(payload.get("sanctions_hit")),
        pep_hit=bool(payload.get("pep_hit")),
    )
