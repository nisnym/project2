"""Service-to-service HTTP.

Downstream calls fail in two ways during a live demo: the service is not
running, or it is slow. Both are handled here rather than in each caller —
a dead dependency becomes an UpstreamError with a human-readable message,
never a stack trace on stage.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

from .config import settings
from .errors import AppError, NotFoundError, UpstreamError
from .models import ServiceStatus


class ServiceClient:
    def __init__(self, name: str, base_url: str, timeout: Optional[float] = None) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout or settings.upstream_timeout_seconds
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # -- calls ---------------------------------------------------------

    async def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", path, params=clean)

    async def post(self, path: str, json: Any = None) -> Any:
        return await self._request("POST", path, json=json)

    async def put(self, path: str, json: Any = None) -> Any:
        return await self._request("PUT", path, json=json)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            res = await self._get_client().request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise UpstreamError(
                f"The {self.name} service did not respond in time.",
                hint=f"{self.base_url}{path} timed out after {self.timeout}s.",
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"The {self.name} service is unreachable right now.",
                hint=f"Is it running on {self.base_url}? (./start.sh boots all services.)",
            ) from exc

        if res.status_code >= 400:
            payload = _safe_json(res) or {}
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = err.get("message") or f"{self.name} returned HTTP {res.status_code}."
            if res.status_code == 404:
                raise NotFoundError(message, hint=err.get("hint"))
            raise AppError(
                message,
                code=err.get("code", "upstream_error"),
                status_code=502 if res.status_code >= 500 else res.status_code,
                hint=err.get("hint"),
            )

        return _safe_json(res)

    # -- health --------------------------------------------------------

    async def probe(self) -> ServiceStatus:
        """Never raises — a health check that can fail is not a health check."""
        started = time.perf_counter()
        try:
            res = await self._get_client().get("/health", timeout=2.5)
            latency = round((time.perf_counter() - started) * 1000, 1)
            if res.status_code < 400:
                return ServiceStatus(
                    name=self.name, url=self.base_url, status="up", latencyMs=latency
                )
            return ServiceStatus(
                name=self.name,
                url=self.base_url,
                status="down",
                latencyMs=latency,
                detail=f"HTTP {res.status_code}",
            )
        except Exception as exc:  # noqa: BLE001 - deliberately total
            return ServiceStatus(
                name=self.name,
                url=self.base_url,
                status="down",
                detail=type(exc).__name__,
            )


def _safe_json(res: httpx.Response) -> Any:
    try:
        return res.json()
    except ValueError:
        return None


async def gather(*aws: Any) -> list[Any]:
    """Run downstream calls concurrently; the gateway does a lot of fan-out."""
    return list(await asyncio.gather(*aws))
