"""HTTP-level tests for the two services that own data.

These run against the real seed ledger with no network and no other service
running — if these pass, the data files and the contract agree.
"""

import pytest
from fastapi.testclient import TestClient

from services.customer_service.main import app as customer_app
from services.transaction_service.main import app as transaction_app


@pytest.fixture(scope="module")
def customers():
    with TestClient(customer_app) as client:
        yield client


@pytest.fixture(scope="module")
def ledger():
    with TestClient(transaction_app) as client:
        yield client


class TestCustomerService:
    def test_health(self, customers):
        body = customers.get("/health").json()
        assert body["status"] == "up"
        assert body["service"] == "customer-service"

    def test_lists_the_seeded_customers(self, customers):
        rows = customers.get("/customers").json()
        assert len(rows) >= 3
        assert {"id", "name", "segment", "monthlyIncome"} <= set(rows[0])

    def test_unknown_customer_is_a_helpful_404(self, customers):
        response = customers.get("/customers/nobody")
        assert response.status_code == 404
        error = response.json()["error"]
        assert "nobody" in error["message"]
        assert "cust-001" in error["hint"]  # tells you what does exist

    def test_account_belongs_to_the_customer(self, customers):
        account = customers.get("/customers/cust-001/account").json()
        assert account["customerId"] == "cust-001"
        assert account["currency"] == "INR"


class TestTransactionService:
    def test_month_summary_adds_up(self, ledger):
        summary = ledger.get("/customers/cust-001/summary", params={"month": "2026-07"}).json()
        assert summary["totalIncome"] == 145000
        assert summary["net"] == summary["totalIncome"] - summary["totalSpend"]
        assert sum(c["total"] for c in summary["byCategory"]) == pytest.approx(summary["totalSpend"])

    def test_filters_compose(self, ledger):
        rows = ledger.get(
            "/transactions",
            params={"customerId": "cust-001", "category": "dining", "from": "2026-08-01"},
        ).json()
        assert rows
        assert all(r["category"] == "dining" and r["date"] >= "2026-08-01" for r in rows)

    def test_pace_reports_which_method_it_used(self, ledger):
        pace = ledger.get("/customers/cust-001/pace").json()
        dining = next(c for c in pace["categories"] if c["category"] == "dining")
        assert dining["method"] == "history"
        assert dining["monthsUsed"] == ["2026-07", "2026-06"]
        assert dining["projectedSpend"] >= dining["spentSoFar"]

    def test_a_customer_with_no_ledger_gets_zeroes_not_an_error(self, ledger):
        summary = ledger.get("/customers/ghost/summary").json()
        assert summary["totalSpend"] == 0
        assert summary["byCategory"] == []

    def test_posting_a_transaction_moves_the_totals(self, ledger):
        before = ledger.get("/customers/cust-002/summary").json()["totalSpend"]

        created = ledger.post(
            "/customers/cust-002/transactions",
            json={"amount": -450, "category": "dining", "merchant": "Test Cafe"},
        )
        assert created.status_code == 201
        assert created.json()["id"].startswith("new-")

        after = ledger.get("/customers/cust-002/summary").json()["totalSpend"]
        assert after == pytest.approx(before + 450)

    def test_a_zero_amount_is_rejected_in_plain_language(self, ledger):
        response = ledger.post(
            "/customers/cust-002/transactions",
            json={"amount": 0, "category": "dining", "merchant": "Nothing"},
        )
        assert response.status_code == 400
        assert "₹0" in response.json()["error"]["message"]
