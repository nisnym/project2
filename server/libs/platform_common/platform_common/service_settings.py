"""Base Django settings shared by all ten services.

A service's ``config/settings.py`` is expected to be roughly::

    from platform_common.service_settings import base_settings
    globals().update(base_settings("payments", port=8005))
    INSTALLED_APPS += ["payments"]

Everything here is env-overridable (12-factor). Defaults are tuned for local
development against a local PostgreSQL; nothing here assumes Docker.
"""

from __future__ import annotations

import os
from pathlib import Path

# Fixed local dev ports. In production these become service DNS names and only
# SERVICE_REGISTRY changes.
SERVICE_PORTS = {
    "identity": 8001,
    "onboarding": 8002,
    "kyc": 8003,
    "account": 8004,
    "payments": 8005,
    "ledger": 8006,
    "fraud": 8007,
    "notification": 8008,
    "audit": 8009,
    "ops": 8010,
}

ALL_SERVICES = tuple(SERVICE_PORTS)


def service_registry() -> dict[str, str]:
    """``{service_name: base_url}``.

    Each entry is overridable with e.g. ``LEDGER_URL=http://ledger:8000`` so the
    same image runs unchanged under compose, Kubernetes or bare processes.
    """
    registry = {}
    for name, port in SERVICE_PORTS.items():
        registry[name] = os.environ.get(
            f"{name.upper()}_URL", f"http://127.0.0.1:{port}"
        )
    return registry


# Which service receives which event. Single source of truth for the whole
# estate; each service reads the same table and publish() filters it to the
# subscribers that are not itself.
#
# "*" subscribes a service to every event -- audit only. Because subscribers_for()
# always appends the wildcard, an event needs no entry here to reach the audit
# log; an entry means some service *acts* on it.
#
# **Every entry must name a service that registers a handler for that event.**
# A route to a service with no handler is not harmless: it writes an outbox row,
# runs an HTTP relay, writes an inbox row on the peer and enqueues a dispatch
# task there -- which then finds nothing to run. Full cost, no effect, no error.
# scripts/verify_wiring.py boots every service, reads its live handler registry
# and fails on any route that does not land, so this table cannot quietly drift
# away from the code again.
EVENT_SUBSCRIPTIONS: dict[str, list[str]] = {
    "*": ["audit"],
    # identity
    "user.login_failed": ["notification"],
    "security.refresh_reuse_detected": ["notification", "ops"],
    # identity administration. Every one of these is a privileged change to a
    # person's access, so ops sees them live and audit chains them for the
    # regulator. The ones the affected user is entitled to hear about also go
    # to notification -- "someone reset your password" is exactly the signal
    # that lets a customer report a takeover before it is used.
    "user.status_changed": ["notification"],
    "user.unlocked": ["notification"],
    "user.sessions_revoked": ["notification"],
    "user.password_reset": ["notification"],
    "user.password_changed": ["notification"],
    # onboarding / kyc
    "kyc.completed": ["onboarding"],
    # account
    "account.opened": ["notification"],
    "account.frozen": ["payments"],
    "beneficiary.added": ["notification"],
    "beneficiary.blocked": ["notification"],
    # payments
    "payment.approved": ["notification"],
    "payment.blocked": ["notification", "ops"],
    "payment.review_required": ["notification"],
    "payment.settled": ["notification"],
    # The recipient's side of an internal transfer. Addressed to *them*, not to
    # the payer, so notification-svc tells the right person that money arrived.
    "payment.received": ["notification"],
    "payment.returned": ["notification", "ops"],
    "payment.failed": ["notification", "ops"],
    "payment.cancelled": ["notification"],
    "schedule.failed": ["notification", "ops"],
    # ledger. account-svc keeps its cached balance from this, which is what
    # lets a customer see their last known balance when ledger-svc is down
    # instead of a flat zero.
    "ledger.posted": ["account"],
    "ledger.invariant_breached": ["ops"],
    # fraud
    "fraud.case_approved": ["payments"],
    "fraud.case_rejected": ["payments"],
    # platform
    "outbox.dead": ["ops"],
    "audit.chain_broken": ["ops"],
}


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Where SQLite files live. One file per service is database-per-service taken
# literally -- there is not even a shared server process to reach across.
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[3] / ".data"))


