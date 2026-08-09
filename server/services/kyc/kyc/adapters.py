"""External KYC providers, behind interfaces with simulated implementations.

Simulated results are **deterministic by input hash**: the same test customer
always gets the same outcome, so demos are repeatable and tests are not flaky.
Seeded "known bad" names always produce a sanctions hit, so the reject path is
demonstrable on demand rather than by luck.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class IdentityResult:
    score: int
    matched: bool
    provider_ref: str


@dataclass(frozen=True)
class DocumentResult:
    authenticity_score: int
    ocr: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScreeningResult:
    hits: list = field(default_factory=list)


# Names that always trigger a sanctions or PEP hit in the simulator.
SANCTIONED_NAMES = {"viktor petrov", "ahmed al-rashid"}
PEP_NAMES = {"maria santos"}


def _score_from(value: str, floor: int = 55, ceiling: int = 99) -> int:
    digest = hashlib.sha256(value.encode()).hexdigest()
    return floor + int(digest[:4], 16) % (ceiling - floor + 1)


class IdentityPort(Protocol):
    def verify(self, *, full_name: str, date_of_birth: date, national_id: str,
               nationality: str) -> IdentityResult: ...


class SimulatedIdentityProvider:
    def verify(self, *, full_name, date_of_birth, national_id, nationality):
        seed = f"{full_name}|{date_of_birth}|{national_id}"
        score = _score_from(seed)
        return IdentityResult(
            score=score, matched=score >= 70,
            provider_ref=f"IDV-{hashlib.sha256(seed.encode()).hexdigest()[:10].upper()}",
        )


class DocumentPort(Protocol):
    def analyse(self, *, doc_type: str, sha256: str) -> DocumentResult: ...


class SimulatedDocumentProvider:
    def analyse(self, *, doc_type, sha256):
        score = _score_from(f"{doc_type}|{sha256}", floor=60)
        return DocumentResult(
            authenticity_score=score,
            ocr={"doc_type": doc_type, "legible": score >= 65},
        )


class SanctionsPort(Protocol):
    def screen(self, *, full_name: str, date_of_birth: date,
               nationality: str) -> ScreeningResult: ...


class SimulatedSanctionsProvider:
    def screen(self, *, full_name, date_of_birth, nationality):
        normalised = full_name.strip().lower()
        hits = []
        if normalised in SANCTIONED_NAMES:
            hits.append({"list_name": "OFAC", "matched_name": full_name,
                         "match_score": 97, "is_pep": False})
        if normalised in PEP_NAMES:
            hits.append({"list_name": "INTERNAL_PEP", "matched_name": full_name,
                         "match_score": 91, "is_pep": True})
        return ScreeningResult(hits=hits)


identity_provider: IdentityPort = SimulatedIdentityProvider()
document_provider: DocumentPort = SimulatedDocumentProvider()
sanctions_provider: SanctionsPort = SimulatedSanctionsProvider()
