"""The rule DSL and its evaluator.

**This is a security boundary.** Rules are authored by administrators through an
API and evaluated on every payment. An administrator who can configure rules must
not thereby obtain code execution, so there is no `eval`, no `exec`, no
`pickle`, and no attribute traversal anywhere in this module. A condition is a
JSON tree walked by a whitelisted interpreter: unknown fact, unknown operator, or
excessive depth is rejected -- at *save* time, so a malformed rule can never
reach the hot path.

Grammar::

    node   := {"all": [node, ...]}
            | {"any": [node, ...]}
            | {"not": node}
            | {"fact": <name>, "op": <operator>, "value": <literal>}

Example::

    {"all": [
      {"fact": "rail",               "op": "eq", "value": "INTERNATIONAL"},
      {"fact": "beneficiary_is_new", "op": "eq", "value": true},
      {"fact": "amount",             "op": "gt", "value": "100000"},
      {"any": [
        {"fact": "destination_is_high_risk", "op": "eq", "value": true},
        {"fact": "amount_zscore",            "op": "gt", "value": 3.0}
      ]}
    ]}
"""

from __future__ import annotations

import dataclasses
import operator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

__all__ = [
    "FeatureVector",
    "evaluate",
    "validate_condition",
    "RuleSyntaxError",
    "FACT_TYPES",
    "OPERATORS",
    "MAX_DEPTH",
]

MAX_DEPTH = 6
MAX_NODES = 60


class RuleSyntaxError(ValueError):
    """A condition that cannot be safely evaluated."""


@dataclass(frozen=True)
class FeatureVector:
    """Everything a rule may reason about. Adding a field here is what makes a
    new fact available to administrators -- there is no other way in."""

    # transaction
    amount: Decimal
    currency: str
    rail: str
    txn_type: str
    hour_of_day: int

    # amount anomaly
    amount_zscore: float
    amount_vs_mean_ratio: float
    amount_vs_max_ratio: float

    # velocity
    txn_count_5m: int
    txn_sum_5m: Decimal
    txn_count_1h: int
    txn_count_24h: int
    txn_sum_24h: Decimal
    distinct_benef_5m: int
    distinct_benef_24h: int

    # beneficiary
    beneficiary_is_new: bool
    beneficiary_age_hours: float
    beneficiary_in_cooling_off: bool
    beneficiary_blacklisted: bool
    beneficiary_txn_count: int

    # geography / device
    destination_country: str
    destination_is_high_risk: bool
    country_changed: bool
    device_is_new: bool
    device_blocked: bool

    # account context
    is_first_txn: bool
    account_age_days: int
    account_txn_count: int

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


FACT_TYPES: dict[str, type] = {
    field.name: field.type for field in dataclasses.fields(FeatureVector)
}

# The entire allowed vocabulary. Anything not here is a syntax error.
OPERATORS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "between": lambda a, b: b[0] <= a <= b[1],
}


def _coerce(value, fact: str):
    """Coerce a JSON literal to the fact's Python type.

    JSON has no Decimal, so amounts arrive as strings (or numbers, which we
    route through str to avoid binary float error). Comparing a Decimal against
    a float would silently reintroduce the precision problem the rest of the
    system works to avoid.
    """
    declared = FACT_TYPES.get(fact)

    if declared in (Decimal, "Decimal"):
        if isinstance(value, (list, tuple)):
            return [_coerce(item, fact) for item in value]
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise RuleSyntaxError(f"{value!r} is not a valid amount for {fact!r}") from exc

    if declared in (bool, "bool"):
        if not isinstance(value, bool):
            raise RuleSyntaxError(f"{fact!r} is a boolean fact; got {value!r}")
        return value

    if declared in (int, "int"):
        if isinstance(value, (list, tuple)):
            return [_coerce(item, fact) for item in value]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise RuleSyntaxError(f"{fact!r} is numeric; got {value!r}")
        return int(value)

    if declared in (float, "float"):
        if isinstance(value, (list, tuple)):
            return [_coerce(item, fact) for item in value]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise RuleSyntaxError(f"{fact!r} is numeric; got {value!r}")
        return float(value)

    return value


