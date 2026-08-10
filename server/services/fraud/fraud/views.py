"""HTTP layer for fraud-svc."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import (
    HasScope,
    IsAdmin,
    IsFraudAnalyst,
    IsService,
    IsStaff,
)
from platform_common.auth.authentication import (
    JWTAuthentication,
    ServiceTokenAuthentication,
)

from . import services
from .models import CaseStatus, FraudCase
from .services import ScreenRequest


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes([IsService, HasScope.of("fraud:screen")])
def screen(request):
    """The 50 ms path. Called synchronously by payments-svc on every payment."""
    data = request.data
    result = services.screen(
        ScreenRequest(
            txn_ref=data["txn_ref"],
            account_ref=data["account_ref"],
            user_ref=data.get("user_ref"),
            amount=Decimal(str(data["amount"])),
            currency=data["currency"],
            rail=data["rail"],
            txn_type=data.get("txn_type", "TRANSFER"),
            beneficiary_fingerprint=data.get("beneficiary_fingerprint", ""),
            beneficiary_country=data.get("beneficiary_country", ""),
            beneficiary_age_hours=float(data.get("beneficiary_age_hours") or 0.0),
            device_fingerprint=data.get("device_fingerprint", ""),
            ip_country=data.get("ip_country", ""),
        )
    )
    return Response(
        {
            "decision_id": result.decision_id,
            "decision": result.decision,
            "score": result.score,
            "reason_codes": result.reason_codes,
            "latency_ms": result.latency_ms,
            "case_id": result.case_id,
            "ruleset_version": result.ruleset_version,
        }
    )


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsFraudAnalyst])
def list_cases(request):
    status_filter = request.query_params.get("status", CaseStatus.OPEN)
    cases = FraudCase.objects.filter(status=status_filter).select_related("decision")[:100]
    return Response(
        {
            "results": [
                {
                    "id": str(case.id),
                    "txn_ref": str(case.txn_ref),
                    "status": case.status,
                    "priority": case.priority,
                    "score": case.decision.score,
                    "reason_codes": [r["code"] for r in case.decision.reason_codes],
                    "amount": {"amount": str(case.decision.amount),
                               "currency": case.decision.currency},
                    "sla_due_at": case.sla_due_at.isoformat(),
                    "created_at": case.created_at.isoformat(),
                }
                for case in cases
            ]
        }
    )


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsFraudAnalyst])
def case_detail(request, case_id):
    """Everything an analyst needs on one screen, including per-rule precision --
    which is what makes the decision trustworthy rather than opaque."""
    from .models import RuleStat

    case = FraudCase.objects.select_related("decision").filter(pk=case_id).first()
    if case is None:
        return Response({"error": {"code": "NOT_FOUND"}}, status=404)

    precision = {
        stat.rule.reason_code: {
            "precision": stat.precision,
            "fired_count": stat.fired_count,
            "rule": stat.rule.name,
        }
        for stat in RuleStat.objects.select_related("rule")
    }
    return Response(
        {
            "id": str(case.id),
            "status": case.status,
            "priority": case.priority,
            "sla_due_at": case.sla_due_at.isoformat(),
            "transaction": {
                "txn_ref": str(case.txn_ref),
                "amount": {"amount": str(case.decision.amount),
                           "currency": case.decision.currency},
                "rail": case.decision.rail,
            },
            "decision": {
                "score": case.decision.score,
                "latency_ms": case.decision.latency_ms,
                "ruleset_version": case.decision.ruleset_version,
                "reason_codes": [
                    {**entry, **precision.get(entry["code"], {})}
                    for entry in case.decision.reason_codes
                ],
                "shadow_codes": case.decision.shadow_codes,
            },
            "features": case.decision.features,
            "audit_trail_url": f"/api/audit/logs?correlation_id={case.decision.correlation_id}",
        }
    )


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsFraudAnalyst])
def approve_case(request, case_id):
    case = services.approve_case(
        case_id, analyst_id=str(request.user.id), note=request.data.get("note", ""),
        resolution=request.data.get("resolution"),
    )
    return Response({"id": str(case.id), "status": case.status,
                     "resolution": case.resolution,
                     "message": "Transfer released. Rule statistics updated."})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsFraudAnalyst])
def reject_case(request, case_id):
    case = services.reject_case(
        case_id, analyst_id=str(request.user.id), note=request.data.get("note", ""),
        resolution=request.data.get("resolution"),
    )
    return Response({"id": str(case.id), "status": case.status,
                     "resolution": case.resolution})


@api_view(["POST"])
@authentication_classes([ServiceTokenAuthentication])
@permission_classes([IsService])
def refresh_caches(request):
    """Force this replica to reload rules, thresholds and lists.

    Rules are cached in process memory for 60s so screening never pays for a
    database round trip. That TTL means an admin change self-heals within a
    minute, but a change should be able to take effect *now* -- so the admin API
    (and the fraud.rule_updated handler) call this on every replica.
    """
    return Response(services.refresh_caches())


# ---------------------------------------------------------------------------
# administrator: rule and threshold configuration
# ---------------------------------------------------------------------------


def _rule_json(rule) -> dict:
    stat = getattr(rule, "stat", None)
    return {
        "id": str(rule.id),
        "code": rule.code,
        "name": rule.name,
        "description": rule.description,
        "condition": rule.condition,
        "weight": rule.weight,
        "hard_block": rule.hard_block,
        "mode": rule.mode,
        "reason_code": rule.reason_code,
        "category": rule.category,
        "version": rule.version,
        "updated_by": rule.updated_by,
        "updated_at": rule.updated_at.isoformat(),
        "stat": {
            "fired_count": stat.fired_count,
            "shadow_fired_count": stat.shadow_fired_count,
            "confirmed_fraud": stat.confirmed_fraud,
            "false_positive": stat.false_positive,
            # None means "not enough resolved cases to judge" -- which is very
            # different from zero, and the UI must not round it to 0%.
            "precision": stat.precision,
        } if stat else None,
    }


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def list_rules(request):
    from .models import Rule

    rules = Rule.objects.select_related("stat").order_by("category", "code")
    if mode := request.query_params.get("mode"):
        rules = rules.filter(mode=mode)
    return Response({"results": [_rule_json(r) for r in rules]})


@api_view(["PATCH"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAdmin])
def update_rule(request, rule_id):
    """Change a rule's weight, mode or condition.

    Conditions are validated here and rejected on syntax error, because an
    unparseable rule saved into the active set would throw on the 50 ms
    screening path for every subsequent payment.
    """
    from .models import Rule, RuleMode
    from .rules import RuleSyntaxError, validate_condition

    rule = Rule.objects.filter(pk=rule_id).first()
    if rule is None:
        return Response({"error": {"code": "NOT_FOUND"}}, status=404)

    changed = []
    if "condition" in request.data:
        try:
            validate_condition(request.data["condition"])
        except RuleSyntaxError as exc:
            return Response(
                {"error": {"code": "INVALID_RULE_CONDITION", "message": str(exc)}},
                status=400,
            )
        rule.condition = request.data["condition"]
        changed.append("condition")

    if "weight" in request.data:
        try:
            weight = int(request.data["weight"])
        except (TypeError, ValueError):
            return Response(
                {"error": {"code": "VALIDATION_ERROR", "message": "weight must be an integer"}},
                status=400,
            )
        if not 0 <= weight <= 100:
            return Response(
                {"error": {"code": "VALIDATION_ERROR",
                           "message": "weight must be between 0 and 100"}},
                status=400,
            )
        rule.weight = weight
        changed.append("weight")

    if "mode" in request.data:
        if request.data["mode"] not in RuleMode.values:
            return Response(
                {"error": {"code": "VALIDATION_ERROR",
                           "message": f"mode must be one of {RuleMode.values}"}},
                status=400,
            )
        rule.mode = request.data["mode"]
        changed.append("mode")

    if "hard_block" in request.data:
        rule.hard_block = bool(request.data["hard_block"])
        changed.append("hard_block")

    if not changed:
        return Response(
            {"error": {"code": "VALIDATION_ERROR", "message": "Nothing to change."}},
            status=400,
        )

    rule.version += 1
    rule.updated_by = str(request.user.id)
    rule.save(update_fields=[*changed, "version", "updated_by", "updated_at"])

    # Every replica caches rules for 60s. Without this the admin watches their
    # change appear to do nothing for up to a minute.
    services.refresh_caches()
    return Response({**_rule_json(rule), "changed": changed})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def dry_run(request):
    """Replay a proposed condition against real stored feature vectors.

    The measurement that lets an admin see a rule's fire rate and likely
    precision *before* it can decline a customer's payment.
    """
    from .rules import RuleSyntaxError, validate_condition

    condition = request.data.get("condition")
    if not isinstance(condition, dict):
        return Response(
            {"error": {"code": "VALIDATION_ERROR", "message": "condition object required"}},
            status=400,
        )
    try:
        validate_condition(condition)
    except RuleSyntaxError as exc:
        return Response(
            {"error": {"code": "INVALID_RULE_CONDITION", "message": str(exc)}},
            status=400,
        )

    days = min(int(request.data.get("sample_days", 7)), 90)
    return Response(services.dry_run_rule(condition, sample_days=days))


@api_view(["GET", "PATCH"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def thresholds(request):
    from decimal import Decimal as _Decimal

    from .models import Threshold

    threshold = Threshold.objects.filter(is_active=True).order_by("-version").first()
    if threshold is None:
        threshold = Threshold.objects.create()

    if request.method == "PATCH":
        if getattr(request.user, "role", "") != "ADMIN":
            return Response(
                {"error": {"code": "FORBIDDEN",
                           "message": "Only an administrator may change thresholds."}},
                status=403,
            )
        allow_below = int(request.data.get("allow_below", threshold.allow_below))
        block_at = int(request.data.get("block_at_or_above", threshold.block_at_or_above))
        if not 0 <= allow_below <= block_at <= 100:
            return Response(
                {"error": {"code": "VALIDATION_ERROR",
                           "message": "Require 0 <= allow_below <= block_at_or_above <= 100."}},
                status=400,
            )
        threshold.allow_below = allow_below
        threshold.block_at_or_above = block_at
        if "safe_harbour_amount" in request.data:
            threshold.safe_harbour_amount = _Decimal(str(request.data["safe_harbour_amount"]))
        threshold.version += 1
        threshold.updated_by = str(request.user.id)
        threshold.save()
        services.refresh_caches()

    return Response({
        "allow_below": threshold.allow_below,
        "block_at_or_above": threshold.block_at_or_above,
        "safe_harbour_amount": str(threshold.safe_harbour_amount),
        "version": threshold.version,
        "updated_by": threshold.updated_by,
        "updated_at": threshold.updated_at.isoformat(),
        # Stated explicitly so the UI can label the bands without re-deriving
        # them and getting an off-by-one at the boundary.
        "bands": {
            "allow": f"0-{threshold.allow_below - 1}",
            "review": f"{threshold.allow_below}-{threshold.block_at_or_above - 1}",
            "block": f"{threshold.block_at_or_above}-100",
        },
    })


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def case_stats(request):
    """Queue and outcome counters for the analyst and admin dashboards."""
    from django.db.models import Count
    from django.utils import timezone

    from .models import FraudDecision

    since = timezone.now() - timedelta(days=int(request.query_params.get("days", 7)))
    decisions = FraudDecision.objects.filter(created_at__gte=since)

    return Response({
        "window_days": int(request.query_params.get("days", 7)),
        "screened": decisions.count(),
        "by_decision": list(
            decisions.values("decision").annotate(count=Count("id")).order_by()
        ),
        "by_case_status": list(
            FraudCase.objects.values("status").annotate(count=Count("id")).order_by()
        ),
        "by_resolution": list(
            FraudCase.objects.exclude(resolution="")
            .values("resolution").annotate(count=Count("id")).order_by()
        ),
        "open_cases": FraudCase.objects.filter(status=CaseStatus.OPEN).count(),
        "breaching_sla": FraudCase.objects.filter(
            status=CaseStatus.OPEN, sla_due_at__lt=timezone.now()
        ).count(),
    })
