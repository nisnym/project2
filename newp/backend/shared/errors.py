"""One error shape for the whole estate.

Every failure — a bad customer id, a downstream that fell over, an unhandled
crash — comes back as {"error": {code, message, hint}}. The SPA can render
`message` verbatim to a customer, and `hint` tells the operator what to do.
"""

from __future__ import annotations

from typing import Optional


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        hint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.hint = hint

    def to_payload(self) -> dict:
        body: dict = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        return {"error": body}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class UpstreamError(AppError):
    """A downstream service is unreachable or returned garbage.

    503 rather than 500: the request was fine, the estate is degraded.
    """
    status_code = 503
    code = "upstream_unavailable"
