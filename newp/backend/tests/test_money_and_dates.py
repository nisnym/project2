"""Rupee formatting and the month maths every projection depends on."""

from shared import dates
from shared.money import format_inr, pct, round_money


class TestRupeeFormatting:
    def test_lakh_grouping_not_thousands(self):
        assert format_inr(145000) == "₹1,45,000"
        assert format_inr(1234567) == "₹12,34,567"

    def test_small_amounts_are_unchanged(self):
        assert format_inr(999) == "₹999"
        assert format_inr(0) == "₹0"

    def test_negatives_and_signs(self):
        assert format_inr(-2500) == "-₹2,500"
        assert format_inr(2500, signed=True) == "+₹2,500"

    def test_paise_only_when_asked_for(self):
        assert format_inr(1234.56) == "₹1,235"
        assert format_inr(1234.56, decimals=True) == "₹1,234.56"

    def test_percentage_survives_a_zero_denominator(self):
        assert pct(50, 200) == 25.0
        assert pct(50, 0) == 0.0

    def test_rounding_is_to_paise(self):
        assert round_money(10.005) == 10.0 or round_money(10.005) == 10.01


class TestMonthMaths:
    def test_previous_months_crosses_the_year(self):
        assert dates.previous_months("2026-02", 3) == ["2026-01", "2025-12", "2025-11"]

    def test_month_bounds(self):
        assert dates.month_bounds("2026-02") == ("2026-02-01", "2026-02-28")
        assert dates.month_bounds("2026-08") == ("2026-08-01", "2026-08-31")

    def test_a_finished_month_is_fully_elapsed(self):
        assert dates.elapsed_days("2026-06") == 30
        assert dates.month_progress("2026-06") == 1.0

    def test_a_future_month_has_not_started(self):
        assert dates.elapsed_days("2027-01") == 0

    def test_the_demo_clock_pins_today(self):
        # DEMO_TODAY defaults to 2026-08-12, mid-way through the seeded month,
        # so month-to-date figures are reproducible whenever this is run.
        assert dates.current_month() == "2026-08"
        assert dates.elapsed_days("2026-08") == 12

    def test_month_label_is_human(self):
        assert dates.month_label("2026-08") == "August 2026"
