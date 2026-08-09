"""Correlation-id middleware. First in MIDDLEWARE so everything downstream --
including error handling -- has an id to report."""

from __future__ import annotations

import uuid

from .context import set_actor, set_correlation_id

CORRELATION_HEADER = "HTTP_X_CORRELATION_ID"
REQUEST_ID_HEADER = "HTTP_X_REQUEST_ID"


class CorrelationIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Accept an inbound id so a chain of service calls shares one; mint one
        # at the edge if the client didn't supply it.
        correlation_id = request.META.get(CORRELATION_HEADER) or str(uuid.uuid4())
        request_id = request.META.get(REQUEST_ID_HEADER) or str(uuid.uuid4())

        set_correlation_id(correlation_id)
        set_actor(None)
        request.correlation_id = correlation_id
        request.request_id = request_id

        try:
            response = self.get_response(request)
        finally:
            # Never leak this request's identity into a reused worker thread.
            set_correlation_id(None)
            set_actor(None)

        response["X-Correlation-Id"] = correlation_id
        response["X-Request-Id"] = request_id
        return response
