"""``POST /internal/events`` -- the ingest endpoint, identical in every service.

Contract:
  202  newly accepted; handlers will run asynchronously
  200  already had this event_id -- duplicate, nothing done (still a success)
  401  bad or missing signature
  400  malformed envelope (permanent: the publisher will mark it DEAD, not retry)

The endpoint does the minimum possible work before returning: write one row,
commit, return. Handlers run on this service's own queue, so a slow handler
never holds the publisher's relay worker open or provokes a spurious retry.
"""

from __future__ import annotations

import functools
import json
import logging

from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..models import InboxEvent
from .envelope import EventEnvelope
from .transport import verify_signature

logger = logging.getLogger(__name__)

__all__ = ["ingest"]


@csrf_exempt
@require_POST
def ingest(request):
    raw = request.body

    if not verify_signature(
        raw,
        request.headers.get("X-Signature", ""),
        request.headers.get("X-Signature-Timestamp", ""),
    ):
        logger.warning(
            "rejected unsigned/badly-signed internal event from %s",
            request.META.get("REMOTE_ADDR"),
        )
        return JsonResponse(
            {"error": {"code": "INVALID_SIGNATURE"}}, status=401
        )

    try:
        data = json.loads(raw)
        envelope = EventEnvelope.from_dict(data)
    except Exception as exc:
        # 400 -> the publisher classifies this as permanent and marks it DEAD
        # rather than retrying a payload that will never parse.
        logger.error("malformed event envelope: %s", exc)
        return JsonResponse(
            {"error": {"code": "MALFORMED_ENVELOPE", "message": str(exc)}}, status=400
        )

    try:
        with transaction.atomic():
            InboxEvent.objects.create(
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                aggregate_id=str(envelope.aggregate_id),
                sequence=envelope.sequence,
                producer=envelope.producer,
                correlation_id=envelope.correlation_id or "",
                envelope=data,
            )
            transaction.on_commit(
                functools.partial(_enqueue_dispatch, str(envelope.event_id))
            )
    except IntegrityError:
        # Duplicate delivery. This is the mechanism that turns at-least-once
        # delivery into effect-once processing -- the common case, not an error.
        logger.debug("duplicate event %s ignored", envelope.event_id)
        return JsonResponse({"status": "duplicate"}, status=200)

    return JsonResponse({"status": "accepted"}, status=202)


def _enqueue_dispatch(event_id: str) -> None:
    from django_q.tasks import async_task

    try:
        async_task(
            "platform_common.events.tasks.dispatch_one",
            event_id,
            q_options={"save": False, "timeout": 60},
        )
    except Exception:
        # The row is committed; sweep_inbox will pick it up within a minute.
        logger.exception("could not enqueue dispatch for %s; sweeper will retry", event_id)


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")
