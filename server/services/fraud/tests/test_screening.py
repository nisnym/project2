"""Screening: decisions, the feature read model, cases, and the tuning loop."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from fraud.models import (
    AccountProfile,
    CaseStatus,
    Decision,
    FraudCase,
    FraudDecision,
    KnownBeneficiary,
    Resolution,
    RuleMode,
    RuleStat,
    ScreenedTxn,
)
from fraud.services import (
    approve_case,
    assemble_features,
    dry_run_rule,
    reject_case,
    screen,
    update_profile,
)
from platform_common.errors import Conflict, NotFound

pytestmark = pytest.mark.django_db


class TestDecisions:
    def test_clean_transaction_is_allowed(self, thresholds, screen_request):
        result = screen(screen_request(amount=Decimal("1000")))
        assert result.decision == Decision.ALLOW
        assert result.score == 0
        assert result.case_id is None

    def test_score_below_threshold_allows(self, thresholds, rule_factory, screen_request):
        rule_factory(weight=30, condition={"fact": "amount", "op": "gt", "value": "500"})
        result = screen(screen_request(amount=Decimal("1000")))
        assert result.decision == Decision.ALLOW
        assert result.score == 30

    def test_mid_score_goes_to_review(self, thresholds, rule_factory, screen_request):
        rule_factory(weight=45, condition={"fact": "amount", "op": "gt", "value": "500"})
        result = screen(screen_request(amount=Decimal("1000")))
        assert result.decision == Decision.REVIEW
        assert result.case_id is not None

    def test_high_score_blocks(self, thresholds, rule_factory, screen_request):
        rule_factory(weight=40, condition={"fact": "amount", "op": "gt", "value": "500"})
        rule_factory(weight=40, condition={"fact": "rail", "op": "eq", "value": "INTERNAL"})
        result = screen(screen_request(amount=Decimal("1000")))
        assert result.score == 80
        assert result.decision == Decision.BLOCK

    def test_hard_block_beats_a_low_score(self, thresholds, rule_factory, screen_request, blacklisted):
        """A blacklisted beneficiary is not something a low score can outvote."""
        fingerprint = "sha256:bad-payee"
        blacklisted(fingerprint)
        rule_factory(
            weight=5, hard_block=True, reason_code="R006",
            condition={"fact": "beneficiary_blacklisted", "op": "eq", "value": True},
        )
        result = screen(screen_request(beneficiary_fingerprint=fingerprint))
        assert result.decision == Decision.BLOCK
        assert result.score == 5           # low score...
        assert "R006" in result.reason_codes  # ...but hard block wins

    def test_score_is_capped_at_100(self, thresholds, rule_factory, screen_request):
        for _ in range(5):
            rule_factory(weight=40, condition={"fact": "amount", "op": "gt", "value": "1"})
        result = screen(screen_request(amount=Decimal("1000")))
        assert result.score == 100

    def test_every_transaction_gets_a_decision_row(self, thresholds, screen_request):
        request = screen_request()
        screen(request)
        assert FraudDecision.objects.filter(txn_ref=request.txn_ref).exists()

    def test_latency_is_recorded_on_every_decision(self, thresholds, screen_request):
        result = screen(screen_request())
        assert result.latency_ms >= 0
        assert FraudDecision.objects.get().latency_ms == result.latency_ms


class TestIdempotency:
    def test_rescreening_returns_the_original_decision(self, thresholds, rule_factory, screen_request):
        """payments-svc retrying must not produce a second decision or a second
        analyst case."""
        rule_factory(weight=45, condition={"fact": "amount", "op": "gt", "value": "500"})
        request = screen_request(amount=Decimal("1000"))

        first = screen(request)
        second = screen(request)

        assert first.decision_id == second.decision_id
        assert first.case_id == second.case_id
        assert FraudDecision.objects.count() == 1
        assert FraudCase.objects.count() == 1


class TestShadowMode:
    def test_shadow_rules_do_not_affect_the_decision(self, thresholds, rule_factory, screen_request):
        """The mechanism that lets a fraud engine be changed without incident."""
        rule_factory(
            weight=90, mode=RuleMode.SHADOW, reason_code="R017",
            condition={"fact": "amount", "op": "gt", "value": "500"},
        )
        result = screen(screen_request(amount=Decimal("1000")))

        assert result.decision == Decision.ALLOW   # would have blocked if active
        assert result.score == 0
        record = FraudDecision.objects.get()
        assert record.shadow_codes == ["R017"]     # but it is recorded

    def test_disabled_rules_are_not_evaluated_at_all(self, thresholds, rule_factory, screen_request):
        rule_factory(
            weight=90, mode=RuleMode.DISABLED,
            condition={"fact": "amount", "op": "gt", "value": "500"},
        )
        result = screen(screen_request(amount=Decimal("1000")))
        assert result.score == 0
        assert FraudDecision.objects.get().shadow_codes == []

    def test_shadow_firings_are_counted_separately(self, thresholds, rule_factory, screen_request):
        rule = rule_factory(
            weight=90, mode=RuleMode.SHADOW,
            condition={"fact": "amount", "op": "gt", "value": "500"},
        )
        screen(screen_request(amount=Decimal("1000")))
        stat = RuleStat.objects.get(rule=rule)
        assert stat.shadow_fired_count == 1
        assert stat.fired_count == 0


class TestResilience:
    def test_a_broken_rule_does_not_fail_the_screening(self, thresholds, rule_factory, screen_request):
        """A config mistake must not become a payments outage."""
        # Bypass serialiser validation to simulate a rule corrupted in the DB.
        from fraud.models import Rule
        from fraud.services import refresh_caches

        rule = rule_factory(weight=30, condition={"fact": "amount", "op": "gt", "value": "1"})
        Rule.objects.filter(pk=rule.pk).update(condition={"fact": "nonexistent", "op": "gt", "value": 1})
        refresh_caches()

        result = screen(screen_request(amount=Decimal("1000")))
        assert result.decision == Decision.ALLOW   # screened successfully
        assert result.score == 0                   # the broken rule contributed nothing


class TestFeatureAssembly:
    def test_first_transaction_is_flagged(self, thresholds, screen_request):
        vector = assemble_features(screen_request())
        assert vector.is_first_txn
        assert vector.account_txn_count == 0

    def test_zscore_is_neutral_without_history(self, thresholds, screen_request):
        """Otherwise every first transaction trips every anomaly rule."""
        vector = assemble_features(screen_request(amount=Decimal("999999")))
        assert vector.amount_zscore == 0.0

    def test_velocity_counts_recent_transactions(self, thresholds, screen_request):
        account = str(uuid.uuid4())
        for _ in range(3):
            request = screen_request(account_ref=account, amount=Decimal("100"))
            screen(request)
            update_profile({
                "txn_ref": request.txn_ref, "account_ref": account,
                "amount": "100", "currency": "INR", "rail": "INTERNAL",
                "benef_fingerprint": "", "country": "", "device_hash": "",
            })

        vector = assemble_features(screen_request(account_ref=account))
        assert vector.txn_count_5m == 3
        assert vector.txn_sum_5m == Decimal("300.0000")

    def test_old_transactions_fall_out_of_the_window(self, thresholds, screen_request):
        account = str(uuid.uuid4())
        ScreenedTxn.objects.create(
            txn_ref=uuid.uuid4(), account_ref=account, amount=Decimal("100"),
            currency="INR", rail="INTERNAL",
            created_at=timezone.now() - timedelta(minutes=10),
        )
        vector = assemble_features(screen_request(account_ref=account))
        assert vector.txn_count_5m == 0
        assert vector.txn_count_24h == 1

    def test_known_beneficiary_is_not_new(self, thresholds, screen_request):
        account, fingerprint = str(uuid.uuid4()), "sha256:known"
        KnownBeneficiary.objects.create(
            account_ref=account, fingerprint=fingerprint, first_seen=timezone.now()
        )
        vector = assemble_features(
            screen_request(account_ref=account, beneficiary_fingerprint=fingerprint)
        )
        assert not vector.beneficiary_is_new

    def test_country_change_is_detected(self, thresholds, screen_request):
        account = str(uuid.uuid4())
        AccountProfile.objects.create(account_ref=account, txn_count=5, last_country="IN")
        vector = assemble_features(
            screen_request(account_ref=account, beneficiary_country="AE")
        )
        assert vector.country_changed


class TestProfileMaintenance:
    def test_welford_tracks_mean_and_stddev(self, thresholds):
        account = str(uuid.uuid4())
        for amount in ["100", "200", "300", "400", "500"]:
            update_profile({
                "txn_ref": str(uuid.uuid4()), "account_ref": account,
                "amount": amount, "currency": "INR", "rail": "INTERNAL",
                "benef_fingerprint": "", "country": "", "device_hash": "",
            })
        profile = AccountProfile.objects.get(account_ref=account)
        assert profile.txn_count == 5
        assert profile.mean_amount == Decimal("300.0000")
        assert profile.max_amount == Decimal("500.0000")
        assert abs(float(profile.stddev) - 158.11) < 0.1   # sample stddev

    def test_update_is_idempotent(self, thresholds):
        account, txn = str(uuid.uuid4()), str(uuid.uuid4())
        payload = {
            "txn_ref": txn, "account_ref": account, "amount": "100",
            "currency": "INR", "rail": "INTERNAL",
            "benef_fingerprint": "", "country": "", "device_hash": "",
        }
        update_profile(payload)
        update_profile(payload)   # Q2 redelivery

        profile = AccountProfile.objects.get(account_ref=account)
        assert profile.txn_count == 1   # counted once, not twice

    def test_beneficiary_becomes_known(self, thresholds):
        account, fingerprint = str(uuid.uuid4()), "sha256:payee"
        update_profile({
            "txn_ref": str(uuid.uuid4()), "account_ref": account, "amount": "100",
            "currency": "INR", "rail": "DOMESTIC",
            "benef_fingerprint": fingerprint, "country": "IN", "device_hash": "",
        })
        known = KnownBeneficiary.objects.get(account_ref=account, fingerprint=fingerprint)
        assert known.txn_count == 1
        assert known.total_amount == Decimal("100.0000")


class TestCaseWorkflow:
    def _reviewed(self, thresholds, rule_factory, screen_request):
        rule_factory(weight=45, reason_code="R005",
                     condition={"fact": "amount", "op": "gt", "value": "500"})
        result = screen(screen_request(amount=Decimal("1000")))
        return FraudCase.objects.get(pk=result.case_id)

    def test_approve_marks_false_positive_and_updates_precision(
        self, thresholds, rule_factory, screen_request
    ):
        case = self._reviewed(thresholds, rule_factory, screen_request)
        approve_case(case.id, analyst_id="analyst-1", note="Customer confirmed payee")

        case.refresh_from_db()
        assert case.status == CaseStatus.APPROVED
        assert case.resolution == Resolution.FALSE_POSITIVE

        stat = RuleStat.objects.get(rule__reason_code="R005")
        assert stat.false_positive == 1
        assert stat.precision == 0.0

    def test_reject_marks_confirmed_fraud(self, thresholds, rule_factory, screen_request):
        case = self._reviewed(thresholds, rule_factory, screen_request)
        reject_case(case.id, analyst_id="analyst-1", note="Mule account")

        case.refresh_from_db()
        assert case.status == CaseStatus.REJECTED
        assert case.resolution == Resolution.CONFIRMED_FRAUD
        assert RuleStat.objects.get(rule__reason_code="R005").confirmed_fraud == 1

    def test_second_analyst_cannot_re_resolve(self, thresholds, rule_factory, screen_request):
        """Two analysts opening one case is normal; both resolving it is not."""
        case = self._reviewed(thresholds, rule_factory, screen_request)
        approve_case(case.id, analyst_id="analyst-1", note="ok")
        with pytest.raises(Conflict, match="already resolved"):
            reject_case(case.id, analyst_id="analyst-2", note="no")

    def test_unknown_case_raises(self):
        with pytest.raises(NotFound):
            approve_case(uuid.uuid4(), analyst_id="a", note="")

    def test_precision_drives_the_tuning_loop(self, thresholds, rule_factory, screen_request):
        """Three false positives to one real catch -> precision 0.25, which is
        what tells an admin to lower the weight."""
        rule_factory(weight=45, reason_code="R005",
                     condition={"fact": "amount", "op": "gt", "value": "500"})
        for index in range(4):
            result = screen(screen_request(amount=Decimal("1000")))
            case = FraudCase.objects.get(pk=result.case_id)
            if index == 0:
                reject_case(case.id, analyst_id="a", note="fraud")
            else:
                approve_case(case.id, analyst_id="a", note="fine")

        stat = RuleStat.objects.get(rule__reason_code="R005")
        assert stat.confirmed_fraud == 1
        assert stat.false_positive == 3
        assert stat.precision == 0.25


class TestDryRun:
    def test_replays_a_proposed_rule_against_history(self, thresholds, screen_request):
        """Possible only because FraudDecision.features stores the exact vector."""
        for amount in ["100", "1000", "200000"]:
            screen(screen_request(amount=Decimal(amount)))

        result = dry_run_rule({"fact": "amount", "op": "gt", "value": "500"})
        assert result["evaluated"] == 3
        assert result["would_fire"] == 2

    def test_reports_overlap_with_confirmed_fraud(self, thresholds, rule_factory, screen_request):
        rule_factory(weight=45, reason_code="R005",
                     condition={"fact": "amount", "op": "gt", "value": "500"})
        result = screen(screen_request(amount=Decimal("200000")))
        reject_case(FraudCase.objects.get(pk=result.case_id).id, analyst_id="a", note="")

        outcome = dry_run_rule({"fact": "amount", "op": "gt", "value": "100000"})
        assert outcome["would_fire"] == 1
        assert outcome["overlap_with_confirmed_fraud"] == 1
        assert outcome["estimated_precision"] == 1.0


class TestLatencyBudget:
    def test_p99_screening_latency_is_within_budget(self, thresholds, rule_factory, screen_request):
        """The claim the whole fraud design rests on: p99 < 50 ms.

        Recorded on every decision row, so a regression shows up in production
        metrics too, not only here.
        """
        for index in range(12):
            rule_factory(
                code=f"RPERF{index:02d}", weight=3,
                condition={"all": [
                    {"fact": "amount", "op": "gt", "value": str(index * 100)},
                    {"any": [
                        {"fact": "rail", "op": "in", "value": ["INTERNAL", "DOMESTIC"]},
                        {"fact": "beneficiary_is_new", "op": "eq", "value": True},
                    ]},
                ]},
            )

        account = str(uuid.uuid4())
        latencies = []
        for _ in range(100):
            result = screen(screen_request(account_ref=account, amount=Decimal("1200")))
            latencies.append(result.latency_ms)

        latencies.sort()
        p99 = latencies[int(len(latencies) * 0.99)]
        assert p99 < 50, f"p99 was {p99}ms; budget is 50ms"