def _walk(node, vector: FeatureVector | None, depth: int, counter: list[int]) -> bool:
    """Evaluate (vector given) or validate (vector None) one node."""
    counter[0] += 1
    if depth > MAX_DEPTH:
        raise RuleSyntaxError(f"condition nested deeper than {MAX_DEPTH}")
    if counter[0] > MAX_NODES:
        raise RuleSyntaxError(f"condition has more than {MAX_NODES} nodes")
    if not isinstance(node, dict):
        raise RuleSyntaxError(f"expected an object, got {type(node).__name__}")

    if "all" in node:
        children = node["all"]
        if not isinstance(children, list) or not children:
            raise RuleSyntaxError("'all' needs a non-empty list")
        results = [_walk(child, vector, depth + 1, counter) for child in children]
        return all(results)

    if "any" in node:
        children = node["any"]
        if not isinstance(children, list) or not children:
            raise RuleSyntaxError("'any' needs a non-empty list")
        results = [_walk(child, vector, depth + 1, counter) for child in children]
        return any(results)

    if "not" in node:
        return not _walk(node["not"], vector, depth + 1, counter)

    if "fact" not in node:
        raise RuleSyntaxError(
            f"node must be one of all/any/not/fact; got keys {sorted(node)}"
        )

    fact = node["fact"]
    op_name = node.get("op")

    if fact not in FACT_TYPES:
        raise RuleSyntaxError(
            f"unknown fact {fact!r}. Valid facts: {', '.join(sorted(FACT_TYPES))}"
        )
    if op_name not in OPERATORS:
        raise RuleSyntaxError(
            f"unknown operator {op_name!r}. Valid operators: {', '.join(sorted(OPERATORS))}"
        )
    if "value" not in node:
        raise RuleSyntaxError(f"rule on {fact!r} is missing 'value'")

    raw = node["value"]
    if op_name in ("in", "not_in") and not isinstance(raw, list):
        raise RuleSyntaxError(f"operator {op_name!r} needs a list value")
    if op_name == "between" and (not isinstance(raw, list) or len(raw) != 2):
        raise RuleSyntaxError("operator 'between' needs a two-element list")

    expected = _coerce(raw, fact)

    if vector is None:
        return True  # validation pass: syntax is fine, nothing to compare

    actual = getattr(vector, fact)
    try:
        return bool(OPERATORS[op_name](actual, expected))
    except TypeError as exc:
        raise RuleSyntaxError(
            f"cannot apply {op_name!r} to {fact!r} ({type(actual).__name__} vs "
            f"{type(expected).__name__})"
        ) from exc


def evaluate(condition: dict, vector: FeatureVector) -> bool:
    """Does this condition hold for this transaction?"""
    return _walk(condition, vector, depth=0, counter=[0])


def validate_condition(condition: dict) -> None:
    """Raise RuleSyntaxError if the condition is not safely evaluable.

    Called by the serialiser on write, so a bad rule is rejected by the admin
    API and can never reach the screening path.
    """
    _walk(condition, None, depth=0, counter=[0])


def sample_vector(**overrides) -> FeatureVector:
    """A neutral vector, for validation and for rule dry-runs."""
    defaults = dict(
        amount=Decimal("0"), currency="INR", rail="INTERNAL", txn_type="TRANSFER",
        hour_of_day=12, amount_zscore=0.0, amount_vs_mean_ratio=1.0,
        amount_vs_max_ratio=1.0, txn_count_5m=0, txn_sum_5m=Decimal("0"),
        txn_count_1h=0, txn_count_24h=0, txn_sum_24h=Decimal("0"),
        distinct_benef_5m=0, distinct_benef_24h=0, beneficiary_is_new=False,
        beneficiary_age_hours=0.0, beneficiary_in_cooling_off=False,
        beneficiary_blacklisted=False, beneficiary_txn_count=0,
        destination_country="IN", destination_is_high_risk=False,
        country_changed=False, device_is_new=False, device_blocked=False,
        is_first_txn=False, account_age_days=0, account_txn_count=0,
    )
    defaults.update(overrides)
    return FeatureVector(**defaults)
