"""The advice engine.

The rule that matters most here isn't any single insight — it's that no
insight ever ships without a reason and traceable data points.
"""

import pytest

from services.insight_service import rules
from services.insight_service.rules import Context
from shared.money import format_inr

CUSTOMER = {
    "id": "cust-t",
    "name": "Test Person",
    "segment": "salaried",
    "monthlyIncome": 100000,
    "incomeStability": "regular",
    "goals": [
        {"id": "g1", "label": "Emergency fund", "targetAmount": 300000, "savedAmount": 100000, "targetDate": "2027-08-31"},
        {"id": "g2", "label": "New bike", "targetAmount": 200000, "savedAmount": 0, "targetDate": "2027-02-28"},
    ],
}

ACCOUNT = {"id": "acc-t", "customerId": "cust-t", "type": "savings", "balance": 50000}


def summary(month, spend, income, categories, count=10):
    return {
        "month": month,
        "totalSpend": spend,
        "totalIncome": income,
        "net": income - spend,
        "transactionCount": count,
        "byCategory": [
            {"category": c, "total": t, "count": 3, "shareOfSpend": round(t / spend * 100, 1) if spend else 0}
            for c, t in categories.items()
        ],
    }


def budget(category, limit, spent, projected, status, reason=None):
    # Mirrors what budget-service actually sends, reason included — a fixture
    # with a placeholder reason would quietly weaken every assertion below.
    reason = reason or (
        f"{format_inr(spent)} spent against a {format_inr(limit)} limit, "
        f"heading for {format_inr(projected)}."
    )
    return {
        "customerId": "cust-t",
        "category": category,
        "monthlyLimit": limit,
        "spentSoFar": spent,
        "remaining": limit - spent,
        "pctUsed": round(spent / limit * 100, 1),
        "status": status,
        "projectedSpend": projected,
        "reason": reason,
    }


@pytest.fixture
def ctx():
    return Context(
        customer=CUSTOMER,
        account=ACCOUNT,
        month="2026-08",
        summary=summary("2026-08", 20000, 100000, {"dining": 12000, "groceries": 8000}),
        prior_summaries={
            "2026-07": summary("2026-07", 60000, 100000, {"dining": 6000, "groceries": 9000}),
            "2026-06": summary("2026-06", 55000, 100000, {"dining": 5000, "groceries": 8500}),
        },
        monthly=[
            {"month": "2026-08", "spend": 20000, "income": 100000, "net": 80000, "count": 10},
            {"month": "2026-07", "spend": 60000, "income": 100000, "net": 40000, "count": 20},
            {"month": "2026-06", "spend": 55000, "income": 100000, "net": 45000, "count": 20},
        ],
        budgets=[
            budget("dining", 8000, 12000, 14000, "over"),
            budget("groceries", 12000, 8000, 11000, "on-track"),
        ],
        recurring={
            "monthsExamined": ["2026-08", "2026-07", "2026-06"],
            "monthlyTotal": 35029,
            "recurring": [
                {"merchant": "Landlord", "category": "rent", "averageAmount": 30000,
                 "occurrences": 3, "transactionIds": ["t2"], "monthsSeen": []},
                {"merchant": "Adobe Creative Cloud", "category": "subscriptions", "averageAmount": 4230,
                 "occurrences": 3, "transactionIds": ["t3"], "monthsSeen": []},
                {"merchant": "Netflix", "category": "subscriptions", "averageAmount": 799,
                 "occurrences": 3, "transactionIds": ["t1"], "monthsSeen": []},
            ],
        },
        transactions=[
            {"id": "t10", "customerId": "cust-t", "date": "2026-08-04", "amount": -7000, "category": "dining", "merchant": "Toit"},
            {"id": "t11", "customerId": "cust-t", "date": "2026-08-06", "amount": -5000, "category": "dining", "merchant": "Swiggy"},
            {"id": "t12", "customerId": "cust-t", "date": "2026-08-01", "amount": 100000, "category": "income", "merchant": "Employer"},
        ],
        all_transactions=[{"id": f"t{i}"} for i in range(40)],
    )


