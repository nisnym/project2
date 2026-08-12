"""The aggregation layer — where every number on screen ultimately comes from."""

from services.transaction_service import analytics


def txn(id, date, amount, category, merchant, customer="cust-x"):
    return {
        "id": id,
        "customerId": customer,
        "date": date,
        "amount": amount,
        "category": category,
        "merchant": merchant,
    }


LEDGER = [
    # June — a complete month
    txn("a1", "2026-06-02", 50000, "income", "Employer"),
    txn("a2", "2026-06-03", -1000, "dining", "Swiggy"),
    txn("a3", "2026-06-20", -2000, "dining", "Toit"),
    txn("a4", "2026-06-14", -799, "subscriptions", "Netflix"),
    # July — a complete month
    txn("b1", "2026-07-02", 50000, "income", "Employer"),
    txn("b2", "2026-07-05", -1500, "dining", "Swiggy"),
    txn("b3", "2026-07-25", -2500, "dining", "Toit"),
    txn("b4", "2026-07-14", -799, "subscriptions", "Netflix"),
    # August — in progress, "today" is the 12th
    txn("c1", "2026-08-01", 50000, "income", "Employer"),
    txn("c2", "2026-08-04", -3000, "dining", "Swiggy"),
    txn("c3", "2026-08-07", -799, "subscriptions", "Netflix"),
]


class TestSigns:
    def test_negative_is_spend_positive_is_income(self):
        assert analytics.spend_of(txn("x", "2026-08-01", -250, "dining", "M")) == 250
        assert analytics.spend_of(txn("x", "2026-08-01", 250, "income", "M")) == 0
        assert analytics.income_of(txn("x", "2026-08-01", 250, "income", "M")) == 250

    def test_a_category_invented_in_the_data_just_works(self):
        rows = LEDGER + [txn("z1", "2026-08-05", -400, "pet-care", "Heads Up For Tails")]
        totals = {c["category"] for c in analytics.category_totals(analytics.in_month(rows, "2026-08"))}
        assert "pet-care" in totals


class TestSummaries:
    def test_month_summary_totals(self):
        summary = analytics.summarize_month(LEDGER, "2026-07")
        assert summary["totalIncome"] == 50000
        assert summary["totalSpend"] == 4799
        assert summary["net"] == 45201
        assert summary["transactionCount"] == 4

    def test_categories_ranked_by_spend_with_shares(self):
        summary = analytics.summarize_month(LEDGER, "2026-07")
        top = summary["byCategory"][0]
        assert top["category"] == "dining"
        assert top["total"] == 4000
        assert top["shareOfSpend"] == 83.4

    def test_typical_uses_median_so_one_off_does_not_set_the_norm(self):
        rows = LEDGER + [txn("d1", "2026-05-09", -90000, "dining", "Wedding caterer")]
        typical = analytics.typical_monthly_spend(rows, "dining", ["2026-07", "2026-06", "2026-05"])
        assert typical == 4000  # the ₹90,000 month is not "typical"


class TestRecurringDetection:
    def test_flat_monthly_charge_is_recurring(self):
        found = {r["merchant"] for r in analytics.detect_recurring(LEDGER, ["2026-08", "2026-07", "2026-06"])}
        assert "Netflix" in found

    def test_a_merchant_you_simply_visit_often_is_not(self):
        found = {r["merchant"] for r in analytics.detect_recurring(LEDGER, ["2026-08", "2026-07", "2026-06"])}
        assert "Swiggy" not in found  # amounts vary
        assert "Toit" not in found

    def test_two_similar_but_unequal_charges_are_not_a_subscription(self):
        rows = [
            txn("g1", "2026-06-12", -2450, "groceries", "DMart"),
            txn("g2", "2026-07-12", -2550, "groceries", "DMart"),
        ]
        assert analytics.detect_recurring(rows, ["2026-07", "2026-06"]) == []


class TestProjection:
    def test_regular_charge_already_paid_is_not_expected_again(self):
        # Netflix billed on the 7th of August. It billed on the 14th in June
        # and July, so a naive "what happened after the 12th" would predict a
        # second ₹799 that will never come.
        expected, months = analytics.expected_remaining(LEDGER, "subscriptions", "2026-08", day=12)
        assert expected == 0.0
        assert months == ["2026-07", "2026-06"]

    def test_irregular_spending_is_still_projected(self):
        # Toit lands after the 12th in both prior months: ₹2,000 and ₹2,500.
        expected, months = analytics.expected_remaining(LEDGER, "dining", "2026-08", day=12)
        assert expected == 2250.0
        assert months == ["2026-07", "2026-06"]

    def test_no_history_means_no_projection_and_the_caller_is_told(self):
        rows = [txn("n1", "2026-08-03", -500, "dining", "Swiggy")]
        expected, months = analytics.expected_remaining(rows, "dining", "2026-08", day=12)
        assert expected == 0.0
        assert months == []  # signals the caller to fall back to a run-rate

    def test_spend_after_day_ignores_earlier_transactions(self):
        assert analytics.spend_after_day(LEDGER, "dining", "2026-07", day=12) == 2500
        assert analytics.spend_after_day(LEDGER, "dining", "2026-07", day=26) == 0
