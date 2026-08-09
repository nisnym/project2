"""Django Q2 tasks for kyc-svc."""

from __future__ import annotations

from platform_common.observability.context import correlation_scope

from . import services


def process_case(case_id: str) -> str:
    """Document OCR + sanctions screening. Genuinely slow, hence async."""
    with correlation_scope():
        return services.process_case(case_id).status


def sweep_stuck_cases() -> dict:
    with correlation_scope():
        return services.sweep_stuck_cases()
