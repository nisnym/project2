"""One error envelope for every service.

    {"error": {"code", "message", "detail", "field_errors",
               "correlation_id", "retryable"}}

``retryable`` is a machine-readable contract, not decoration: true means the
client may safely resend the identical request with the same Idempotency-Key.
Getting it wrong on a money endpoint causes either a double debit or a stuck
payment, so it is set explicitly per error class and never inferred.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.http import Http404
from rest_framework import status as http_status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)

__all__ = [
    "DomainError",
    "ValidationFailed",
    "NotFound",
    "Forbidden",
    "Conflict",
    "IllegalStateTransition",
    "InsufficientFunds",
    "LimitExceeded",
    "ServiceUnavailable",
    "IdempotencyConflict",
    "RequestInProgress",
    "TransactionBlocked",
    "exception_handler",
]


class DomainError(APIException):
    """Base for every business-rule failure.

    Subclasses set ``status_code``, ``default_code`` and ``retryable``.
    """

    status_code = http_status.HTTP_400_BAD_REQUEST
    default_code = "DOMAIN_ERROR"
    default_detail = "The request could not be completed."
    retryable = False

    def __init__(self, message: str | None = None, *, detail: dict | None = None,
                 code: str | None = None):
        self.message = message or self.default_detail
        self.extra = detail or {}
        self.code = code or self.default_code
        super().__init__(self.message)


class ValidationFailed(DomainError):
    status_code = http_status.HTTP_400_BAD_REQUEST
    default_code = "VALIDATION_ERROR"
    default_detail = "The request payload is invalid."


class NotFound(DomainError):
    status_code = http_status.HTTP_404_NOT_FOUND
    default_code = "NOT_FOUND"
    default_detail = "Not found."


class Forbidden(DomainError):
    status_code = http_status.HTTP_403_FORBIDDEN
    default_code = "FORBIDDEN"
    default_detail = "You do not have access to this resource."


class Conflict(DomainError):
    status_code = http_status.HTTP_409_CONFLICT
    default_code = "CONFLICT"
    default_detail = "The request conflicts with the current state."


class IllegalStateTransition(Conflict):
    default_code = "ILLEGAL_STATE_TRANSITION"
    default_detail = "That action is not allowed from the current state."


class IdempotencyConflict(Conflict):
    default_code = "IDEMPOTENCY_KEY_CONFLICT"
    default_detail = "This Idempotency-Key was already used with a different payload."


class RequestInProgress(Conflict):
    default_code = "REQUEST_IN_PROGRESS"
    default_detail = "An identical request is still being processed."
    # Safe to retry: the idempotency record will replay the original response.
    retryable = True


class InsufficientFunds(DomainError):
    status_code = http_status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "INSUFFICIENT_FUNDS"
    default_detail = "Available balance is lower than the requested amount."


class LimitExceeded(DomainError):
    status_code = http_status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "LIMIT_EXCEEDED"
    default_detail = "This transaction exceeds a configured limit."


class TransactionBlocked(DomainError):
    status_code = http_status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "TRANSACTION_BLOCKED"
    # Deliberately uninformative. Telling a fraudster which rule fired tells
    # them how to evade it; reason codes appear only on analyst endpoints.
    default_detail = "We couldn't complete this transaction. Please contact support."


class ServiceUnavailable(DomainError):
    status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "SERVICE_UNAVAILABLE"
    default_detail = "A downstream service is unavailable. Please retry."
    retryable = True


def _correlation_id() -> str:
    from .observability.context import get_correlation_id

    return get_correlation_id() or ""


def _envelope(code, message, *, detail=None, field_errors=None, retryable=False):
    body = {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": _correlation_id(),
            "retryable": retryable,
        }
    }
    if detail:
        body["error"]["detail"] = detail
    if field_errors:
        body["error"]["field_errors"] = field_errors
    return body


def exception_handler(exc, context):
    """DRF hook. Everything leaving a service as a non-2xx passes through here."""

    if isinstance(exc, DomainError):
        return Response(
            _envelope(exc.code, exc.message, detail=exc.extra, retryable=exc.retryable),
            status=exc.status_code,
        )

    if isinstance(exc, ValidationError):
        detail = exc.detail
        field_errors = detail if isinstance(detail, dict) else {"non_field_errors": detail}
        return Response(
            _envelope(
                "VALIDATION_ERROR",
                "The request payload is invalid.",
                field_errors=field_errors,
            ),
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, DjangoValidationError):
        return Response(
            _envelope(
                "VALIDATION_ERROR",
                "The request payload is invalid.",
                field_errors={"non_field_errors": list(exc.messages)},
            ),
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, Http404):
        return Response(
            _envelope("NOT_FOUND", "Not found."), status=http_status.HTTP_404_NOT_FOUND
        )

    if isinstance(exc, IntegrityError):
        # Usually a unique constraint doing its job under a race. 409 rather
        # than 500: the client's request lost a legitimate contest.
        logger.warning("integrity error surfaced to the API: %s", exc)
        return Response(
            _envelope("CONFLICT", "The request conflicts with existing data."),
            status=http_status.HTTP_409_CONFLICT,
        )

    response = drf_exception_handler(exc, context)
    if response is not None:
        code = getattr(exc, "default_code", "ERROR")
        message = (
            exc.detail if isinstance(getattr(exc, "detail", None), str) else str(exc)
        )
        retryable = response.status_code in (429, 502, 503, 504)
        return Response(
            _envelope(str(code).upper(), message, retryable=retryable),
            status=response.status_code,
        )

    # Unhandled: log with the correlation id, return nothing internal.
    logger.exception("unhandled exception [correlation_id=%s]", _correlation_id())
    return Response(
        _envelope("INTERNAL_ERROR", "An unexpected error occurred.", retryable=True),
        status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
