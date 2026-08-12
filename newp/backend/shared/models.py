"""The wire contract, shared by every service and the SPA.

Field names are camelCase on purpose: these models ARE the JSON the React app
consumes, so what you read here is exactly what shows up in the browser's
network tab. No alias indirection to trip over during a live demo.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["critical", "warning", "info", "positive"]
Confidence = Literal["high", "medium", "low"]


# ----------------------------------------------------------------------
# customer domain
# ----------------------------------------------------------------------

class Goal(BaseModel):
    id: str
    label: str
    targetAmount: float
    savedAmount: float = 0.0
    targetDate: Optional[str] = None


class Customer(BaseModel):
    id: str
    name: str
    segment: str                       # salaried | student | self-employed | ...
    city: Optional[str] = None
    monthlyIncome: float = 0.0
    incomeStability: str = "regular"   # regular | variable
    currency: str = "INR"
    joinedOn: Optional[str] = None
    goals: list[Goal] = Field(default_factory=list)


class Account(BaseModel):
    id: str
    customerId: str
    type: str                          # savings | current | ...
    balance: float
    currency: str = "INR"
    openedOn: Optional[str] = None


# ----------------------------------------------------------------------
# transaction domain
# ----------------------------------------------------------------------

class Transaction(BaseModel):
    id: str
    customerId: str
    date: str                          # ISO YYYY-MM-DD
    amount: float                      # negative = debit, positive = credit
    category: str
    merchant: str
    channel: Optional[str] = None      # upi | card | netbanking | auto-debit
    note: Optional[str] = None


class NewTransaction(BaseModel):
    """Body for POST /transactions — the live-demo lever.

    Add a spend during the demo and every budget, insight and chat answer
    downstream moves, because nothing is precomputed.
    """
    date: Optional[str] = None
    amount: float
    category: str
    merchant: str
    channel: Optional[str] = "upi"
    note: Optional[str] = None


class CategoryTotal(BaseModel):
    category: str
    total: float
    count: int
    shareOfSpend: float = 0.0


class MonthTotal(BaseModel):
    month: str                         # YYYY-MM
    spend: float
    income: float
    net: float
    count: int


class SpendSummary(BaseModel):
    customerId: str
    month: str
    from_: str = Field(serialization_alias="from", validation_alias="from")
    to: str
    totalSpend: float
    totalIncome: float
    net: float
    transactionCount: int
    byCategory: list[CategoryTotal] = Field(default_factory=list)
    topMerchant: Optional[str] = None

    model_config = {"populate_by_name": True}


# ----------------------------------------------------------------------
# budget domain
# ----------------------------------------------------------------------

class Budget(BaseModel):
    customerId: str
    category: str
    monthlyLimit: float
    spentSoFar: float = 0.0
    remaining: float = 0.0
    pctUsed: float = 0.0
    status: Literal["under", "on-track", "at-risk", "over"] = "under"
    projectedSpend: float = 0.0        # month-end estimate at the current pace
    reason: Optional[str] = None       # why we called it at-risk / over


class BudgetUpdate(BaseModel):
    monthlyLimit: float


class BudgetSuggestion(BaseModel):
    category: str
    currentLimit: Optional[float] = None
    suggestedLimit: float
    change: float
    reason: str
    dataPoints: list["DataPoint"] = Field(default_factory=list)
    confidence: Confidence = "medium"


# ----------------------------------------------------------------------
# insight domain
# ----------------------------------------------------------------------

class DataPoint(BaseModel):
    """One traceable fact behind a recommendation.

    `source` points back at the record it came from (txn:<id>,
    budget:<category>, account:<id>, month:<YYYY-MM>) so any number on screen
    can be walked back to the customer's own ledger.
    """
    label: str
    value: str
    source: Optional[str] = None


class Insight(BaseModel):
    id: str
    customerId: str
    type: str
    severity: Severity
    category: Optional[str] = None
    headline: str
    detail: str
    reason: str                        # plain language, no jargon
    recommendedAction: Optional[str] = None
    impact: Optional[float] = None     # rupees at stake, when quantifiable
    confidence: Confidence = "high"
    dataPoints: list[DataPoint] = Field(default_factory=list)


class HealthComponent(BaseModel):
    label: str
    score: int
    weight: float
    reason: str


class HealthScore(BaseModel):
    customerId: str
    score: int                         # 0-100
    band: Literal["strong", "steady", "stretched", "strained"]
    summary: str
    components: list[HealthComponent] = Field(default_factory=list)


# ----------------------------------------------------------------------
# chat domain
# ----------------------------------------------------------------------

class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """Accepts either {customerId, message} or the SPA's older
    {customer, transactions, budgets, message} shape — the extra payload is
    ignored, because the chat service fetches its own facts from the
    services that own them."""
    customerId: Optional[str] = None
    message: str
    history: list[ChatTurn] = Field(default_factory=list)
    customer: Optional[dict[str, Any]] = None


class ChatResponse(BaseModel):
    # snake_case here matches what ChatPanel.jsx already reads.
    answer: str
    reasoning: Optional[str] = None
    data_points_used: list[DataPoint] = Field(default_factory=list)
    confidence: Confidence = "medium"
    intent: str = "unknown"
    source: str = "fallback"           # github_sdk | github_cli | fallback
    model: Optional[str] = None


# ----------------------------------------------------------------------
# bank-side domain
# ----------------------------------------------------------------------

class PortfolioRow(BaseModel):
    customerId: str
    name: str
    segment: str
    city: Optional[str] = None
    balance: float
    monthlyIncome: float
    monthToDateSpend: float
    savingsRate: float
    healthScore: int
    band: str
    openAlerts: int
    topFlag: Optional[str] = None


class BankAlert(BaseModel):
    customerId: str
    customerName: str
    severity: Severity
    headline: str
    reason: str
    recommendedAction: Optional[str] = None
    impact: Optional[float] = None


class ServiceStatus(BaseModel):
    name: str
    url: str
    status: Literal["up", "down"]
    latencyMs: Optional[float] = None
    detail: Optional[str] = None


class SystemHealth(BaseModel):
    status: Literal["healthy", "degraded"]
    gateway: str = "up"
    checkedAt: str
    services: list[ServiceStatus] = Field(default_factory=list)


BudgetSuggestion.model_rebuild()
