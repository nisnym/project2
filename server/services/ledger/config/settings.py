"""Settings for ledger-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("ledger", port=8006))

INSTALLED_APPS = INSTALLED_APPS + ["ledger"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["ledger.handlers"]

# Q2 schedules owned by this service; registered by
# `manage.py register_schedules`. (name, dotted-path, minutes)
SERVICE_SCHEDULES = [
    ("expire_holds", "ledger.tasks.expire_holds", 1),
    ("verify_invariants", "ledger.tasks.verify_invariants", 60),
]
EXTRA_METRIC_PROVIDERS = ["ledger.tasks.ledger_metrics"]
