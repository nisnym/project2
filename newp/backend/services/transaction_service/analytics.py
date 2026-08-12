"""Pure aggregation over a list of transaction dicts.

No FastAPI, no I/O — every function here takes rows and returns numbers, so
each one is unit-testable and any figure in the UI can be reproduced in a
Python REPL in front of a judge.

Sign convention, applied everywhere: negative amount = money out (spend),
positive = money in (income). Nothing else classifies a transaction, which
is why a brand new category invented in the JSON just works.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any, Iterable, Optional

from shared.dates import month_key, previous_months
from shared.money import pct, round_money

Row = dict[str, Any]


# ----------------------------------------------------------------------
# primitives
# ----------------------------------------------------------------------

def spend_of(row: Row) -> float:
    amount = float(row.get("amount", 0))
    return -amount if amount < 0 else 0.0


def income_of(row: Row) -> float:
    amount = float(row.get("amount", 0))
    return amount if amount > 0 else 0.0


def in_month(rows: Iterable[Row], month: str) -> list[Row]:
    return [r for r in rows if str(r.get("date", ""))[:7] == month]


def total_spend(rows: Iterable[Row]) -> float:
    return round_money(sum(spend_of(r) for r in rows))


def total_income(rows: Iterable[Row]) -> float:
    return round_money(sum(income_of(r) for r in rows))


# ----------------------------------------------------------------------
# summaries
# ----------------------------------------------------------------------

def category_totals(rows: Iterable[Row]) -> list[dict]:
    """Spend per category, biggest first, with each category's share."""
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        spend = spend_of(row)
        if spend:
            category = row.get("category", "uncategorised")
            totals[category] += spend
            counts[category] += 1

    grand = sum(totals.values())
    return [
        {
            "category": category,
            "total": round_money(amount),
            "count": counts[category],
            "shareOfSpend": pct(amount, grand),
        }
        for category, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    ]


def top_merchant(rows: Iterable[Row]) -> Optional[str]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        spend = spend_of(row)
        if spend:
            totals[row.get("merchant", "unknown")] += spend
    if not totals:
        return None
    return max(totals.items(), key=lambda kv: kv[1])[0]


def summarize_month(rows: Iterable[Row], month: str) -> dict:
    scoped = in_month(rows, month)
    return {
        "month": month,
        "totalSpend": total_spend(scoped),
        "totalIncome": total_income(scoped),
        "net": round_money(total_income(scoped) - total_spend(scoped)),
        "transactionCount": len(scoped),
        "byCategory": category_totals(scoped),
        "topMerchant": top_merchant(scoped),
    }


def monthly_totals(rows: Iterable[Row], months: list[str]) -> list[dict]:
    rows = list(rows)
    out = []
    for month in months:
        scoped = in_month(rows, month)
        spend = total_spend(scoped)
        income = total_income(scoped)
        out.append(
            {
                "month": month,
                "spend": spend,
                "income": income,
                "net": round_money(income - spend),
                "count": len(scoped),
            }
        )
    return out


def known_months(rows: Iterable[Row]) -> list[str]:
    """Every month the ledger actually covers, newest first."""
    return sorted({month_key(str(r.get("date", ""))) for r in rows if r.get("date")}, reverse=True)


# ----------------------------------------------------------------------
# inputs the budget + insight services rely on
# ----------------------------------------------------------------------

def category_spend_in_month(rows: Iterable[Row], category: str, month: str) -> float:
    return round_money(
        sum(spend_of(r) for r in in_month(rows, month) if r.get("category") == category)
    )


def spend_after_day(rows: Iterable[Row], category: str, month: str, day: int) -> float:
    """What this category cost *after* day-of-month `day`, in `month`.

    This is the honest way to project a part-finished month. A linear
    run-rate says someone who paid rent on the 2nd will pay it fifteen more
    times; looking at what actually happened after the 12th in previous
    months does not.
    """
    total = 0.0
    for row in in_month(rows, month):
        if row.get("category") != category:
            continue
        date = str(row.get("date", ""))
        if len(date) >= 10 and int(date[8:10]) > day:
            total += spend_of(row)
    return round_money(total)


