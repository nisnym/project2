"""The rule DSL.

Half of these are security tests. Administrators author rules through an API and
those rules are evaluated on every payment, so the evaluator is a place where a
config feature could become remote code execution. It must be impossible to make
it execute anything.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fraud.rules import (
    FACT_TYPES,
    MAX_DEPTH,
    OPERATORS,
    RuleSyntaxError,
    evaluate,
    sample_vector,
    validate_condition,
)


class TestSecurity:
    """A rule is data. It must never become code."""

    @pytest.mark.parametrize(
        "malicious",
        [
            "__import__('os').system('id')",
            {"fact": "__class__", "op": "eq", "value": "x"},
            {"fact": "amount.__class__", "op": "eq", "value": "x"},
            {"fact": "amount", "op": "eval", "value": "1"},
            {"fact": "amount", "op": "__call__", "value": "1"},
            {"op": "eq", "value": 1},
            {"unknown_key": []},
            ["not", "an", "object"],
            42,
            None,
        ],
    )
    def test_rejects_anything_that_is_not_the_grammar(self, malicious):
        with pytest.raises(RuleSyntaxError):
            validate_condition(malicious)

    def test_no_attribute_traversal(self):
        # Even a real attribute of the dataclass is unreachable unless it is a
        # declared fact.
        with pytest.raises(RuleSyntaxError, match="unknown fact"):
            validate_condition({"fact": "as_dict", "op": "eq", "value": 1})

    def test_operator_set_is_closed(self):
        assert set(OPERATORS) == {
            "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "between"
        }

    def test_depth_is_bounded(self):
        node = {"fact": "amount", "op": "gt", "value": "1"}
        for _ in range(MAX_DEPTH + 2):
            node = {"all": [node]}
        with pytest.raises(RuleSyntaxError, match="nested deeper"):
            validate_condition(node)

    def test_node_count_is_bounded(self):
        wide = {"any": [{"fact": "amount", "op": "gt", "value": str(i)} for i in range(200)]}
        with pytest.raises(RuleSyntaxError, match="more than"):
            validate_condition(wide)


class TestValidation:
    def test_unknown_fact_names_the_valid_ones(self):
        with pytest.raises(RuleSyntaxError, match="Valid facts"):
            validate_condition({"fact": "benef_count_5min", "op": "gt", "value": 3})

    def test_missing_value_is_rejected(self):
        with pytest.raises(RuleSyntaxError, match="missing 'value'"):
            validate_condition({"fact": "amount", "op": "gt"})

    def test_in_requires_a_list(self):
        with pytest.raises(RuleSyntaxError, match="needs a list"):
            validate_condition({"fact": "rail", "op": "in", "value": "INTERNAL"})

    def test_between_requires_two_elements(self):
        with pytest.raises(RuleSyntaxError, match="two-element"):
            validate_condition({"fact": "amount", "op": "between", "value": ["1"]})

    def test_boolean_fact_rejects_non_boolean(self):
        with pytest.raises(RuleSyntaxError, match="boolean fact"):
            validate_condition(
                {"fact": "beneficiary_is_new", "op": "eq", "value": "yes"}
            )

    def test_empty_all_is_rejected(self):
        with pytest.raises(RuleSyntaxError, match="non-empty"):
            validate_condition({"all": []})

    def test_a_valid_condition_passes(self):
        validate_condition(
            {"all": [
                {"fact": "rail", "op": "eq", "value": "INTERNATIONAL"},
                {"fact": "beneficiary_is_new", "op": "eq", "value": True},
                {"any": [
                    {"fact": "amount", "op": "gt", "value": "100000"},
                    {"not": {"fact": "destination_is_high_risk", "op": "eq", "value": False}},
                ]},
            ]}
        )


class TestEvaluation:
    def test_simple_comparison(self):
        condition = {"fact": "amount", "op": "gt", "value": "100000"}
        assert evaluate(condition, sample_vector(amount=Decimal("150000")))
        assert not evaluate(condition, sample_vector(amount=Decimal("50000")))

    def test_amounts_compare_as_decimal_not_float(self):
        """A float comparison here would reintroduce the precision bug the whole
        system avoids."""
        condition = {"fact": "amount", "op": "gt", "value": "0.1"}
        assert evaluate(condition, sample_vector(amount=Decimal("0.2")))
        assert not evaluate(condition, sample_vector(amount=Decimal("0.1")))

    def test_all_requires_every_child(self):
        condition = {"all": [
            {"fact": "rail", "op": "eq", "value": "INTERNATIONAL"},
            {"fact": "amount", "op": "gt", "value": "1000"},
        ]}
        assert evaluate(condition, sample_vector(rail="INTERNATIONAL", amount=Decimal("5000")))
        assert not evaluate(condition, sample_vector(rail="INTERNAL", amount=Decimal("5000")))

    def test_any_requires_one_child(self):
        condition = {"any": [
            {"fact": "device_blocked", "op": "eq", "value": True},
            {"fact": "beneficiary_blacklisted", "op": "eq", "value": True},
        ]}
        assert evaluate(condition, sample_vector(device_blocked=True))
        assert evaluate(condition, sample_vector(beneficiary_blacklisted=True))
        assert not evaluate(condition, sample_vector())

    def test_not_inverts(self):
        condition = {"not": {"fact": "beneficiary_is_new", "op": "eq", "value": True}}
        assert evaluate(condition, sample_vector(beneficiary_is_new=False))
        assert not evaluate(condition, sample_vector(beneficiary_is_new=True))

    def test_in_and_not_in(self):
        assert evaluate(
            {"fact": "destination_country", "op": "in", "value": ["AE", "SG"]},
            sample_vector(destination_country="AE"),
        )
        assert evaluate(
            {"fact": "destination_country", "op": "not_in", "value": ["AE", "SG"]},
            sample_vector(destination_country="IN"),
        )

    def test_between_is_inclusive(self):
        condition = {"fact": "hour_of_day", "op": "between", "value": [1, 4]}
        assert evaluate(condition, sample_vector(hour_of_day=1))
        assert evaluate(condition, sample_vector(hour_of_day=4))
        assert not evaluate(condition, sample_vector(hour_of_day=5))

    def test_the_brief_example_rule(self):
        """International + brand-new beneficiary + large amount + high-risk."""
        condition = {"all": [
            {"fact": "rail", "op": "eq", "value": "INTERNATIONAL"},
            {"fact": "beneficiary_is_new", "op": "eq", "value": True},
            {"fact": "amount", "op": "gt", "value": "100000"},
            {"any": [
                {"fact": "destination_is_high_risk", "op": "eq", "value": True},
                {"fact": "amount_zscore", "op": "gt", "value": 3.0},
            ]},
        ]}
        assert evaluate(condition, sample_vector(
            rail="INTERNATIONAL", beneficiary_is_new=True,
            amount=Decimal("150000"), amount_zscore=4.2,
        ))
        # Same transaction, established beneficiary -> does not fire.
        assert not evaluate(condition, sample_vector(
            rail="INTERNATIONAL", beneficiary_is_new=False,
            amount=Decimal("150000"), amount_zscore=4.2,
        ))


class TestFactCoverage:
    def test_every_vector_field_is_a_usable_fact(self):
        vector = sample_vector()
        for fact in FACT_TYPES:
            assert hasattr(vector, fact)

    def test_the_briefs_named_vectors_all_exist(self):
        """Amount, velocity, location, device, blacklist, high-risk country,
        frequency -- every screening vector the requirements name."""
        required = {
            "amount", "txn_count_5m", "distinct_benef_5m", "country_changed",
            "device_is_new", "device_blocked", "beneficiary_blacklisted",
            "destination_is_high_risk", "beneficiary_is_new", "amount_zscore",
        }
        assert required <= set(FACT_TYPES)
