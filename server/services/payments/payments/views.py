"""HTTP layer: authorise, parse, delegate, respond.

No business logic -- if there's an `if` about the domain, it belongs in
services.py.
"""

from __future__ import annotations

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import IsCustomer, IsStaff
from platform_common.auth.authentication import JWTAuthentication
from platform_common.errors import NotFound

from . import services
from .models import FundingSource, SagaStep, Transaction, TransferSchedule, TxnStatus
from .serializers import (
    AddFundingSourceSerializer,
    CreateFundingSerializer,
    CreateScheduleSerializer,
    CreateTransferSerializer,
)

# ---------------------------------------------------------------------------
# representations
# ---------------------------------------------------------------------------


def _txn_json(txn: Transaction) -> dict:
    return {
        "id": str(txn.id),
        "reference": txn.reference,
        "txn_type": txn.txn_type,
        "rail": txn.rail,
        "direction": txn.direction,
        "amount": str(txn.amount),
        "currency": txn.currency,
        "status": txn.status,
        "status_reason": txn.status_reason,
        "account_id": str(txn.account_id),
        "beneficiary_id": str(txn.beneficiary_id) if txn.beneficiary_id else None,
        "beneficiary_masked": txn.beneficiary_masked,
        "funding_source_id": (
            str(txn.funding_source_id) if txn.funding_source_id else None
        ),
        "fraud_score": txn.fraud_score,
        "fraud_case_id": str(txn.fraud_case_id) if txn.fraud_case_id else None,
        "remarks": txn.remarks,
        "purpose_code": txn.purpose_code,
        "rail_ref": txn.rail_ref,
        "schedule_id": str(txn.schedule_id) if txn.schedule_id else None,
        "correlation_id": txn.correlation_id,
        "is_cancellable": txn.is_cancellable,
        "created_at": txn.created_at.isoformat(),
        "updated_at": txn.updated_at.isoformat(),
        "settled_at": txn.settled_at.isoformat() if txn.settled_at else None,
    }


def _schedule_json(schedule: TransferSchedule) -> dict:
    return {
        "id": str(schedule.id),
        "account_id": str(schedule.account_id),
        "beneficiary_id": str(schedule.beneficiary_id),
        "amount": str(schedule.amount),
        "currency": schedule.currency,
        "rail": schedule.rail,
        "frequency": schedule.frequency,
        "status": schedule.status,
        "remarks": schedule.remarks,
        "next_run_at": schedule.next_run_at.isoformat(),
        "end_date": schedule.end_date.isoformat() if schedule.end_date else None,
        "max_runs": schedule.max_runs,
        "runs_completed": schedule.runs_completed,
        "consecutive_failures": schedule.consecutive_failures,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "created_at": schedule.created_at.isoformat(),
    }


