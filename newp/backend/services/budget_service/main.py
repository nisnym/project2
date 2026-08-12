"""budget-service (:9003)

Owns budgets.json — but only the *limits*. Everything else on a budget
(spent, remaining, projected, status) is computed live by asking
transaction-service, so a budget can never drift out of sync with the ledger.

This is the clearest example of the three layers genuinely talking: this
service holds no transactions at all, and cannot answer a single question
without a live call to :9002.
"""

from __future__ import annotations

from fastapi import Query

from shared.categories import NON_BUDGETABLE_CATEGORIES
from shared.config import settings
from shared.dates import current_month, days_in_month, elapsed_days, month_label, previous_months
from shared.errors import AppError, NotFoundError
from shared.http import ServiceClient
from shared.models import Budget, BudgetSuggestion, BudgetUpdate, DataPoint
from shared.money import format_inr, pct, round_money
from shared.service import create_app
from shared.store import JsonStore

txn_client = ServiceClient("transaction", settings.transaction_service_url)

app = create_app(
    name="budget-service",
    title="PFA · budget-service",
    description="Monthly limits, live spend against them, and month-end projections.",
    on_shutdown=txn_client.aclose,
)

budgets = JsonStore("budgets.json", key="category")


# ----------------------------------------------------------------------
# verdicts
# ----------------------------------------------------------------------

def _months_phrase(months: list[str]) -> str:
    labels = [month_label(m).split(" ")[0] for m in months]
    if len(labels) == 1:
        return labels[0]
    return " and ".join([", ".join(labels[:-1]), labels[-1]])


def _verdict(
    *,
    category: str,
    limit: float,
    spent: float,
    projected: float,
    method: str,
    months_used: list[str],
    month: str,
) -> tuple[str, str]:
    """Return (status, reason). The reason is what the customer actually reads.

    It has to survive the "explain any line" question, so it always names the
    numbers it used and where the projection came from.
    """
    day = elapsed_days(month)
    days_left = max(days_in_month(month) - day, 0)
    remaining = round_money(limit - spent)

    if limit <= 0:
        return "under", f"No limit set for {category}; {format_inr(spent)} spent so far."

    if spent > limit:
        return (
            "over",
            f"{format_inr(spent)} spent against a {format_inr(limit)} limit — "
            f"{format_inr(spent - limit)} past it, with {days_left} days still to run in "
            f"{month_label(month)}.",
        )

    if projected > limit:
        if method == "history":
            basis = (
                f"in {_months_phrase(months_used)} you spent another "
                f"{format_inr(projected - spent)} on {category} after the {day}th"
            )
        else:
            basis = f"at the current run-rate another {format_inr(projected - spent)} is likely"
        return (
            "at-risk",
            f"{format_inr(spent)} of {format_inr(limit)} is gone {day} days in. Because {basis}, "
            f"this is heading for about {format_inr(projected)} — roughly "
            f"{format_inr(projected - limit)} over.",
        )

    if projected > limit * 0.85:
        return (
            "on-track",
            f"{format_inr(spent)} of {format_inr(limit)}. Heading for about "
            f"{format_inr(projected)} — inside the limit, but with little room left.",
        )

    return (
        "under",
        f"{format_inr(spent)} of {format_inr(limit)}, heading for about {format_inr(projected)}. "
        f"{format_inr(remaining)} of headroom.",
    )


async def _pace_index(customer_id: str, month: str) -> dict[str, dict]:
    pace = await txn_client.get(f"/customers/{customer_id}/pace", month=month)
    return {row["category"]: row for row in pace.get("categories", [])}


def _limits_for(customer_id: str) -> list[dict]:
    return budgets.find(customerId=customer_id)


async def _build_budgets(customer_id: str, month: str) -> list[dict]:
    paces = await _pace_index(customer_id, month)
    rows = _limits_for(customer_id)

    out: list[dict] = []
    for row in rows:
        category = row["category"]
        limit = float(row.get("monthlyLimit", 0))
        pace = paces.get(category, {})
        spent = float(pace.get("spentSoFar", 0.0))
        projected = float(pace.get("projectedSpend", spent))
        status, reason = _verdict(
            category=category,
            limit=limit,
            spent=spent,
            projected=projected,
            method=pace.get("method", "run-rate"),
            months_used=pace.get("monthsUsed", []),
            month=month,
        )
        out.append(
            {
                "customerId": customer_id,
                "category": category,
                "monthlyLimit": round_money(limit),
                "spentSoFar": round_money(spent),
                "remaining": round_money(limit - spent),
                "pctUsed": pct(spent, limit),
                "status": status,
                "projectedSpend": round_money(projected),
                "reason": reason,
            }
        )

    severity_order = {"over": 0, "at-risk": 1, "on-track": 2, "under": 3}
    return sorted(out, key=lambda b: (severity_order[b["status"]], -b["pctUsed"]))


# ----------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------

@app.get("/budgets", response_model=list[Budget], tags=["budgets"])
async def list_budgets(
    customerId: str = Query(..., description="Customer to report on"),
    month: str | None = Query(None, description="YYYY-MM; defaults to the current month"),
) -> list[dict]:
    return await _build_budgets(customerId, month or current_month())


@app.get("/budgets/{customer_id}", response_model=list[Budget], tags=["budgets"])
async def budgets_for_customer(customer_id: str, month: str | None = Query(None)) -> list[dict]:
    return await _build_budgets(customer_id, month or current_month())