class TestTheHouseRules:
    def test_every_insight_has_a_reason(self, ctx):
        found = rules.run_all(ctx)
        assert found
        for insight in found:
            assert insight.reason.strip(), f"{insight.id} shipped without a reason"

    def test_reasons_quote_real_rupee_figures(self, ctx):
        for insight in rules.run_all(ctx):
            assert "₹" in insight.reason, f"{insight.id} gives no figures"

    def test_data_points_trace_back_to_records(self, ctx):
        breach = next(i for i in rules.run_all(ctx) if i.type == "budget_breach")
        sources = [p.source for p in breach.dataPoints if p.source]
        assert any(s.startswith("txn:") for s in sources)
        assert any(s.startswith("budget:") for s in sources)

    def test_worst_first(self, ctx):
        severities = [i.severity for i in rules.run_all(ctx)]
        rank = {"critical": 0, "warning": 1, "info": 2, "positive": 3}
        assert severities == sorted(severities, key=lambda s: rank[s])


class TestIndividualRules:
    def test_breach_reports_the_overshoot(self, ctx):
        insight = rules.budget_breach(ctx)[0]
        assert insight.severity == "critical"
        assert insight.impact == 4000
        assert "₹4,000" in insight.headline

    def test_spike_is_measured_against_the_person_not_a_benchmark(self, ctx):
        spike = next(i for i in rules.category_spike(ctx) if i.category == "dining")
        assert "₹12,000" in spike.reason  # this month
        assert "₹5,500" in spike.reason   # their own median of June/July

    def test_a_single_large_purchase_is_called_a_one_off(self, ctx):
        ctx.summary = summary("2026-08", 40000, 100000, {"shopping": 40000})
        ctx.prior_summaries["2026-07"]["byCategory"].append(
            {"category": "shopping", "total": 3000, "count": 1, "shareOfSpend": 5}
        )
        ctx.prior_summaries["2026-06"]["byCategory"].append(
            {"category": "shopping", "total": 3000, "count": 1, "shareOfSpend": 5}
        )
        ctx.transactions.append(
            {"id": "t99", "customerId": "cust-t", "date": "2026-08-09", "amount": -38000,
             "category": "shopping", "merchant": "Croma"}
        )
        spike = next(i for i in rules.category_spike(ctx) if i.category == "shopping")
        assert spike.severity == "info"  # not an alarm
        assert "one-off" in spike.reason

    def test_rent_is_never_called_a_cancellable_subscription(self, ctx):
        insight = rules.subscription_creep(ctx)[0]
        assert "Landlord" not in insight.reason
        assert "Netflix" in insight.reason
        # ₹30,000 of rent is excluded from both the total and the annual figure.
        assert insight.impact == (4230 + 799) * 12
        assert "commitments, not subscriptions" in insight.reason

    def test_goals_are_funded_in_order_not_from_the_same_rupees(self, ctx):
        # ₹40,000 kept last month. The bike needs ~₹33,333/month and has the
        # nearer deadline; the emergency fund must then compete for what's left.
        found = rules.goal_progress(ctx)
        bike = next(i for i in found if "bike" in i.headline.lower())
        fund = next(i for i in found if "Emergency" in i.headline)
        assert bike.severity == "positive"
        assert fund.severity == "warning"
        assert "already spoken for" in fund.reason

    def test_thin_history_says_so_instead_of_guessing(self, ctx):
        ctx.all_transactions = [{"id": "t1"}, {"id": "t2"}]
        insight = rules.thin_history(ctx)[0]
        assert insight.type == "insufficient_data"
        assert "guess" in insight.reason

    def test_a_broken_rule_cannot_take_the_advisor_down(self, ctx, monkeypatch):
        def exploding_rule(_):
            raise ValueError("boom")

        monkeypatch.setattr(rules, "ALL_RULES", [exploding_rule, rules.budget_breach])
        found = rules.run_all(ctx)
        assert [i.type for i in found] == ["budget_breach"]


class TestHealthScore:
    def test_score_is_bounded_and_banded(self, ctx):
        health = rules.health_score(ctx)
        assert 0 <= health.score <= 100
        assert health.band in {"strong", "steady", "stretched", "strained"}

    def test_every_component_explains_itself(self, ctx):
        health = rules.health_score(ctx)
        assert len(health.components) == 4
        assert sum(c.weight for c in health.components) == pytest.approx(1.0)
        for component in health.components:
            assert component.reason.strip()

    def test_breached_budgets_pull_the_score_down(self, ctx):
        with_breach = rules.health_score(ctx).score
        ctx.budgets = [budget("dining", 8000, 4000, 7000, "under")]
        assert rules.health_score(ctx).score > with_breach
