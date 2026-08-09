"""Settings for notification-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("notification", port=8008))

INSTALLED_APPS = INSTALLED_APPS + ["notification"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["notification.handlers"]
EXTRA_METRIC_PROVIDERS = ["notification.tasks.notification_metrics"]

# Q2 schedules owned by this service; registered by
# `manage.py register_schedules`. (name, dotted-path, minutes)
SERVICE_SCHEDULES = [
    ("retry_failed_deliveries", "notification.tasks.retry_failed_deliveries", 5),
]