def expected_remaining(rows: Iterable[Row], category: str, month: str, day: int, lookback: int = 2) -> tuple[float, list[str]]:
    """What this category is still likely to cost before the month ends.

    Two parts, because a category is usually two different things at once:

    * **Regular charges** — a merchant that bills the same amount every month.
      If it has already billed this month, it will not bill again, so it
      contributes nothing. This is the difference between correctly saying
      "your subscriptions are done for the month" and wrongly warning someone
      that Netflix is about to charge them a second time.
    * **Everything else** — averaged from what actually landed after this
      day-of-month in previous months.

    Returns (amount, months_used). An empty months_used means there is no
    history to learn from, and the caller falls back to a linear run-rate and
    says so in the reason.
    """
    rows = list(rows)
    history = [m for m in previous_months(month, lookback) if in_month(rows, m)]
    if not history:
        return 0.0, []

    in_category = [r for r in rows if r.get("category") == category]
    regular = {r["merchant"] for r in detect_recurring(in_category, [month] + history)}
    already_billed = {r.get("merchant") for r in in_month(in_category, month)}

    # 1. irregular spending that historically lands after this point
    tails = []
    for past in history:
        total = 0.0
        for row in in_month(in_category, past):
            if row.get("merchant") in regular:
                continue
            date = str(row.get("date", ""))
            if len(date) >= 10 and int(date[8:10]) > day:
                total += spend_of(row)
        tails.append(total)
    expected = sum(tails) / len(tails)

    # 2. regular charges that haven't come out yet this month
    for merchant in regular - already_billed:
        charges = [
            spend_of(r)
            for r in in_category
            if r.get("merchant") == merchant and month_key(str(r.get("date", ""))) in history
        ]
        if charges:
            expected += sum(charges) / len(charges)

    return round_money(expected), history


def category_history(rows: Iterable[Row], category: str, months: list[str]) -> list[dict]:
    rows = list(rows)
    return [
        {
            "month": month,
            "spend": category_spend_in_month(rows, category, month),
            "count": len([r for r in in_month(rows, month) if r.get("category") == category]),
        }
        for month in months
    ]


def typical_monthly_spend(rows: Iterable[Row], category: str, months: list[str]) -> float:
    """Median, not mean — one ₹32,999 laptop shouldn't redefine 'typical'."""
    values = [category_spend_in_month(rows, category, m) for m in months]
    values = [v for v in values if v > 0]
    return round_money(median(values)) if values else 0.0


def largest_spend(rows: Iterable[Row]) -> Optional[Row]:
    debits = [r for r in rows if spend_of(r) > 0]
    return max(debits, key=spend_of) if debits else None


def detect_recurring(rows: Iterable[Row], months: list[str], min_occurrences: int = 2) -> list[dict]:
    """Merchants that bill like a standing order: once a month, same amount.

    Three tests, all of which have to pass — deliberately strict, because a
    favourite restaurant visited monthly is not a subscription:

    * seen in at least two of the months examined
    * exactly one charge per month (not a merchant you visit repeatedly)
    * amounts within 2% of each other — and if it has only been seen twice,
      identical to the rupee. Two grocery runs that happened to cost about
      the same are not a subscription, and calling them one would put
      "consider cancelling DMart" in front of a customer.
    """
    rows = list(rows)
    by_merchant: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if spend_of(row) > 0 and month_key(str(row.get("date", ""))) in months:
            by_merchant[row.get("merchant", "unknown")].append(row)

    out = []
    for merchant, charges in by_merchant.items():
        seen_months = {month_key(str(c["date"])) for c in charges}
        if len(seen_months) < min_occurrences:
            continue
        if len(charges) != len(seen_months):
            continue
        amounts = [spend_of(c) for c in charges]
        spread = (max(amounts) - min(amounts)) / max(amounts) if max(amounts) else 1
        if spread > 0.02:
            continue
        if len(seen_months) < 3 and spread > 0:
            continue
        out.append(
            {
                "merchant": merchant,
                "category": charges[0].get("category", "uncategorised"),
                "averageAmount": round_money(sum(amounts) / len(amounts)),
                "occurrences": len(charges),
                "monthsSeen": sorted(seen_months, reverse=True),
                "lastCharged": max(str(c["date"]) for c in charges),
                "transactionIds": [c["id"] for c in charges],
            }
        )
    return sorted(out, key=lambda r: r["averageAmount"], reverse=True)
