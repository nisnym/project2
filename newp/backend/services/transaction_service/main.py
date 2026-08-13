"""transaction-service (:9002)

Owns transactions.json. Everyone else — budgets, insights, the advisor —
asks this service for spend numbers rather than reading the ledger
themselves, so there is exactly one implementation of "how much did they
spend on dining in August".
"""

from __future__ import annotations

from fastapi import Query

from shared.dates import current_month, elapsed_days, month_progress, previous_months, today
from shared.errors import AppError, NotFoundError
from shared.models import NewTransaction, Transaction
from shared.money import round_money
from shared.service import create_app
from shared.store import JsonStore

from . import analytics

app = create_app(
    name="transaction-service",
    title="PFA · transaction-service",
    description="The ledger, plus every aggregation computed over it.",
)

transactions = JsonStore("transactions.json")


def _for_customer(customer_id: str) -> list[dict]:
    return transactions.find(customerId=customer_id)


def _sorted_desc(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (str(r.get("date", "")), str(r.get("id", ""))), reverse=True)


# ----------------------------------------------------------------------
# raw ledger
# ----------------------------------------------------------------------

@app.get("/transactions", response_model=list[Transaction], tags=["ledger"])
async def list_transactions(
    customerId: str | None = Query(None),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    category: str | None = Query(None),
    merchant: str | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
) -> list[dict]:
    rows = transactions.all()
    if customerId:
        rows = [r for r in rows if r.get("customerId") == customerId]
    if date_from:
        rows = [r for r in rows if str(r.get("date", "")) >= date_from]
    if date_to:
        rows = [r for r in rows if str(r.get("date", "")) <= date_to]
    if category:
        rows = [r for r in rows if r.get("category") == category]
    if merchant:
        rows = [r for r in rows if str(r.get("merchant", "")).lower() == merchant.lower()]
    return _sorted_desc(rows)[:limit]


@app.get("/transactions/{transaction_id}", response_model=Transaction, tags=["ledger"])
async def get_transaction(transaction_id: str) -> dict:
    row = transactions.get(transaction_id)
    if row is None:
        raise NotFoundError(f"No transaction with id '{transaction_id}'.")
    return row


@app.get(
    "/customers/{customer_id}/transactions",
    response_model=list[Transaction],
    tags=["ledger"],
)
async def customer_transactions(
    customer_id: str,
    month: str | None = Query(None, description="YYYY-MM; omit for the full history"),
    limit: int = Query(500, ge=1, le=5000),
) -> list[dict]:
    rows = _for_customer(customer_id)
    if month:
        rows = analytics.in_month(rows, month)
    return _sorted_desc(rows)[:limit]


@app.post(
    "/customers/{customer_id}/transactions",
    response_model=Transaction,
    status_code=201,
    tags=["ledger"],
)
async def add_transaction(customer_id: str, body: NewTransaction) -> dict:
    """Post a spend mid-demo and watch every derived number move.

    Held in memory by default; set PERSIST_WRITES=true to write it back to
    transactions.json.
    """
    if body.amount == 0:
        raise AppError("A transaction of ₹0 wouldn't tell us anything.", code="invalid_amount")
    if not body.merchant.strip() or not body.category.strip():
        raise AppError("Both merchant and category are required.", code="invalid_transaction")

    row = {
        # "new-001" rather than a seed-style id: anything added during a demo
        # should be obvious as such in the ledger and in insight data points.
        "id": transactions.next_id("new-"),
        "customerId": customer_id,
        "date": body.date or today().isoformat(),
        "amount": round_money(body.amount),
        "category": body.category.strip().lower(),
        "merchant": body.merchant.strip(),
        "channel": body.channel or "upi",
        "note": body.note,
    }
    return transactions.append(row)


# ----------------------------------------------------------------------
# aggregation
# ----------------------------------------------------------------------

