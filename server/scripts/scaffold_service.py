#!/usr/bin/env python
"""Create the standard skeleton for a service.

Every service has the same shape (LLD s1.1). Generating it means the shape is
identical by construction rather than by discipline, and a reviewer can find
anything in any service without having to look around first.

    uv run python scripts/scaffold_service.py payments
    uv run python scripts/scaffold_service.py --all

Existing files are never overwritten, so this is safe to re-run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs" / "platform_common"))

from platform_common.service_settings import SERVICE_PORTS  # noqa: E402

SERVICES = ROOT / "services"


def manage_py(service: str, app: str) -> str:
    return f'''#!/usr/bin/env python
"""Django entrypoint for {service}-svc."""
import os
import sys
from pathlib import Path

# The service directory itself must be importable so `config` and `{app}` resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
'''


def settings_py(service: str, app: str, port: int) -> str:
    return f'''"""Settings for {service}-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("{service}", port={port}))

INSTALLED_APPS = INSTALLED_APPS + ["{app}"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["{app}.handlers"]
'''


def urls_py(app: str) -> str:
    return f'''from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from platform_common.urls import platform_urlpatterns

urlpatterns = [
    *platform_urlpatterns(),
    path("api/schema", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("", include("{app}.urls")),
]
'''


WSGI_PY = '''import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
'''


def apps_py(app: str, klass: str) -> str:
    return f'''from django.apps import AppConfig


class {klass}Config(AppConfig):
    name = "{app}"
    default_auto_field = "django.db.models.BigAutoField"
'''


def stubs(service: str) -> dict[str, str]:
    return {
        "models.py": (
            f'"""Domain models owned by {service}-svc.\n\n'
            'Never imported by another service -- there is no import path across a\n'
            'database boundary, which is the point.\n"""\n\n'
            "from django.db import models  # noqa: F401\n"
        ),
        "serializers.py": (
            '"""Request validation and response representation.\n\n'
            'ALL input validation lives here, never in views.\n"""\n\n'
            "from rest_framework import serializers  # noqa: F401\n"
        ),
        "views.py": (
            '"""HTTP layer: authorise, parse, delegate, respond.\n\n'
            "No business logic -- if there's an `if` about the domain, it belongs in\n"
            'services.py.\n"""\n\n'
            "from rest_framework.views import APIView  # noqa: F401\n"
        ),
        "services.py": (
            '"""Domain logic. The only module that opens transaction.atomic()."""\n'
        ),
        "tasks.py": (
            '"""Django Q2 task entrypoints.\n\n'
            "Thin, and idempotent -- assume every task runs twice, because a worker\n"
            'can die after the side effect but before the ack.\n"""\n'
        ),
        "handlers.py": (
            '"""Inbound event handlers. Idempotent and order-tolerant."""\n\n'
            "from platform_common.events import subscribe  # noqa: F401\n"
        ),
        "clients.py": (
            '"""Outbound calls to other services.\n\n'
            'The only module that knows another service\'s URL.\n"""\n\n'
            "from platform_common.http import get_client  # noqa: F401\n"
        ),
        "urls.py": "from django.urls import path  # noqa: F401\n\nurlpatterns = []\n",
    }


def scaffold(service: str) -> None:
    if service not in SERVICE_PORTS:
        raise SystemExit(f"unknown service {service!r}; expected one of {list(SERVICE_PORTS)}")

    app = service
    klass = "".join(part.capitalize() for part in service.split("_"))
    port = SERVICE_PORTS[service]
    base = SERVICES / service

    (base / "config").mkdir(parents=True, exist_ok=True)
    (base / app / "migrations").mkdir(parents=True, exist_ok=True)
    (base / "tests").mkdir(parents=True, exist_ok=True)

    files: dict[Path, str] = {
        base / "manage.py": manage_py(service, app),
        base / "config" / "__init__.py": "",
        base / "config" / "settings.py": settings_py(service, app, port),
        base / "config" / "urls.py": urls_py(app),
        base / "config" / "wsgi.py": WSGI_PY,
        base / app / "__init__.py": "",
        base / app / "apps.py": apps_py(app, klass),
        base / app / "migrations" / "__init__.py": "",
        base / "tests" / "__init__.py": "",
    }
    for name, content in stubs(service).items():
        files[base / app / name] = content

    created = 0
    for path, content in files.items():
        if path.exists():
            continue  # never clobber real code
        path.write_text(content)
        created += 1

    (base / "manage.py").chmod(0o755)
    status = f"{created} files created" if created else "already present"
    print(f"  {service:14s} port {port}  ({status})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", nargs="?", help="service name")
    parser.add_argument("--all", action="store_true", help="scaffold every service")
    args = parser.parse_args()

    if args.all:
        targets = list(SERVICE_PORTS)
    elif args.service:
        targets = [args.service]
    else:
        parser.error("give a service name or --all")

    print("scaffolding services:")
    for service in targets:
        scaffold(service)


if __name__ == "__main__":
    main()
