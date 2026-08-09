"""Health monitoring, failure cases and reports."""

from __future__ import annotations

import uuid

import pytest

from ops import services
from ops.models import FailureCase, HealthSnapshot, Report

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_outbound_events(monkeypatch):
    monkeypatch.setattr(
        "platform_common.events.publisher._enqueue_relay", lambda outbox_id: None
    )


def snapshot(**overrides):
    defaults = dict(
        service="payments", reachable=True, queue_depth=5, failed_tasks_24h=0,
        outbox_pending=0, outbox_dead=0, inbox_failed=0, oldest_pending_age_s=1,
    )
    defaults.update(overrides)
    return HealthSnapshot.objects.create(**defaults)


class TestHealthStatus:
    def test_a_quiet_service_is_healthy(self):
        assert snapshot().status == "HEALTHY"

    def test_an_unreachable_service_says_so(self):
        s = snapshot(reachable=False, error="connection refused")
        assert s.status == "UNREACHABLE"
        assert "connection refused" in s.reason

    def test_dead_events_degrade_it(self):
        s = snapshot(outbox_dead=3)
        assert s.status == "DEGRADED"
        assert "3 dead event(s)" in s.reason

    def test_a_deep_queue_degrades_it(self):
        s = snapshot(queue_depth=500)
        assert s.status == "DEGRADED"
        assert "queue depth 500" in s.reason

    def test_a_stale_backlog_degrades_it(self):
        """Depth alone is not enough: 5 events stuck for an hour is worse than
        500 flowing through in a second."""
        s = snapshot(queue_depth=5, oldest_pending_age_s=3600)
        assert s.status == "DEGRADED"
        assert "3600s" in s.reason

    def test_failed_inbound_events_degrade_it(self):
        assert snapshot(inbox_failed=2).status == "DEGRADED"


class TestPolling:
    def test_records_a_snapshot_per_service(self, monkeypatch):
        class FakeClient:
            @staticmethod
            def get(path):
                return {"service": "x", "queue_depth": 3, "failed_tasks_24h": 0,
                        "outbox_pending": 1, "outbox_dead": 0, "inbox_failed": 0,
                        "oldest_pending_age_s": 2, "txn_under_review": 7}

        monkeypatch.setattr("ops.services.get_client", lambda *a, **k: FakeClient)
        result = services.poll_health()

        assert result["polled"] == len(services.MONITORED)
        assert result["unreachable"] == []
        assert HealthSnapshot.objects.count() == len(services.MONITORED)

    def test_service_specific_metrics_are_kept(self, monkeypatch):
        """payments reports txn_under_review; ops must not discard it just
        because it isn't one of the standard fields."""
        class FakeClient:
            @staticmethod
            def get(path):
                return {"queue_depth": 1, "txn_under_review": 7}

        monkeypatch.setattr("ops.services.get_client", lambda *a, **k: FakeClient)
        services.poll_health()
        assert HealthSnapshot.objects.first().extra["txn_under_review"] == 7

    def test_an_unreachable_service_is_recorded_not_skipped(self, monkeypatch):
        """The dashboard must show that a service is down, which means writing
        a row when the scrape fails."""
        class FakeClient:
            @staticmethod
            def get(path):
                raise RuntimeError("connection refused")

        monkeypatch.setattr("ops.services.get_client", lambda *a, **k: FakeClient)
        result = services.poll_health()

        assert len(result["unreachable"]) == len(services.MONITORED)
        assert HealthSnapshot.objects.filter(reachable=False).count() == len(services.MONITORED)

    def test_latest_health_returns_the_most_recent_per_service(self):
        snapshot(service="payments", queue_depth=1)
        snapshot(service="payments", queue_depth=99)
        latest = {s.service: s for s in services.latest_health()}
        assert latest["payments"].queue_depth == 99