def database_config(service: str) -> dict:
    """SQLite by default; PostgreSQL when DB_ENGINE=postgres.

    SQLite is the default because it ships with Python and needs no server --
    the whole estate runs on a laptop with nothing installed. The code is
    identical on both: money is stored via MoneyField (exact integers), so
    there is no backend-specific numeric behaviour to reason about.

    Three SQLite options are load-bearing rather than cosmetic:

      transaction_mode=IMMEDIATE  take the write lock when the transaction
          opens, not on first write. SQLite ignores SELECT ... FOR UPDATE, so
          this is what actually serialises two concurrent transfers reading the
          same balance -- without it the second gets SQLITE_BUSY on lock
          upgrade, or worse, both read a stale balance.
      journal_mode=WAL            readers do not block the writer, so the API
          stays responsive while a Q2 worker is committing.
      timeout=30                  wait for a busy lock instead of failing
          immediately under contention.
    """
    engine = os.environ.get("DB_ENGINE", "sqlite").lower()

    if engine in ("postgres", "postgresql"):
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", f"{service}_db"),
            "USER": os.environ.get("DB_USER", os.environ.get("USER", "postgres")),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("DB_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "60")),
            "ATOMIC_REQUESTS": False,
        }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    name = os.environ.get("DB_NAME") or str(DATA_DIR / f"{service}.sqlite3")
    # Allow bare names under DATA_DIR so DB_NAME=foo_test works like Postgres.
    if not name.endswith(".sqlite3") and not name.startswith(("/", ":")):
        name = str(DATA_DIR / f"{name}.sqlite3")

    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": name,
        "ATOMIC_REQUESTS": False,
        # Django's default SQLite test database is in-memory with a shared
        # cache, where table-level locks bypass busy_timeout entirely and
        # concurrent writers fail instantly with "database table is locked".
        # A file-based test database behaves like production: writers queue on
        # the busy timeout instead of erroring.
        "TEST": {"NAME": str(DATA_DIR / f"test_{Path(name).stem}.sqlite3")},
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": int(os.environ.get("DB_TIMEOUT", "30")),
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA foreign_keys=ON;"
                "PRAGMA busy_timeout=30000;"
            ),
        },
    }


def apply_test_database(databases: dict, label: str) -> dict:
    """Point a settings module's DATABASES at an isolated test database.

    Each suite needs its own, or two suites running against the same service
    name would clobber each other. Works on both backends: a distinct file under
    .data/ for SQLite, a distinct database name for PostgreSQL.
    """
    config = databases["default"]
    if "sqlite" in config["ENGINE"]:
        config["NAME"] = str(DATA_DIR / f"{label}.sqlite3")
        config.setdefault("TEST", {})["NAME"] = str(DATA_DIR / f"test_{label}.sqlite3")
    else:
        config["NAME"] = f"{label}_db"
        config.setdefault("TEST", {})["NAME"] = f"test_{label}"
    return databases


