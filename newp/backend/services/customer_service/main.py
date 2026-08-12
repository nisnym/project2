"""customer-service (:9001)

Owns customers.json and accounts.json. Deliberately dumb: it answers "who is
this person and what do they hold", and nothing else. No spend logic lives
here, so there is never a second opinion about a customer's identity.
"""

from __future__ import annotations

from fastapi import Query

from shared.errors import NotFoundError
from shared.models import Account, Customer, Goal
from shared.service import create_app
from shared.store import JsonStore

app = create_app(
    name="customer-service",
    title="PFA · customer-service",
    description="Customers, accounts and goals. The system of record for identity.",
)

customers = JsonStore("customers.json")
accounts = JsonStore("accounts.json")


def _require_customer(customer_id: str) -> dict:
    row = customers.get(customer_id)
    if row is None:
        known = ", ".join(c["id"] for c in customers.all()) or "none loaded"
        raise NotFoundError(
            f"We don't hold a customer with id '{customer_id}'.",
            hint=f"Known customers: {known}.",
        )
    return row


@app.get("/customers", response_model=list[Customer], tags=["customers"])
async def list_customers(segment: str | None = Query(None)) -> list[dict]:
    rows = customers.all()
    if segment:
        rows = [r for r in rows if r.get("segment") == segment]
    return sorted(rows, key=lambda r: r.get("name", ""))


@app.get("/customers/{customer_id}", response_model=Customer, tags=["customers"])
async def get_customer(customer_id: str) -> dict:
    return _require_customer(customer_id)


@app.get("/customers/{customer_id}/account", response_model=Account, tags=["accounts"])
async def get_primary_account(customer_id: str) -> dict:
    _require_customer(customer_id)
    held = accounts.find(customerId=customer_id)
    if not held:
        raise NotFoundError(
            "This customer doesn't have an account on file yet.",
            hint=f"Add a row to accounts.json with customerId '{customer_id}'.",
        )
    return held[0]


@app.get("/customers/{customer_id}/accounts", response_model=list[Account], tags=["accounts"])
async def list_accounts(customer_id: str) -> list[dict]:
    _require_customer(customer_id)
    return accounts.find(customerId=customer_id)


@app.get("/customers/{customer_id}/goals", response_model=list[Goal], tags=["customers"])
async def list_goals(customer_id: str) -> list[dict]:
    return _require_customer(customer_id).get("goals", [])


@app.get("/accounts/{account_id}", response_model=Account, tags=["accounts"])
async def get_account(account_id: str) -> dict:
    row = accounts.get(account_id)
    if row is None:
        raise NotFoundError(f"No account with id '{account_id}'.")
    return row


@app.get("/meta/stats", tags=["meta"])
async def stats() -> dict:
    return {
        "customers": customers.count(),
        "accounts": accounts.count(),
        "source": str(customers.path.parent),
    }