@app.get("/budgets/{customer_id}/status", tags=["budgets"])
async def budget_status(customer_id: str, month: str | None = Query(None)) -> dict:
    month = month or current_month()
    rows = await _build_budgets(customer_id, month)
    over = [b for b in rows if b["status"] == "over"]
    at_risk = [b for b in rows if b["status"] == "at-risk"]
    return {
        "customerId": customer_id,
        "month": month,
        "budgetCount": len(rows),
        "overCount": len(over),
        "atRiskCount": len(at_risk),
        "totalLimit": round_money(sum(b["monthlyLimit"] for b in rows)),
        "totalSpent": round_money(sum(b["spentSoFar"] for b in rows)),
        "totalProjected": round_money(sum(b["projectedSpend"] for b in rows)),
        "over": [b["category"] for b in over],
        "atRisk": [b["category"] for b in at_risk],
    }


@app.put("/budgets/{customer_id}/{category}", response_model=Budget, tags=["budgets"])
async def set_limit(customer_id: str, category: str, body: BudgetUpdate) -> dict:
    """Change a limit and every downstream verdict changes with it."""
    if body.monthlyLimit < 0:
        raise AppError("A monthly limit can't be negative.", code="invalid_limit")

    category = category.strip().lower()
    budgets.upsert(
        {
            "customerId": customer_id,
            "category": category,
            "monthlyLimit": round_money(body.monthlyLimit),
        },
        customerId=customer_id,
        category=category,
    )
    rows = await _build_budgets(customer_id, current_month())
    for row in rows:
        if row["category"] == category:
            return row
    raise NotFoundError(f"Budget for '{category}' could not be read back.")


# ----------------------------------------------------------------------
# suggestions
# ----------------------------------------------------------------------

def _round_to(value: float, step: int = 100) -> float:
    return float(int(round(value / step)) * step)


@app.get(
    "/budgets/{customer_id}/suggestions",
    response_model=list[BudgetSuggestion],
    tags=["suggestions"],
)
async def suggest_budgets(
    customer_id: str,
    months: int = Query(3, ge=2, le=12, description="Completed months to learn from"),
) -> list[dict]:
    """Propose limits from what this person actually spends.

    Three cases, each with its own reason:

    * **Unrealistic limit** — they break it every month. A limit that is
      always broken has stopped being information, so we propose a reachable
      step down rather than pretending.
    * **Stale limit** — they never come close. That headroom is money that
      could be going at a goal.
    * **No limit at all** — a category with real, repeated spend and nothing
      watching it.
    """
    month = current_month()
    history = previous_months(month, months)  # completed months only
    existing = {b["category"]: float(b.get("monthlyLimit", 0)) for b in _limits_for(customer_id)}

    summaries = {}
    for past in history:
        summaries[past] = await txn_client.get(f"/customers/{customer_id}/summary", month=past)

    spend_by_category: dict[str, list[float]] = {}
    for past, summary in summaries.items():
        for entry in summary.get("byCategory", []):
            spend_by_category.setdefault(entry["category"], []).append(float(entry["total"]))

    suggestions: list[dict] = []
    for category, amounts in spend_by_category.items():
        # Rent, EMIs, investments and medical spend are tracked but never
        # capped — see shared/categories.py for why.
        if category in NON_BUDGETABLE_CATEGORIES:
            continue
        observed = sorted(amounts)
        typical = observed[len(observed) // 2]  # median: one-offs don't set the norm
        if typical < 200:  # not worth a budget line
            continue

        current_limit = existing.get(category)

        def _spend_in(past: str, _category: str = category) -> float:
            entries = summaries[past].get("byCategory", [])
            return next((e["total"] for e in entries if e["category"] == _category), 0.0)

        points = [
            DataPoint(
                label=f"{month_label(past).split(' ')[0]} {category} spend",
                value=format_inr(_spend_in(past)),
                source=f"month:{past}",
            ).model_dump()
            for past in history
        ]

        if current_limit is None:
            suggested = _round_to(typical * 1.1)
            reason = (
                f"You spend on {category} every month — a typical month is "
                f"{format_inr(typical)} — but there is no limit on it. "
                f"{format_inr(suggested)} matches your own habit with a little room."
            )
            confidence = "medium"
        elif typical > current_limit * 1.2:
            step = _round_to((typical + current_limit) / 2)
            suggested = step
            reason = (
                f"Your {category} limit is {format_inr(current_limit)}, but a typical month is "
                f"{format_inr(typical)} — you have broken it in most of the last {len(observed)} "
                f"months. A limit you always break stops being useful. "
                f"{format_inr(step)} is reachable now; tighten it again next month."
            )
            confidence = "high"
        elif typical < current_limit * 0.6:
            suggested = _round_to(max(typical * 1.15, 200))
            reason = (
                f"You have {format_inr(current_limit)} set aside for {category} but typically "
                f"spend {format_inr(typical)}. Freeing {format_inr(current_limit - suggested)} "
                f"of that headroom puts it somewhere it can work for you."
            )
            confidence = "medium"
        else:
            continue

        suggestions.append(
            {
                "category": category,
                "currentLimit": current_limit,
                "suggestedLimit": suggested,
                "change": round_money(suggested - (current_limit or 0)),
                "reason": reason,
                "dataPoints": points,
                "confidence": confidence,
            }
        )

    return sorted(suggestions, key=lambda s: abs(s["change"]), reverse=True)


@app.get("/meta/stats", tags=["meta"])
async def stats() -> dict:
    rows = budgets.all()
    return {
        "budgets": len(rows),
        "customers": sorted({r.get("customerId") for r in rows}),
        "dependsOn": {"transaction-service": settings.transaction_service_url},
    }