@app.get("/customers/{customer_id}/summary", tags=["analysis"])
async def month_summary(
    customer_id: str,
    month: str | None = Query(None, description="YYYY-MM; defaults to the current month"),
) -> dict:
    """Everything about one month: totals, category split, top merchant."""
    rows = _for_customer(customer_id)
    month = month or current_month()
    summary = analytics.summarize_month(rows, month)
    summary.update(
        {
            "customerId": customer_id,
            "from": f"{month}-01",
            "to": f"{month}-{elapsed_days(month) or 1:02d}",
            "monthProgress": month_progress(month),
            "daysElapsed": elapsed_days(month),
        }
    )
    return summary


@app.get("/customers/{customer_id}/monthly", tags=["analysis"])
async def monthly_trend(
    customer_id: str,
    months: int = Query(6, ge=1, le=24),
) -> dict:
    """Month-by-month spend/income, newest first — the trend line."""
    rows = _for_customer(customer_id)
    current = current_month()
    window = [current] + previous_months(current, months - 1)
    return {
        "customerId": customer_id,
        "months": analytics.monthly_totals(rows, window),
        "monthsWithData": analytics.known_months(rows),
    }


@app.get("/customers/{customer_id}/categories/{category}/history", tags=["analysis"])
async def category_trend(
    customer_id: str,
    category: str,
    months: int = Query(4, ge=1, le=24),
) -> dict:
    rows = _for_customer(customer_id)
    current = current_month()
    window = [current] + previous_months(current, months - 1)
    history = analytics.category_history(rows, category, window)
    return {
        "customerId": customer_id,
        "category": category,
        "history": history,
        "typicalMonthlySpend": analytics.typical_monthly_spend(
            rows, category, previous_months(current, months - 1)
        ),
    }


@app.get("/customers/{customer_id}/pace", tags=["analysis"])
async def spending_pace(
    customer_id: str,
    month: str | None = Query(None),
) -> dict:
    """Per-category month-end projection — the input to every budget verdict.

    Preferred method ("history"): spend so far + the average of what this
    category actually cost after today's day-of-month in previous months.
    Fallback ("run-rate"): straight-line extrapolation, used only when there
    is no history to learn from. The method used is returned with the number
    so the reason shown to the customer can be truthful about it.
    """
    rows = _for_customer(customer_id)
    month = month or current_month()
    day = elapsed_days(month)
    progress = month_progress(month)

    out = []
    categories = {c["category"] for c in analytics.category_totals(rows)}
    for category in sorted(categories):
        spent = analytics.category_spend_in_month(rows, category, month)
        remaining, months_used = analytics.expected_remaining(rows, category, month, day)
        if months_used:
            method = "history"
        else:
            method = "run-rate"
            remaining = round_money((spent / progress - spent) if progress else 0.0)
        out.append(
            {
                "category": category,
                "spentSoFar": spent,
                "expectedRemaining": remaining,
                "projectedSpend": round_money(spent + remaining),
                "method": method,
                "monthsUsed": months_used,
            }
        )

    return {
        "customerId": customer_id,
        "month": month,
        "daysElapsed": day,
        "monthProgress": progress,
        "categories": out,
    }


@app.get("/customers/{customer_id}/recurring", tags=["analysis"])
async def recurring_charges(
    customer_id: str,
    months: int = Query(3, ge=2, le=12),
) -> dict:
    """Merchants billing a steady amount most months — the silent outgoings."""
    rows = _for_customer(customer_id)
    current = current_month()
    window = [current] + previous_months(current, months - 1)
    found = analytics.detect_recurring(rows, window)
    return {
        "customerId": customer_id,
        "monthsExamined": window,
        "recurring": found,
        "monthlyTotal": round_money(sum(r["averageAmount"] for r in found)),
    }


@app.get("/meta/stats", tags=["meta"])
async def stats() -> dict:
    rows = transactions.all()
    return {
        "transactions": len(rows),
        "customers": sorted({r.get("customerId") for r in rows if r.get("customerId")}),
        "months": analytics.known_months(rows),
        "categories": sorted({r.get("category") for r in rows if r.get("category")}),
    }
