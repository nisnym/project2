"""Money is the foundation everything else stands on: if it rounds wrong or
accepts a float, every ledger invariant downstream is meaningless."""

from __future__ import annotations

from decimal import Decimal

import pytest

from platform_common.money import CurrencyMismatch, InvalidMoney, Money


class TestConstruction:
    def test_rejects_float(self):
        # The single most important rule in the module.
        with pytest.raises(InvalidMoney, match="float is banned"):
            Money(150.75, "INR")

    def test_rejects_bool(self):
        # bool is a subclass of int; without an explicit guard True becomes 1.00.
        with pytest.raises(InvalidMoney):
            Money(True, "INR")

    @pytest.mark.parametrize("code", ["inr", "IN", "INRR", "", "123", None])
    def test_rejects_bad_currency(self, code):
        with pytest.raises(InvalidMoney):
            Money(Decimal("1"), code)

    def test_accepts_string_and_int(self):
        assert Money("150.5", "INR").amount == Decimal("150.5000")
        assert Money(150, "INR").amount == Decimal("150.0000")

    def test_rejects_nan_and_infinity(self):
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with pytest.raises(InvalidMoney, match="finite"):
                Money(bad, "INR")

    def test_quantises_to_four_places(self):
        assert Money(Decimal("1.23456789"), "INR").amount == Decimal("1.2346")

    def test_uses_bankers_rounding(self):
        # ROUND_HALF_EVEN: exact halves go to the even digit, so rounding does
        # not introduce a systematic upward bias over millions of transactions.
        assert Money(Decimal("1.00005"), "INR").amount == Decimal("1.0000")
        assert Money(Decimal("1.00015"), "INR").amount == Decimal("1.0002")

    def test_is_immutable(self):
        money = Money(Decimal("10"), "INR")
        with pytest.raises(Exception):
            money.amount = Decimal("20")


class TestArithmetic:
    def test_add_and_subtract(self):
        assert Money("10.50", "INR") + Money("4.50", "INR") == Money("15.00", "INR")
        assert Money("10.50", "INR") - Money("4.50", "INR") == Money("6.00", "INR")

    def test_currency_mismatch_raises_rather_than_coercing(self):
        with pytest.raises(CurrencyMismatch):
            Money("10", "INR") + Money("10", "USD")
        with pytest.raises(CurrencyMismatch):
            Money("10", "INR") < Money("10", "USD")

    def test_refuses_float_multiplication(self):
        with pytest.raises(InvalidMoney):
            Money("10", "INR") * 1.5

    def test_multiplication_by_decimal(self):
        assert Money("100", "INR") * Decimal("1.5") == Money("150", "INR")

    def test_no_precision_loss_across_many_additions(self):
        # 0.1 + 0.2 == 0.3 exactly, which is the whole reason for Decimal.
        total = Money.zero("INR")
        for _ in range(10):
            total = total + Money("0.10", "INR")
        assert total == Money("1.00", "INR")


class TestSerialisation:
    def test_round_trip_through_the_wire_format(self):
        original = Money(Decimal("150000.1234"), "INR")
        assert original.to_dict() == {"amount": "150000.1234", "currency": "INR"}
        assert Money.parse(original.to_dict()) == original

    def test_amount_is_a_string_not_a_number(self):
        # A JSON number would be parsed as a float by most clients.
        assert isinstance(Money("1", "INR").to_dict()["amount"], str)

    def test_parse_none_passes_through(self):
        assert Money.parse(None) is None

    @pytest.mark.parametrize("bad", [{}, {"amount": "1"}, {"currency": "INR"}, "10 INR"])
    def test_parse_rejects_malformed(self, bad):
        with pytest.raises(InvalidMoney):
            Money.parse(bad)


class TestRailFormatting:
    def test_rounds_to_currency_minor_units(self):
        assert Money("100.5678", "INR").for_rail() == Decimal("100.57")
        assert Money("100.5678", "JPY").for_rail() == Decimal("101")   # zero-decimal
        assert Money("100.5678", "KWD").for_rail() == Decimal("100.568")  # three-decimal

    def test_unknown_currency_defaults_to_two_places(self):
        assert Money("100.5678", "XYZ").for_rail() == Decimal("100.57")
