"""Django Q2 tasks for onboarding-svc."""

from __future__ import annotations

from platform_common.observability.context import correlation_scope

from . import services


def sweep_stuck_applications() -> dict:
    with correlation_scope():
        return services.sweep_stuck_applications()
