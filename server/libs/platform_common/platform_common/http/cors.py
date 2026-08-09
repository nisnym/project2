"""CORS for the browser SPA, without adding a dependency.

The SPA talks to ten services on ten ports. In development the Vite proxy
fronts them all on one origin, so CORS never comes up -- but a developer
running ``npm run build && npm run preview``, or pointing the SPA straight at a
service, hits a cross-origin request immediately. Rather than make that a
confusing failure, this middleware answers it.

Deliberately *not* ``django-cors-headers``: one more package to pin for about
forty lines of logic we want to be able to read.

Two rules this enforces that a permissive implementation gets wrong:

1. ``Access-Control-Allow-Origin`` is echoed from an allow-list, never ``*``.
   With ``*`` the browser refuses to send credentials, and -- worse -- ``*``
   plus ``Allow-Credentials`` is a spec violation that some proxies happily
   pass through. The allow-list keeps refresh cookies workable.
2. ``Vary: Origin`` is always set. Without it a shared cache can serve one
   origin's allow header to a different origin.
"""

from __future__ import annotations

import os

from django.http import HttpResponse

__all__ = ["CorsMiddleware", "allowed_origins"]

DEFAULT_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",   # vite preview
    "http://127.0.0.1:4173",
)

ALLOWED_HEADERS = (
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "X-Correlation-Id",
    "X-Request-Id",
)

EXPOSED_HEADERS = ("X-Correlation-Id", "X-Request-Id")

ALLOWED_METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS")

PREFLIGHT_MAX_AGE = "600"


def allowed_origins() -> frozenset[str]:
    configured = os.environ.get("CORS_ALLOWED_ORIGINS")
    if configured:
        return frozenset(o.strip() for o in configured.split(",") if o.strip())
    return frozenset(DEFAULT_ORIGINS)


class CorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.origins = allowed_origins()

    def __call__(self, request):
        origin = request.META.get("HTTP_ORIGIN")

        # A preflight must never reach a view: it carries no credentials, so
        # authentication would reject it before the real request is ever sent.
        if request.method == "OPTIONS" and "HTTP_ACCESS_CONTROL_REQUEST_METHOD" in request.META:
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        response["Vary"] = (
            f"{response['Vary']}, Origin" if response.has_header("Vary") else "Origin"
        )

        if origin and origin in self.origins:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Credentials"] = "true"
            response["Access-Control-Allow-Methods"] = ", ".join(ALLOWED_METHODS)
            response["Access-Control-Allow-Headers"] = ", ".join(ALLOWED_HEADERS)
            response["Access-Control-Expose-Headers"] = ", ".join(EXPOSED_HEADERS)
            response["Access-Control-Max-Age"] = PREFLIGHT_MAX_AGE

        return response