class TestFailureCases:
    def test_opening_a_case(self):
        case = services.open_case(
            failure_type="PAYMENT_RETURNED", source_service="payments",
            subject_ref="txn-1", correlation_id="corr-1",
            detail={"return_reason": "BENEFICIARY_ACCOUNT_CLOSED"},
        )
        assert case.status == FailureCase.Status.OPEN

    def test_a_redelivered_event_does_not_open_a_second_case(self):
        """Events are at-least-once; ops must not get duplicate work items."""
        for _ in range(3):
            services.open_case(failure_type="PAYMENT_RETURNED",
                               source_service="payments", subject_ref="txn-1")
        assert FailureCase.objects.count() == 1

    def test_different_subjects_get_their_own_cases(self):
        services.open_case(failure_type="PAYMENT_RETURNED",
                           source_service="payments", subject_ref="txn-1")
        services.open_case(failure_type="PAYMENT_RETURNED",
                           source_service="payments", subject_ref="txn-2")
        assert FailureCase.objects.count() == 2

    def test_resolving_a_case(self):
        case = services.open_case(failure_type="PAYMENT_FAILED",
                                  source_service="payments", subject_ref="txn-9")
        assert services.resolve_case(case.id, note="customer contacted") is True
        case.refresh_from_db()
        assert case.status == FailureCase.Status.RESOLVED
        assert case.resolved_at is not None


class TestHandlerWiring:
    def test_failure_events_open_cases(self):
        from platform_common.events import EventEnvelope

        import ops.handlers as handlers

        env = EventEnvelope(
            event_type="payment.returned", aggregate_type="transaction",
            aggregate_id="txn-77", sequence=3, producer="payments",
            correlation_id="corr-77",
            payload={"transaction_id": "txn-77", "return_reason": "CLOSED"},
        )
        handlers.on_failure_event(env)

        case = FailureCase.objects.get()
        assert case.failure_type == "PAYMENT_RETURNED"
        assert case.subject_ref == "txn-77"
        assert case.correlation_id == "corr-77"

    def test_every_case_source_has_a_handler(self):
        from platform_common.events.dispatcher import handlers_for

        import ops.handlers as handlers

        for event_type in handlers.CASE_SOURCES:
            assert handlers_for(event_type), f"no handler for {event_type}"

    def test_the_critical_events_are_all_covered(self):
        """A ledger invariant breach or a broken audit chain must reach a human."""
        import ops.handlers as handlers

        for critical in ("ledger.invariant_breached", "audit.chain_broken",
                         "outbox.dead", "security.refresh_reuse_detected"):
            assert critical in handlers.CASE_SOURCES


class TestReports:
    def test_service_health_report(self):
        snapshot(service="payments")
        snapshot(service="ledger", reachable=False, error="down")

        report = Report.objects.create(report_type="SERVICE_HEALTH")
        services.generate_report(report.id)

        report.refresh_from_db()
        assert report.status == Report.Status.READY
        statuses = {row["service"]: row["status"] for row in report.result["services"]}
        assert statuses["payments"] == "HEALTHY"
        assert statuses["ledger"] == "UNREACHABLE"

    def test_failure_summary_report(self):
        services.open_case(failure_type="PAYMENT_FAILED",
                           source_service="payments", subject_ref="a")
        services.open_case(failure_type="PAYMENT_RETURNED",
                           source_service="payments", subject_ref="b")

        report = Report.objects.create(report_type="FAILURE_SUMMARY", params={"days": 7})
        services.generate_report(report.id)

        report.refresh_from_db()
        assert report.status == Report.Status.READY
        assert report.result["by_type"] == {"PAYMENT_FAILED": 1, "PAYMENT_RETURNED": 1}
        assert report.result["open"] == 2

    def test_an_unknown_report_type_fails_cleanly(self):
        report = Report.objects.create(report_type="NOT_A_REPORT")
        services.generate_report(report.id)
        report.refresh_from_db()
        assert report.status == Report.Status.FAILED
        assert "unknown report type" in report.result["error"]
