"""HTTP transport for events, with request signing.

Events cross a service boundary as a signed POST to the subscriber's
``/internal/events``. Signing matters because ``/internal/*`` is only network
isolated: without it, any process that reaches the port could inject a
``payment.settled``. The signature covers the exact bytes sent.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import httpx
from django.conf import settings

from .envelope import canonical_json

logger = logging.getLogger(__name__)

__all__ = [
    "deliver",
    "sign_body",
    "verify_signature",
    "PermanentDeliveryError",
    "TransientDeliveryError",
]

_SIGNATURE_HEADER = "X-Signature"
_TIMESTAMP_HEADER = "X-Signature-Timestamp"
_MAX_CLOCK_SKEW_SECONDS = 300


class TransientDeliveryError(Exception):
    """Worth retrying: connection refused, timeout, 5xx, 429."""


class PermanentDeliveryError(Exception):
    """Not worth retrying: 4xx other than 408/429. Retrying a malformed or
    rejected event just burns the ladder and delays the DEAD signal to ops."""


def _hmac_key() -> bytes:
    key = getattr(settings, "INTERNAL_HMAC_KEY", None)
    if not key:
        raise RuntimeError("INTERNAL_HMAC_KEY is not configured")
    return key.encode() if isinstance(key, str) else key


def sign_body(body: bytes, timestamp: str) -> str:
    """Sign timestamp + body together so a captured request cannot be replayed
    later with a fresh timestamp."""
    mac = hmac.new(_hmac_key(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return mac.hexdigest()


def verify_signature(body: bytes, signature: str, timestamp: str) -> bool:
    try:
        skew = abs(time.time() - float(timestamp))
    except (TypeError, ValueError):
        return False
    if skew > _MAX_CLOCK_SKEW_SECONDS:
        logger.warning("rejecting internal request: clock skew %.0fs", skew)
        return False
    # compare_digest: constant time, so a timing side-channel can't be used to
    # forge a signature byte by byte.
    return hmac.compare_digest(sign_body(body, timestamp), signature or "")


def _endpoint(subscriber: str) -> str:
    registry = getattr(settings, "SERVICE_REGISTRY", {})
    base = registry.get(subscriber)
    if not base:
        raise PermanentDeliveryError(
            f"no base URL configured for subscriber {subscriber!r}; "
            "check SERVICE_REGISTRY"
        )
    return base.rstrip("/") + "/internal/events"


def deliver(subscriber: str, envelope_dict: dict, *, timeout: float = 5.0) -> int:
    """POST one event. Returns the HTTP status on success.

    200 means the subscriber already had this event_id -- a duplicate, which is
    a *success* for our purposes. 202 means newly accepted.
    """
    url = _endpoint(subscriber)
    body = canonical_json(envelope_dict).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        _SIGNATURE_HEADER: sign_body(body, timestamp),
        _TIMESTAMP_HEADER: timestamp,
        "X-Correlation-Id": str(envelope_dict.get("correlation_id") or ""),
        "X-Event-Id": str(envelope_dict.get("event_id") or ""),
        "User-Agent": f"platform-common/{getattr(settings, 'SERVICE_NAME', 'unknown')}",
    }

    try:
        response = httpx.post(url, content=body, headers=headers, timeout=timeout)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise TransientDeliveryError(f"{subscriber} unreachable: {exc}") from exc
    except httpx.TimeoutException as exc:
        # The subscriber may well have accepted it; we simply don't know. Retry
        # is safe because the inbox deduplicates on event_id.
        raise TransientDeliveryError(f"{subscriber} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise TransientDeliveryError(f"{subscriber} transport error: {exc}") from exc

    if 200 <= response.status_code < 300:
        return response.status_code
    if response.status_code in (408, 429) or response.status_code >= 500:
        raise TransientDeliveryError(
            f"{subscriber} returned {response.status_code}: {response.text[:200]}"
        )
    raise PermanentDeliveryError(
        f"{subscriber} rejected the event with {response.status_code}: {response.text[:200]}"
    )
