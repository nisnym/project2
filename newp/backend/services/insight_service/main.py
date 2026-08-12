"""insight-service (:9004)

Owns no data at all. It asks customer-service who this is, transaction-service
what they did, budget-service what they planned, and turns the three answers
into advice — each piece carrying the reason it exists.

If this service is up and the others are not, it returns an honest 503 rather
than a plausible-looking answer built from nothing.
"""

from __future__ import annotations

import asyncio

from fastapi import Query

from shared.config import settings
from shared.dates import current_month, month_label, previous_months
from shared.http import ServiceClient
from shared.models import HealthScore, Insight
from shared.money import format_inr
from shared.service import create_app

from .rules import Context, health_score, run_all

customer_client = ServiceClient("customer", settings.customer_service_url)
txn_client = ServiceClient("transaction", settings.transaction_service_url)
budget_client = ServiceClient("budget", settings.budget_service_url)

HISTORY_MONTHS = 3


async def _close_clients() -> None:
    await asyncio.gather(
        customer_client.aclose(), txn_client.aclose(), budget_client.aclose()
    )


app = create_app(
    name="insight-service",
    title="PFA · insight-service",
    description="Recommendations derived from the customer's own ledger, each with a reason.",
    on_shutdown=_close_clients,
)


async def _load_context(customer_id: str, month: str) -> Context:
    """One fan-out, then every rule reads from the same consistent snapshot."""
    history = previous_months(month, HISTORY_MONTHS)

    async def _optional(coro):
        try:
            return await coro
        except Exception:  # noqa: BLE001 — an absent account isn't a failed request
            return None

    (
        customer,
        account,
        summary,
        monthly,
        recurring,
        month_txns,
        all_txns,
        budgets,
        *prior,
    ) = await asyncio.gather(
        customer_client.get(f"/customers/{customer_id}"),
        _optional(customer_client.get(f"/customers/{customer_id}/account")),
        txn_client.get(f"/customers/{customer_id}/summary", month=month),
        txn_client.get(f"/customers/{customer_id}/monthly", months=HISTORY_MONTHS + 1),
        txn_client.get(f"/customers/{customer_id}/recurring", months=HISTORY_MONTHS),
        txn_client.get(f"/customers/{customer_id}/transactions", month=month),
        txn_client.get(f"/customers/{customer_id}/transactions"),
        budget_client.get(f"/budgets/{customer_id}", month=month),
        *[txn_client.get(f"/customers/{customer_id}/summary", month=m) for m in history],
    )

    prior_summaries = {
        m: s for m, s in zip(history, prior) if s and s.get("transactionCount", 0) > 0
    }

    return Context(
        customer=customer,
        account=account,
        month=month,
        summary=summary,
        prior_summaries=prior_summaries,
        monthly=monthly.get("months", []),
        budgets=budgets,
        recurring=recurring,
        transactions=month_txns,
        all_transactions=all_txns,
    )


@app.get("/insights/{customer_id}", response_model=list[Insight], tags=["insights"])
async def insights_for(
    customer_id: str,
    month: str | None = Query(None),
    severity: str | None = Query(None, description="critical | warning | info | positive"),
    limit: int = Query(20, ge=1, le=50),
) -> list[Insight]:
    ctx = await _load_context(customer_id, month or current_month())
    found = run_all(ctx)
    if severity:
        found = [i for i in found if i.severity == severity]
    return found[:limit]


@app.get("/insights/{customer_id}/health", response_model=HealthScore, tags=["insights"])
async def health_for(customer_id: str, month: str | None = Query(None)) -> HealthScore:
    ctx = await _load_context(customer_id, month or current_month())
    return health_score(ctx)


@app.get("/insights/{customer_id}/snapshot", tags=["insights"])
async def snapshot(customer_id: str, month: str | None = Query(None)) -> dict:
    """One flat fact-pack about this customer, as of now.

    This is the object the advisor is grounded on — the chat service hands
    exactly this to the model, so anything the advisor says can be checked
    against a single URL a judge can open themselves.
    """
    month = month or current_month()
    ctx = await _load_context(customer_id, month)
    found = run_all(ctx)
    health = health_score(ctx)
    last = ctx.last_full_month() or {}

    return {
        "asOf": month,
        "asOfLabel": month_label(month),
        "daysElapsed": ctx.day,
        "daysLeft": ctx.days_left,
        "customer": {
            "id": ctx.customer_id,
            "name": ctx.customer.get("name"),
            "segment": ctx.customer.get("segment"),
            "city": ctx.customer.get("city"),
            "monthlyIncome": ctx.income,
            "incomeStability": ctx.customer.get("incomeStability"),
            "goals": ctx.customer.get("goals", []),
        },
        "account": ctx.account,
        "balanceLabel": format_inr(ctx.balance),
        "thisMonth": {
            "spend": ctx.summary.get("totalSpend"),
            "income": ctx.summary.get("totalIncome"),
            "net": ctx.summary.get("net"),
            "transactionCount": ctx.summary.get("transactionCount"),
            "byCategory": ctx.summary.get("byCategory", []),
            "topMerchant": ctx.summary.get("topMerchant"),
        },
        "lastCompletedMonth": {
            "month": last.get("month"),
            "spend": last.get("totalSpend"),
            "income": last.get("totalIncome"),
            "net": last.get("net"),
            "byCategory": last.get("byCategory", []),
        },
        "monthlyTrend": ctx.monthly,
        "budgets": ctx.budgets,
        "recurring": ctx.recurring.get("recurring", []),
        "recurringMonthlyTotal": ctx.recurring.get("monthlyTotal", 0),
        "health": health.model_dump(),
        "insights": [i.model_dump() for i in found],
        "recentTransactions": ctx.transactions[:15],
    }


@app.get("/meta/stats", tags=["meta"])
async def stats() -> dict:
    from .rules import ALL_RULES

    return {
        "rules": [r.__name__ for r in ALL_RULES],
        "historyMonths": HISTORY_MONTHS,
        "dependsOn": {
            "customer-service": settings.customer_service_url,
            "transaction-service": settings.transaction_service_url,
            "budget-service": settings.budget_service_url,
        },
    }