def _source_json(source: FundingSource) -> dict:
    return {
        "id": str(source.id),
        "source_type": source.source_type,
        "display_name": source.display_name,
        "currency": source.currency,
        "verified": source.verified,
        "verification_method": source.verification_method,
        "is_active": source.is_active,
        "created_at": source.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# money movement
# ---------------------------------------------------------------------------


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def create_transfer(request):
    """Create and immediately run the saga.

    The response status is always the one the saga produced, including for a
    replay: ``with_idempotency`` stores it so a retried request cannot report a
    different outcome than the original.
    """
    payload = CreateTransferSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = payload.validated_data

    def run():
        txn = services.create_transfer(
            user_id=request.user.id,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            context=_client_context(request),
            **data,
        )
        return 201, _txn_json(services.submit(txn))

    status_code, body = services.with_idempotency(
        user_id=request.user.id,
        key=request.headers.get("Idempotency-Key", ""),
        endpoint="create_transfer",
        body={**{k: str(v) for k, v in data.items()}},
        run=run,
    )
    return Response(body, status=status_code)


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def create_funding(request):
    payload = CreateFundingSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = payload.validated_data

    def run():
        txn = services.create_funding(
            user_id=request.user.id,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            context=_client_context(request),
            **data,
        )
        return 201, _txn_json(services.submit(txn))

    status_code, body = services.with_idempotency(
        user_id=request.user.id,
        key=request.headers.get("Idempotency-Key", ""),
        endpoint="create_funding",
        body={**{k: str(v) for k, v in data.items()}},
        run=run,
    )
    return Response(body, status=status_code)


def _client_context(request) -> dict:
    """Signals the fraud rules need, captured at the only place that has them."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return {
        "device_fingerprint": request.headers.get("X-Device-Fingerprint", ""),
        "ip_country": request.headers.get("X-Ip-Country", ""),
        "ip": (forwarded.split(",")[0].strip()
               or request.META.get("REMOTE_ADDR", "")),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200],
    }


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def cancel_transaction(request, txn_id):
    txn = services.cancel(
        txn_id, user_id=request.user.id,
        reason=request.data.get("reason", "cancelled by customer"),
    )
    return Response(_txn_json(txn))


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def list_transactions(request):
    query = Transaction.objects.filter(user_id=request.user.id)

    if account_id := request.query_params.get("account_id"):
        query = query.filter(account_id=account_id)
    if status_filter := request.query_params.get("status"):
        query = query.filter(status__in=status_filter.split(","))
    if txn_type := request.query_params.get("txn_type"):
        query = query.filter(txn_type=txn_type)
    if search := request.query_params.get("q"):
        query = query.filter(reference__icontains=search)

    # Keyset pagination on created_at: with rows arriving constantly, an OFFSET
    # page boundary shifts underneath the reader and duplicates or skips rows.
    if before := request.query_params.get("before"):
        query = query.filter(created_at__lt=before)

    limit = min(int(request.query_params.get("limit", 25)), 100)
    rows = list(query.order_by("-created_at", "-id")[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]

    return Response({
        "results": [_txn_json(t) for t in rows],
        "next_cursor": rows[-1].created_at.isoformat() if has_more and rows else None,
        "has_more": has_more,
    })


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def transaction_detail(request, txn_id):
    txn = Transaction.objects.filter(pk=txn_id, user_id=request.user.id).first()
    if txn is None:
        raise NotFound("Transaction not found.")

    steps = SagaStep.objects.filter(transaction=txn).order_by("started_at")
    return Response({
        **_txn_json(txn),
        # The saga trace is what turns "why is my transfer stuck" from a support
        # ticket into something the customer can see for themselves.
        "steps": [
            {
                "name": s.name, "status": s.status, "attempt": s.attempt,
                "error": s.error,
                "started_at": s.started_at.isoformat(),
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
            }
            for s in steps
        ],
    })


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def transaction_summary(request):
    """Counts by status for the dashboard, in one query instead of N."""
    from django.db.models import Count, Sum

    rows = (
        Transaction.objects.filter(user_id=request.user.id)
        .values("status")
        .annotate(count=Count("id"), total=Sum("amount"))
    )
    by_status = {
        row["status"]: {"count": row["count"], "total": str(row["total"] or 0)}
        for row in rows
    }
    return Response({
        "by_status": by_status,
        "in_flight": sum(
            entry["count"] for status, entry in by_status.items()
            if status not in (TxnStatus.SETTLED, TxnStatus.BLOCKED, TxnStatus.REJECTED,
                              TxnStatus.CANCELLED, TxnStatus.EXPIRED, TxnStatus.REVERSED)
        ),
        "needs_review": by_status.get(TxnStatus.UNDER_REVIEW, {}).get("count", 0),
    })


# ---------------------------------------------------------------------------
# scheduled and recurring transfers
# ---------------------------------------------------------------------------


@api_view(["GET", "POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def schedules(request):
    if request.method == "GET":
        query = TransferSchedule.objects.filter(user_id=request.user.id)
        if status_filter := request.query_params.get("status"):
            query = query.filter(status=status_filter)
        return Response({
            "results": [_schedule_json(s) for s in query.order_by("next_run_at")[:200]]
        })

    payload = CreateScheduleSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    schedule = services.create_schedule(user_id=request.user.id, **payload.validated_data)
    return Response(_schedule_json(schedule), status=201)


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def schedule_detail(request, schedule_id):
    schedule = TransferSchedule.objects.filter(
        pk=schedule_id, user_id=request.user.id
    ).first()
    if schedule is None:
        raise NotFound("Schedule not found.")

    if request.method == "DELETE":
        schedule.status = TransferSchedule.Status.CANCELLED
        schedule.save(update_fields=["status"])
    elif request.method == "PATCH":
        wanted = request.data.get("status")
        allowed = {TransferSchedule.Status.ACTIVE, TransferSchedule.Status.PAUSED}
        if wanted in allowed and schedule.status in allowed:
            schedule.status = wanted
            schedule.save(update_fields=["status"])

    return Response(_schedule_json(schedule))


# ---------------------------------------------------------------------------
# funding sources
# ---------------------------------------------------------------------------


@api_view(["GET", "POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsCustomer])
def funding_sources(request):
    if request.method == "GET":
        sources = FundingSource.objects.filter(
            user_id=request.user.id, is_active=True
        ).order_by("created_at")
        return Response({"results": [_source_json(s) for s in sources]})

    payload = AddFundingSourceSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    source = FundingSource.objects.create(
        user_id=request.user.id,
        # Simulated environment: a real deployment verifies with a micro-deposit
        # or a 3DS challenge before the source may fund anything.
        verified=True,
        verification_method="SIMULATED",
        **payload.validated_data,
    )
    return Response(_source_json(source), status=201)


# ---------------------------------------------------------------------------
# staff
# ---------------------------------------------------------------------------


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsStaff])
def staff_transaction_detail(request, txn_id):
    """Read-only view for an analyst working a case or ops chasing a failure.

    Staff reach transactions through this endpoint rather than by presenting an
    elevated token to the customer route, so the two access paths stay
    separately auditable.
    """
    txn = Transaction.objects.filter(pk=txn_id).first()
    if txn is None:
        txn = Transaction.objects.filter(reference=str(txn_id)).first()
    if txn is None:
        raise NotFound("Transaction not found.")

    steps = SagaStep.objects.filter(transaction=txn).order_by("started_at")
    return Response({
        **_txn_json(txn),
        "user_id": str(txn.user_id),
        "hold_id": str(txn.hold_id) if txn.hold_id else None,
        "journal_entry_id": (
            str(txn.journal_entry_id) if txn.journal_entry_id else None
        ),
        "context": txn.context,
        "steps": [
            {
                "name": s.name, "status": s.status, "attempt": s.attempt,
                "error": s.error, "request": s.request, "response": s.response,
                "started_at": s.started_at.isoformat(),
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
            }
            for s in steps
        ],
    })
