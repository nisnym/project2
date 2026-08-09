"""Eligibility decisioning.

A module rather than a service (ADR-004): it owns no durable state of its own,
always changes together with onboarding product rules, and runs in the same
request cycle.

`evaluate()` is a pure function -- request dataclass in, response dataclass out,
no I/O. That is the seam: extracting it into its own service later means
wrapping it in a view and swapping the caller for a ServiceClient. Nothing else
changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

POLICY_VERSION = "2026.08.1"

MIN_AGE = 18
MAX_AGE = 100
PROHIBITED_COUNTRIES = {"KP", "IR", "SY"}


@dataclass(frozen=True)
class EligibilityRequest:
    age: int
    nationality: str
    annual_income: Decimal
    employment_status: str
    kyc_status: str
    kyc_risk_rating: str
    identity_score: int
    sanctions_hit: bool = False
    pep_hit: bool = False


@dataclass(frozen=True)
class Factor:
    code: str
    points: int
    detail: str = ""


@dataclass(frozen=True)
class EligibilityResponse:
    decision: str
    risk_score: int
    tier: str
    factors: list[Factor] = field(default_factory=list)
    policy_version: str = POLICY_VERSION
    reason: str = ""


def age_from(date_of_birth: date, today: date | None = None) -> int:
    today = today or date.today()
    return today.year - date_of_birth.year - (
        (today.month, today.day) < (date_of_birth.month, date_of_birth.day)
    )


def _fail(reason: str) -> EligibilityResponse:
    return EligibilityResponse(decision="FAIL", risk_score=100, tier="NONE", reason=reason)


def evaluate(request: EligibilityRequest) -> EligibilityResponse:
    # Hard stops first. A good income cannot outweigh a sanctions match.
    if request.sanctions_hit:
        return _fail("SANCTIONS_HIT")
    if request.kyc_status == "FAILED":
        return _fail("KYC_FAILED")
    if request.age < MIN_AGE:
        return _fail("UNDERAGE")
    if request.age > MAX_AGE:
        return _fail("AGE_OUT_OF_RANGE")
    if request.nationality in PROHIBITED_COUNTRIES:
        return _fail("PROHIBITED_COUNTRY")

    factors: list[Factor] = []

    if request.annual_income >= Decimal("2000000"):
        factors.append(Factor("INCOME_BAND_4", 30, "income >= 20L"))
    elif request.annual_income >= Decimal("1000000"):
        factors.append(Factor("INCOME_BAND_3", 22, "income >= 10L"))
    elif request.annual_income >= Decimal("400000"):
        factors.append(Factor("INCOME_BAND_2", 14, "income >= 4L"))
    else:
        factors.append(Factor("INCOME_BAND_1", 5, "income < 4L"))

    employment_points = {
        "SALARIED": 20, "SELF_EMPLOYED": 14, "BUSINESS": 14,
        "STUDENT": 6, "RETIRED": 10, "UNEMPLOYED": 2,
    }
    factors.append(Factor(
        f"EMPLOYMENT_{request.employment_status}",
        employment_points.get(request.employment_status, 5),
    ))

    factors.append(Factor(
        f"KYC_RISK_{request.kyc_risk_rating or 'UNKNOWN'}",
        {"LOW": 30, "MEDIUM": 18, "HIGH": 4}.get(request.kyc_risk_rating, 10),
    ))

    if request.identity_score >= 90:
        factors.append(Factor("IDENTITY_STRONG", 20))
    elif request.identity_score >= 75:
        factors.append(Factor("IDENTITY_GOOD", 12))
    else:
        factors.append(Factor("IDENTITY_WEAK", 4))

    # Risk score is the inverse of accumulated confidence, so a higher number
    # always means riskier -- consistent with the fraud engine's convention.
    confidence = sum(f.points for f in factors)
    risk_score = max(0, min(100, 100 - confidence))

    if request.pep_hit:
        # A PEP is bankable, but never auto-approved.
        return EligibilityResponse("REVIEW", risk_score, "BASIC", factors,
                                   reason="PEP_MATCH")
    if request.kyc_status == "MANUAL_REVIEW":
        return EligibilityResponse("REVIEW", risk_score, "BASIC", factors,
                                   reason="KYC_MANUAL_REVIEW")

    if risk_score < 35:
        return EligibilityResponse("PASS", risk_score, "PREMIUM", factors)
    if risk_score < 55:
        return EligibilityResponse("PASS", risk_score, "STANDARD", factors)
    if risk_score < 75:
        return EligibilityResponse("PASS", risk_score, "BASIC", factors)
    return EligibilityResponse("REVIEW", risk_score, "BASIC", factors,
                               reason="RISK_SCORE_HIGH")
