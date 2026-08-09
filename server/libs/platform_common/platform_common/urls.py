"""Routes every service mounts identically.

In a service's ``config/urls.py``::

    urlpatterns = [
        *platform_urlpatterns(),
        path("api/...", include("payments.urls")),
    ]

``/internal/*`` is never routed by the gateway -- it is reachable only from
inside the network.
"""

from django.urls import path

from .events.views import ingest
from .observability.health import healthz, metrics, readyz


def platform_urlpatterns():
    return [
        path("healthz", healthz, name="healthz"),
        path("readyz", readyz, name="readyz"),
        path("internal/events", ingest, name="internal-events"),
        path("internal/metrics", metrics, name="internal-metrics"),
    ]