def base_settings(service: str, *, port: int | None = None) -> dict:
    if service not in SERVICE_PORTS:
        raise ValueError(f"unknown service {service!r}; expected one of {ALL_SERVICES}")

    port = port or SERVICE_PORTS[service]
    debug = _bool("DEBUG", True)

    q_workers = int(os.environ.get("Q_WORKERS", "4"))
    q_timeout = int(os.environ.get("Q_TIMEOUT", "60"))
    # django-q2 warns (correctly) if retry <= timeout: the task would be
    # redelivered while the first copy is still running. Enforce the margin here
    # so no service can misconfigure it.
    q_retry = max(int(os.environ.get("Q_RETRY", "120")), q_timeout * 2)

    return {
        "SERVICE_NAME": service,
        "SERVICE_PORT": port,
        "BASE_DIR": os.getcwd(),
        "DEBUG": debug,
        "SECRET_KEY": os.environ.get(
            "SECRET_KEY", "dev-only-insecure-key-change-in-production"
        ),
        "ALLOWED_HOSTS": os.environ.get("ALLOWED_HOSTS", "*").split(","),
        "INSTALLED_APPS": [
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.staticfiles",
            "django_q",
            "rest_framework",
            "drf_spectacular",
            "platform_common",
        ],
        "MIDDLEWARE": [
            "platform_common.observability.middleware.CorrelationIdMiddleware",
            # Before CommonMiddleware: a preflight must be answered even when
            # CommonMiddleware would redirect (APPEND_SLASH) or reject the URL.
            "platform_common.http.cors.CorsMiddleware",
            "django.middleware.common.CommonMiddleware",
        ],
        "ROOT_URLCONF": "config.urls",
        "TEMPLATES": [
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": []},
            }
        ],
        "WSGI_APPLICATION": "config.wsgi.application",
        "DATABASES": {"default": database_config(service)},
        "DEFAULT_AUTO_FIELD": "django.db.models.BigAutoField",
        "USE_TZ": True,
        "TIME_ZONE": "UTC",
        "LANGUAGE_CODE": "en-us",
        "STATIC_URL": "/static/",
        "AUTH_PASSWORD_VALIDATORS": [],
        # ---- platform wiring -------------------------------------------
        "SERVICE_REGISTRY": service_registry(),
        "EVENT_SUBSCRIPTIONS": EVENT_SUBSCRIPTIONS,
        "EVENT_HANDLER_MODULES": [],  # each service appends its own
        "INTERNAL_HMAC_KEY": os.environ.get(
            "INTERNAL_HMAC_KEY", "dev-only-internal-hmac-key"
        ),
        "JWT_ISSUER": os.environ.get("JWT_ISSUER", "identity-svc"),
        "JWT_AUDIENCE": os.environ.get("JWT_AUDIENCE", "banking-platform"),
        "JWKS_CACHE_SECONDS": int(os.environ.get("JWKS_CACHE_SECONDS", "300")),
        # ---- Django Q2 --------------------------------------------------
        # Values verified against django-q2 1.10.0 in the P0 spike:
        #   retry MUST exceed timeout, and max_attempts MUST be set explicitly
        #   because its default of 0 means *infinite* retries -- a poison task
        #   would otherwise occupy the queue forever.
        "Q_CLUSTER": {
            "name": service,
            "orm": "default",
            "workers": q_workers,
            "recycle": int(os.environ.get("Q_RECYCLE", "500")),
            "timeout": q_timeout,
            "retry": q_retry,
            "max_attempts": int(os.environ.get("Q_MAX_ATTEMPTS", "5")),
            "queue_limit": int(os.environ.get("Q_QUEUE_LIMIT", "200")),
            "bulk": int(os.environ.get("Q_BULK", "10")),
            "poll": float(os.environ.get("Q_POLL", "0.2")),
            "save_limit": int(os.environ.get("Q_SAVE_LIMIT", "1000")),
            "ack_failures": True,
            # After downtime, do not fire every missed schedule at once. Our
            # sweepers query for whatever piled up anyway.
            "catch_up": False,
            "sync": _bool("Q_SYNC", False),
            "label": f"Tasks ({service})",
        },
        "REST_FRAMEWORK": {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "platform_common.auth.authentication.JWTAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
            "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
            "EXCEPTION_HANDLER": "platform_common.errors.exception_handler",
            "UNAUTHENTICATED_USER": None,
            "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
            + (["rest_framework.renderers.BrowsableAPIRenderer"] if debug else []),
        },
        "SPECTACULAR_SETTINGS": {
            "TITLE": f"{service}-svc API",
            "VERSION": "1.0.0",
            "SERVE_INCLUDE_SCHEMA": False,
            "COMPONENT_SPLIT_REQUEST": True,
        },
        "LOGGING": {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": "platform_common.observability.logging.JsonFormatter",
                    "service": service,
                },
                "plain": {
                    "format": "%(asctime)s %(levelname)-7s [" + service + "] %(name)s: %(message)s",
                    "datefmt": "%H:%M:%S",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "plain" if debug else "json",
                }
            },
            "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
            "loggers": {
                "django.db.backends": {"level": "WARNING"},
                "django.utils.autoreload": {"level": "WARNING"},
                "httpx": {"level": "WARNING"},
                "django_q": {"level": os.environ.get("Q_LOG_LEVEL", "WARNING")},
            },
        },
    }
