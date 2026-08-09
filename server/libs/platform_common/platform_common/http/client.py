"""Service-to-service HTTP.

Every outbound call between services goes through here. A bare ``httpx`` or
``requests`` call inside a service is a code-review finding, because it will be
missing at least one of: a timeout, correlation propagation, a service token, a
circuit breaker, or -- most importantly -- the retry-safety rule.

**The retry-safety rule.** Retrying is a *correctness* question, not a
resilience one. After a timeout we do not know whether the peer applied the
request. Retrying then is safe only if the call carries an Idempotency-Key. This
client refuses to guess: a retry-after-timeout without a key raises instead of
risking a double debit.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx
from django.conf import settings

from ..auth.tokens import issue_service_token
from ..errors import ServiceUnavailable
from ..observability.context import get_correlation_id

logger = logging.getLogger(__name__)

__all__ = ["ServiceClient", "get_client", "CircuitOpen", "RemoteServiceError"]

RETRYABLE_STATUS = frozenset({502, 503, 504})


class CircuitOpen(ServiceUnavailable):
    default_code = "SERVICE_UNAVAILABLE"


class RemoteServiceError(Exception):
    """Non-2xx from a peer that we are not going to retry.

    Carries the parsed error envelope so callers can branch on the peer's
    ``code`` (e.g. INSUFFICIENT_FUNDS) rather than on a status number.
    """

    def __init__(self, service: str, status_code: int, body: dict | str):
        self.service = service
        self.status_code = status_code
        self.body = body
        error = body.get("error", {}) if isinstance(body, dict) else {}
        self.code = error.get("code", f"HTTP_{status_code}")
        self.detail = error.get("detail", {})
        self.retryable = bool(error.get("retryable", False))
        super().__init__(f"{service} -> {status_code} {self.code}")


class _CircuitBreaker:
    """Trips after N consecutive failures; half-opens after a cooldown.

    Purpose is to fail *fast* rather than burn a latency budget waiting on a
    peer that is known to be down -- particularly the 50 ms fraud budget.
    """

    def __init__(self, threshold: int = 5, reset_seconds: float = 30.0):
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._failures < self.threshold:
                return False
            if time.monotonic() - self._opened_at >= self.reset_seconds:
                return False  # half-open: allow one probe through
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures == self.threshold:
                self._opened_at = time.monotonic()
                logger.error("circuit breaker OPEN after %s failures", self._failures)


class ServiceClient:
    def __init__(
        self,
        service: str,
        *,
        timeout: float = 5.0,
        retries: int = 2,
        breaker_threshold: int = 5,
        breaker_reset: float = 30.0,
        scopes: tuple[str, ...] = (),
    ):
        base_url = settings.SERVICE_REGISTRY.get(service)
        if not base_url:
            raise ValueError(f"no base URL for service {service!r}")
        self.service = service
        self.timeout = timeout
        self.retries = retries
        self.scopes = scopes
        self.breaker = _CircuitBreaker(breaker_threshold, breaker_reset)
        # One pooled client per process. Building one per request would add a
        # TCP (and in production TLS) handshake to every call.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
            headers={"User-Agent": f"platform-common/{settings.SERVICE_NAME}"},
        )

    # ---- public API ---------------------------------------------------

    def get(self, path: str, *, params: dict | None = None, timeout: float | None = None):
        return self._request("GET", path, params=params, timeout=timeout)

    def post(self, path, json=None, *, idempotency_key=None, timeout=None):
        return self._request(
            "POST", path, json=json, idempotency_key=idempotency_key, timeout=timeout
        )

    def patch(self, path, json=None, *, idempotency_key=None, timeout=None):
        return self._request(
            "PATCH", path, json=json, idempotency_key=idempotency_key, timeout=timeout
        )

    # ---- internals ----------------------------------------------------

    def _reset_pool(self) -> None:
        """Discard pooled connections after a transport-level failure.

        httpx cannot tell a half-open socket from a healthy one until it writes
        to it, so a stale keep-alive fails every attempt that reuses the pool.
        """
        try:
            self._client.close()
        except Exception:
            logger.debug("could not close pooled connections for %s", self.service)
        self._client = httpx.Client(
            base_url=str(self._client.base_url),
            timeout=self.timeout,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
            headers={"User-Agent": f"platform-common/{settings.SERVICE_NAME}"},
        )

    def _headers(self, idempotency_key: str | None) -> dict:
        headers = {
            "Authorization": f"Bearer {issue_service_token(settings.SERVICE_NAME, self.scopes)}",
            "X-Correlation-Id": get_correlation_id() or "",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        return headers

    def _request(self, method, path, *, json=None, params=None,
                 idempotency_key=None, timeout=None):
        if self.breaker.is_open:
            raise CircuitOpen(
                f"{self.service} is unavailable (circuit open)",
                detail={"service": self.service},
            )

        idempotent_method = method in ("GET", "HEAD", "OPTIONS")
        # A retry after a timeout is only safe when the peer can deduplicate.
        safe_to_retry_on_timeout = idempotent_method or bool(idempotency_key)

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._client.request(
                    method, path, json=json, params=params,
                    headers=self._headers(idempotency_key),
                    timeout=timeout or self.timeout,
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                self.breaker.record_failure()
                if not safe_to_retry_on_timeout:
                    raise ServiceUnavailable(
                        f"{self.service} timed out and the request is not safe to retry "
                        "without an Idempotency-Key",
                        detail={"service": self.service, "method": method, "path": path},
                    ) from exc
                if attempt >= self.retries:
                    break
            except httpx.HTTPError as exc:
                # Connection-level failure: usually a keep-alive socket the peer
                # closed while idle. Retrying against the same pool would just
                # grab another dead one, so drop the pooled connections first.
                last_exc = exc
                self.breaker.record_failure()
                self._reset_pool()
                if attempt >= self.retries:
                    break
            else:
                if response.status_code in RETRYABLE_STATUS and attempt < self.retries:
                    self.breaker.record_failure()
                    time.sleep(0.1 * (2**attempt))
                    continue

                self.breaker.record_success()
                if 200 <= response.status_code < 300:
                    return response.json() if response.content else {}

                try:
                    body = response.json()
                except ValueError:
                    body = response.text[:500]
                raise RemoteServiceError(self.service, response.status_code, body)

            time.sleep(0.1 * (2**attempt))

        raise ServiceUnavailable(
            f"{self.service} unreachable after {self.retries + 1} attempts: {last_exc}",
            detail={"service": self.service},
        ) from last_exc


_clients: dict[str, ServiceClient] = {}
_clients_lock = threading.Lock()


def get_client(service: str, **kwargs) -> ServiceClient:
    """Process-wide cached client, so connection pools are actually reused."""
    key = f"{service}:{sorted(kwargs.items())}"
    with _clients_lock:
        if key not in _clients:
            _clients[key] = ServiceClient(service, **kwargs)
        return _clients[key]
