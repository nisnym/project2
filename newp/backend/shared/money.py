"""Rupee helpers.

Amounts travel as signed floats in rupees: negative = money out, positive =
money in. Anything user-visible goes through format_inr so the whole product
speaks in lakh/crore grouping rather than western thousands.
"""

from __future__ import annotations

from .config import settings

__all__ = ["round_money", "format_inr", "pct"]


def round_money(value: float) -> float:
    """Rupees, two decimals. Applied at every service boundary."""
    return round(float(value) + 0.0, 2)


def _group_indian(whole: str) -> str:
    """12345678 -> 1,23,45,678 (last three digits, then pairs)."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def format_inr(amount: float, *, decimals: bool = False, signed: bool = False) -> str:
    """₹1,23,456 — the string form used in insight text and chat answers.

    Reasons read better without paise, so decimals default off; pass
    decimals=True where exactness matters (e.g. a single transaction).
    """
    value = round_money(amount)
    negative = value < 0
    value = abs(value)

    if decimals:
        whole, frac = f"{value:.2f}".split(".")
        body = f"{_group_indian(whole)}.{frac}"
    else:
        body = _group_indian(f"{round(value):.0f}")

    sign = "-" if negative else ("+" if signed else "")
    return f"{sign}{settings.currency_symbol}{body}"


def pct(part: float, whole: float) -> float:
    """Percentage, guarded against a zero denominator."""
    if not whole:
        return 0.0
    return round((part / whole) * 100, 1)
