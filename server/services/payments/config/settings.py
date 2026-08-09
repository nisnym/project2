"""Settings for payments-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("payments", port=8005))

INSTALLED_APPS = INSTALLED_APPS + ["payments"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["payments.handlers"]

EXTRA_METRIC_PROVIDERS = ["payments.tasks.payments_metrics"]

# Q2 schedules owned by this service; registered by
# `manage.py register_schedules`. (name, dotted-path, minutes)
SERVICE_SCHEDULES = [
    ("run_due_schedules", "payments.tasks.run_due_schedules", 1),
    ("retry_compensation", "payments.tasks.retry_compensation", 5),
    ("sweep_stuck_sagas", "payments.tasks.sweep_stuck_sagas", 10),
    ("expire_stale_reviews", "payments.tasks.expire_stale_reviews", 60),
]
