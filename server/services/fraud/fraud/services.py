"""Fraud screening. Domain logic; the only module that opens transactions.

The hot path is ``screen()``: three indexed reads plus in-memory rule
evaluation, budgeted at p99 < 50 ms. Everything that is not needed to reach a
decision -- profile updates, velocity bookkeeping -- happens *after* the
response, on the queue.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from cachetools import TTLCache
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from platform_common.errors import Conflict, NotFound
from platform_common.events import EventEnvelope, publish
from platform_common.observability.context import get_correlation_id

from .models import (
    AccountProfile,
    CaseStatus,
    Decision,
    FraudCase,
    FraudDecision,
    KnownBeneficiary,
    ListEntry,
    Resolution,
    Rule,
    RuleMode,
    RuleStat,
    ScreenedTxn,
    Threshold,
)
from .rules import FeatureVector, RuleSyntaxError, evaluate

logger = logging.getLogger(__name__)

# Rules and lists live in process memory and are refreshed on a short TTL. A
# database round trip per rule per payment would not fit the budget, and the
# rule set changes at most a few times a day.
_CACHE: TTLCache = TTLCache(maxsize=8, ttl=60)

SLA_BY_PRIORITY = {
    "CRITICAL": timedelta(minutes=15),
    "HIGH": timedelta(hours=1),
    "MEDIUM": timedelta(hours=4),
    "LOW": timedelta(hours=24),
}


@dataclass(frozen=True)
class ScreenRequest:
    txn_ref: str
    account_ref: str
    amount: Decimal
    currency: str
    rail: str
    txn_type: str = "TRANSFER"
    user_ref: str | None = None
    beneficiary_fingerprint: str = ""
    beneficiary_country: str = ""
    beneficiary_age_hours: float = 0.0
    beneficiary_in_cooling_off: bool = False
    device_fingerprint: str = ""
    ip_country: str = ""
    account_opened_at=None


@dataclass(frozen=True)
class ScreenResponse:
    decision_id: str
    decision: str
    score: int
    reason_codes: list[str]
    latency_ms: int
    case_id: str | None
    ruleset_version: str


# --------------------------------------------------------------- rule cache


def _active_rules() -> list[Rule]:
    if "rules" not in _CACHE:
        _CACHE["rules"] = list(
            Rule.objects.exclude(mode=RuleMode.DISABLED).order_by("code")
        )
    return _CACHE["rules"]


def _ruleset_version() -> str:
    if "ruleset_version" not in _CACHE:
        newest = Rule.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()
        count = Rule.objects.exclude(mode=RuleMode.DISABLED).count()
        _CACHE["ruleset_version"] = (
            f"{newest:%Y-%m-%d.%H%M}.{count}" if newest else "empty"
        )
    return _CACHE["ruleset_version"]


def _lists() -> dict[str, set[str]]:
    if "lists" not in _CACHE:
        buckets: dict[str, set[str]] = {}
        for entry in ListEntry.objects.filter(is_active=True).only("list_type", "value"):
            buckets.setdefault(entry.list_type, set()).add(entry.value.upper())
        _CACHE["lists"] = buckets
    return _CACHE["lists"]


def _thresholds() -> Threshold:
    if "thresholds" not in _CACHE:
        _CACHE["thresholds"] = (
            Threshold.objects.filter(is_active=True).order_by("-version").first()
            or Threshold(allow_below=40, block_at_or_above=75, version=0)
        )
    return _CACHE["thresholds"]


def refresh_caches() -> dict:
    """Called by a Q2 schedule and on rule change. Cheap and safe to over-call."""
    _CACHE.clear()
    return {"rules": len(_active_rules()), "ruleset_version": _ruleset_version()}


# ----------------------------------------------------------- feature assembly


def assemble_features(request: ScreenRequest) -> FeatureVector:
    """Three indexed reads plus in-memory lookups. This is the latency budget."""
    now = timezone.now()
    lists = _lists()

    # 1. single-row primary-key read
    profile = AccountProfile.objects.filter(account_ref=request.account_ref).first()

    # 2. one index range scan over the last 24h, bucketed with filtered aggregates
    since_24h = now - timedelta(hours=24)
    since_1h = now - timedelta(hours=1)
    since_5m = now - timedelta(minutes=5)
    window = ScreenedTxn.objects.filter(
        account_ref=request.account_ref, created_at__gte=since_24h
    ).aggregate(
        count_5m=Count("txn_ref", filter=Q(created_at__gte=since_5m)),
        sum_5m=Sum("amount", filter=Q(created_at__gte=since_5m)),
        count_1h=Count("txn_ref", filter=Q(created_at__gte=since_1h)),
        count_24h=Count("txn_ref"),
        benef_5m=Count(
            "benef_fingerprint", distinct=True,
            filter=Q(created_at__gte=since_5m) & ~Q(benef_fingerprint=""),
        ),
        benef_24h=Count("benef_fingerprint", distinct=True, filter=~Q(benef_fingerprint="")),
        sum_24h=Sum("amount"),
    )

    # 3. unique-index probe
    known = None
    if request.beneficiary_fingerprint:
        known = KnownBeneficiary.objects.filter(
            account_ref=request.account_ref, fingerprint=request.beneficiary_fingerprint
        ).first()

    amount = Decimal(request.amount)
    mean = profile.mean_amount if profile else Decimal("0")
    stddev = profile.stddev if profile else Decimal("0")
    max_amount = profile.max_amount if profile else Decimal("0")

    # A z-score needs at least a little history; with none, treat as neutral
    # rather than infinitely anomalous -- otherwise every first transaction
    # trips every anomaly rule.
    if profile and profile.txn_count >= 5 and stddev > 0:
        zscore = float((amount - mean) / stddev)
    else:
        zscore = 0.0

    account_age_days = 0
    if profile and profile.account_opened_at:
        account_age_days = max(0, (now - profile.account_opened_at).days)
    elif profile:
        account_age_days = max(0, (now - profile.first_seen_at).days)

    country = (request.beneficiary_country or request.ip_country or "").upper()

    return FeatureVector(
        amount=amount,
        currency=request.currency,
        rail=request.rail,
        txn_type=request.txn_type,
        hour_of_day=now.hour,
        amount_zscore=zscore,
        amount_vs_mean_ratio=float(amount / mean) if mean > 0 else 1.0,
        amount_vs_max_ratio=float(amount / max_amount) if max_amount > 0 else 1.0,
        txn_count_5m=window["count_5m"] or 0,
        txn_sum_5m=window["sum_5m"] or Decimal("0"),
        txn_count_1h=window["count_1h"] or 0,
        txn_count_24h=window["count_24h"] or 0,
        txn_sum_24h=window["sum_24h"] or Decimal("0"),
        distinct_benef_5m=window["benef_5m"] or 0,
        distinct_benef_24h=window["benef_24h"] or 0,
        beneficiary_is_new=known is None and bool(request.beneficiary_fingerprint),
        beneficiary_age_hours=request.beneficiary_age_hours,
        beneficiary_in_cooling_off=request.beneficiary_in_cooling_off,
        beneficiary_blacklisted=(
            request.beneficiary_fingerprint.upper()
            in lists.get(ListEntry.ListType.BLACKLIST_BENEFICIARY, set())
        ),
        beneficiary_txn_count=known.txn_count if known else 0,
        destination_country=country,
        destination_is_high_risk=(
            country in lists.get(ListEntry.ListType.HIGH_RISK_COUNTRY, set())
        ),
        country_changed=bool(
            profile and profile.last_country and country
            and profile.last_country != country
        ),
        device_is_new=bool(
            request.device_fingerprint
            and profile
            and profile.last_device_hash
            and profile.last_device_hash != request.device_fingerprint
        ),
        device_blocked=(
            request.device_fingerprint.upper()
            in lists.get(ListEntry.ListType.BLOCKED_DEVICE, set())
        ),
        is_first_txn=(profile is None or profile.txn_count == 0),
        account_age_days=account_age_days,
        account_txn_count=profile.txn_count if profile else 0,
    )


# ------------------------------------------------------------------ screening


def _priority(score: int, amount: Decimal) -> str:
    if score >= 90 or amount >= Decimal("500000"):
        return "CRITICAL"
    if score >= 70:
        return "HIGH"
    if score >= 50:
        return "MEDIUM"
    return "LOW"


def screen(request: ScreenRequest) -> ScreenResponse:
    """Score one transaction. Synchronous, budgeted at p99 < 50 ms."""
    started = time.perf_counter()

    existing = FraudDecision.objects.filter(txn_ref=request.txn_ref).first()
    if existing:
        # Idempotent: payments-svc retrying must not produce a second decision
        # or a second analyst case.
        logger.info("returning existing decision for txn %s", request.txn_ref)
        case = getattr(existing, "case", None)
        return ScreenResponse(
            decision_id=str(existing.id), decision=existing.decision, score=existing.score,
            reason_codes=[r["code"] for r in existing.reason_codes],
            latency_ms=existing.latency_ms,
            case_id=str(case.id) if case else None,
            ruleset_version=existing.ruleset_version,
        )

    vector = assemble_features(request)

    fired, shadow, score, hard_block = [], [], 0, False
    for rule in _active_rules():
        try:
            matched = evaluate(rule.condition, vector)
        except RuleSyntaxError:
            # One malformed rule must never fail the whole screening -- that
            # would convert a config mistake into a payments outage.
            logger.exception("rule %s failed to evaluate; skipping", rule.code)
            continue
        if not matched:
            continue

        if rule.mode == RuleMode.SHADOW:
            shadow.append(rule.reason_code)
            continue

        fired.append({"code": rule.reason_code, "weight": rule.weight, "rule": rule.code})
        score += rule.weight
        hard_block = hard_block or rule.hard_block

    score = max(0, min(score, 100))
    thresholds = _thresholds()
    if hard_block:
        decision = Decision.BLOCK
    elif score >= thresholds.block_at_or_above:
        decision = Decision.BLOCK
    elif score < thresholds.allow_below:
        decision = Decision.ALLOW
    else:
        decision = Decision.REVIEW

    latency_ms = int((time.perf_counter() - started) * 1000)

    with transaction.atomic():
        record = FraudDecision.objects.create(
            txn_ref=request.txn_ref,
            account_ref=request.account_ref,
            user_ref=request.user_ref,
            decision=decision,
            score=score,
            reason_codes=fired,
            shadow_codes=shadow,
            features={k: str(v) for k, v in vector.as_dict().items()},
            amount=Decimal(request.amount),
            currency=request.currency,
            rail=request.rail,
            ruleset_version=_ruleset_version(),
            latency_ms=latency_ms,
            correlation_id=get_correlation_id() or "",
        )

        case = None
        if decision in (Decision.REVIEW, Decision.BLOCK):
            priority = _priority(score, Decimal(request.amount))
            case = FraudCase.objects.create(
                decision=record,
                txn_ref=request.txn_ref,
                account_ref=request.account_ref,
                priority=priority,
                sla_due_at=timezone.now() + SLA_BY_PRIORITY[priority],
            )

        publish(
            EventEnvelope(
                event_type="fraud.decision_made",
                aggregate_type="fraud_decision",
                aggregate_id=str(record.id),
                sequence=0,
                producer="fraud",
                correlation_id=record.correlation_id,
                payload={
                    "decision_id": str(record.id),
                    "txn_ref": str(request.txn_ref),
                    "account_ref": str(request.account_ref),
                    "decision": decision,
                    "score": score,
                    "reason_codes": fired,
                    "shadow_codes": shadow,
                    "ruleset_version": record.ruleset_version,
                    "latency_ms": latency_ms,
                },
            )
        )
        if case is not None:
            publish(
                EventEnvelope(
                    event_type="fraud.case_opened",
                    aggregate_type="fraud_case",
                    aggregate_id=str(case.id),
                    sequence=0,
                    producer="fraud",
                    correlation_id=record.correlation_id,
                    payload={
                        "case_id": str(case.id),
                        "txn_ref": str(request.txn_ref),
                        "decision": decision,
                        "score": score,
                        "priority": case.priority,
                        "sla_due_at": case.sla_due_at.isoformat(),
                    },
                )
            )

        _bump_fired_counters(fired, shadow)

    # Off the hot path: only the *next* transaction needs this, so paying ~5ms
    # for it now would be spending the latency budget on nothing.
    _schedule_profile_update(request, decision)

    return ScreenResponse(
        decision_id=str(record.id),
        decision=decision,
        score=score,
        reason_codes=[f["code"] for f in fired],
        latency_ms=latency_ms,
        case_id=str(case.id) if case else None,
        ruleset_version=record.ruleset_version,
    )


def _bump_fired_counters(fired: list[dict], shadow: list[str]) -> None:
    from django.db.models import F

    for entry in fired:
        RuleStat.objects.filter(rule__reason_code=entry["code"]).update(
            fired_count=F("fired_count") + 1
        )
    for code in shadow:
        RuleStat.objects.filter(rule__reason_code=code).update(
            shadow_fired_count=F("shadow_fired_count") + 1
        )


def _schedule_profile_update(request: ScreenRequest, decision: str) -> None:
    from django_q.tasks import async_task

    payload = {
        "txn_ref": str(request.txn_ref),
        "account_ref": str(request.account_ref),
        "amount": str(request.amount),
        "currency": request.currency,
        "rail": request.rail,
        "benef_fingerprint": request.beneficiary_fingerprint,
        "country": (request.beneficiary_country or request.ip_country or "").upper(),
        "device_hash": request.device_fingerprint,
        "decision": decision,
    }
    try:
        async_task("fraud.tasks.update_profile", payload, q_options={"save": False})
    except Exception:
        logger.exception("could not schedule profile update for %s", request.txn_ref)


def update_profile(payload: dict) -> None:
    """Maintain the read model. Idempotent: keyed on txn_ref.

    Mean and variance use Welford's online algorithm -- numerically stable over
    millions of updates, where accumulating a sum of squares is not.
    """
    amount = Decimal(payload["amount"])
    account_ref = payload["account_ref"]

    with transaction.atomic():
        _, created = ScreenedTxn.objects.get_or_create(
            txn_ref=payload["txn_ref"],
            defaults={
                "account_ref": account_ref,
                "amount": amount,
                "currency": payload["currency"],
                "rail": payload["rail"],
                "benef_fingerprint": payload.get("benef_fingerprint", ""),
                "country": payload.get("country", ""),
                "device_hash": payload.get("device_hash", ""),
                "decision": payload.get("decision", ""),
                "created_at": timezone.now(),
            },
        )
        if not created:
            return  # already applied; a re-run must not double-count

        profile, _ = AccountProfile.objects.select_for_update().get_or_create(
            account_ref=account_ref
        )
        count = profile.txn_count + 1
        delta = amount - profile.mean_amount
        mean = profile.mean_amount + (delta / count)
        profile.m2 = profile.m2 + float(delta) * float(amount - mean)
        profile.txn_count = count
        profile.mean_amount = mean
        profile.max_amount = max(profile.max_amount, amount)
        if payload.get("country"):
            profile.last_country = payload["country"][:2]
        if payload.get("device_hash"):
            profile.last_device_hash = payload["device_hash"]
        profile.last_txn_at = timezone.now()
        profile.save()

        fingerprint = payload.get("benef_fingerprint")
        if fingerprint:
            known, made = KnownBeneficiary.objects.get_or_create(
                account_ref=account_ref,
                fingerprint=fingerprint,
                defaults={"first_seen": timezone.now(), "txn_count": 0,
                          "total_amount": Decimal("0")},
            )
            known.txn_count += 1
            known.total_amount += amount
            known.save(update_fields=["txn_count", "total_amount"])


# ----------------------------------------------------------------- case work


def _resolve_case(case_id, *, approve: bool, analyst_id: str, note: str,
                  resolution: str | None) -> FraudCase:
    with transaction.atomic():
        case = FraudCase.objects.select_for_update().filter(pk=case_id).first()
        if case is None:
            raise NotFound(f"case {case_id} not found")
        if case.status in (CaseStatus.APPROVED, CaseStatus.REJECTED):
            # Two analysts opening the same case is normal; both resolving it
            # is not. Second one loses.
            raise Conflict(
                "case is already resolved",
                detail={"status": case.status, "resolved_by": case.resolved_by},
            )

        case.status = CaseStatus.APPROVED if approve else CaseStatus.REJECTED
        case.resolution = resolution or (
            Resolution.FALSE_POSITIVE if approve else Resolution.CONFIRMED_FRAUD
        )
        case.resolved_by = analyst_id
        case.resolved_at = timezone.now()
        case.resolution_note = note
        case.save()

        _record_outcome(case)

        publish(
            EventEnvelope(
                event_type="fraud.case_approved" if approve else "fraud.case_rejected",
                aggregate_type="fraud_case",
                aggregate_id=str(case.id),
                sequence=1,
                producer="fraud",
                correlation_id=case.decision.correlation_id,
                actor={"type": "analyst", "id": analyst_id},
                payload={
                    "case_id": str(case.id),
                    "txn_ref": str(case.txn_ref),
                    "decision_id": str(case.decision_id),
                    "analyst_id": analyst_id,
                    "resolution": case.resolution,
                    "note": note,
                    "resolved_at": case.resolved_at.isoformat(),
                },
            )
        )
    return case


def _record_outcome(case: FraudCase) -> None:
    """Feed the analyst's verdict back into per-rule precision.

    This is the mechanism behind "reduce false positives": a rule whose firings
    keep being marked FALSE_POSITIVE shows a falling precision, and an admin can
    lower its weight or move it to shadow mode on evidence rather than instinct.
    """
    from django.db.models import F

    field = (
        "false_positive"
        if case.resolution == Resolution.FALSE_POSITIVE
        else "confirmed_fraud"
        if case.resolution == Resolution.CONFIRMED_FRAUD
        else None
    )
    if field is None:
        return
    codes = [entry["code"] for entry in case.decision.reason_codes]
    if codes:
        RuleStat.objects.filter(rule__reason_code__in=codes).update(**{field: F(field) + 1})


def approve_case(case_id, *, analyst_id: str, note: str = "",
                 resolution: str | None = None) -> FraudCase:
    """Release a held transaction. payments-svc resumes its saga from CAPTURE."""
    return _resolve_case(case_id, approve=True, analyst_id=analyst_id, note=note,
                         resolution=resolution)


def reject_case(case_id, *, analyst_id: str, note: str = "",
                resolution: str | None = None) -> FraudCase:
    """Confirm the block. payments-svc compensates: hold released, txn BLOCKED."""
    return _resolve_case(case_id, approve=False, analyst_id=analyst_id, note=note,
                         resolution=resolution)


def escalate_sla_breaches() -> dict:
    overdue = FraudCase.objects.filter(
        status__in=[CaseStatus.OPEN, CaseStatus.IN_REVIEW], sla_due_at__lt=timezone.now()
    )
    count = overdue.count()
    if count:
        logger.warning("%s fraud case(s) past SLA", count)
    return {"overdue": count}


# ------------------------------------------------------------- rule tuning


def dry_run_rule(condition: dict, *, sample_days: int = 7) -> dict:
    """Replay a proposed rule against stored feature vectors.

    Only possible because FraudDecision.features keeps the exact vector for
    every past screening. It turns rule tuning from guesswork into measurement.
    """
    from .rules import FACT_TYPES

    since = timezone.now() - timedelta(days=sample_days)
    decisions = FraudDecision.objects.filter(created_at__gte=since).select_related("case")

    evaluated = would_fire = 0
    overlap_fraud = overlap_false_positive = 0

    for record in decisions.iterator(chunk_size=500):
        vector = _vector_from_stored(record.features)
        if vector is None:
            continue
        evaluated += 1
        try:
            if not evaluate(condition, vector):
                continue
        except RuleSyntaxError:
            continue
        would_fire += 1
        case = getattr(record, "case", None)
        if case and case.resolution == Resolution.CONFIRMED_FRAUD:
            overlap_fraud += 1
        elif case and case.resolution == Resolution.FALSE_POSITIVE:
            overlap_false_positive += 1

    resolved = overlap_fraud + overlap_false_positive
    return {
        "evaluated": evaluated,
        "would_fire": would_fire,
        "fire_rate": round(would_fire / evaluated, 5) if evaluated else 0.0,
        "overlap_with_confirmed_fraud": overlap_fraud,
        "overlap_with_false_positives": overlap_false_positive,
        "estimated_precision": round(overlap_fraud / resolved, 4) if resolved else None,
    }


def _vector_from_stored(stored: dict) -> FeatureVector | None:
    """Rebuild a FeatureVector from the JSON snapshot (all values are strings)."""
    from .rules import FACT_TYPES

    if not stored:
        return None
    kwargs = {}
    for name, declared in FACT_TYPES.items():
        raw = stored.get(name)
        if raw is None:
            return None
        try:
            if declared in (Decimal, "Decimal"):
                kwargs[name] = Decimal(raw)
            elif declared in (bool, "bool"):
                kwargs[name] = str(raw) == "True"
            elif declared in (int, "int"):
                kwargs[name] = int(raw)
            elif declared in (float, "float"):
                kwargs[name] = float(raw)
            else:
                kwargs[name] = str(raw)
        except (ValueError, TypeError):
            return None
    return FeatureVector(**kwargs)
