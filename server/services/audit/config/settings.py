"""Settings for audit-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("audit", port=8009))

INSTALLED_APPS = INSTALLED_APPS + ["audit"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["audit.handlers"]

EXTRA_METRIC_PROVIDERS = ["audit.tasks.chain_stats"]

# Q2 schedules owned by this service; registered by
# `manage.py register_schedules`. (name, dotted-path, minutes)
SERVICE_SCHEDULES = [
    ("verify_chain", "audit.tasks.verify_chain_task", 1440),
]
