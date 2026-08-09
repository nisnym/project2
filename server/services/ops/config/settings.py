"""Settings for ops-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("ops", port=8010))

INSTALLED_APPS = INSTALLED_APPS + ["ops"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["ops.handlers"]

# Q2 schedules owned by this service; registered by
# `manage.py register_schedules`. (name, dotted-path, minutes)
SERVICE_SCHEDULES = [
    ("poll_health", "ops.tasks.poll_health", 1),
    ("prune_snapshots", "ops.tasks.prune_snapshots", 1440),
]
