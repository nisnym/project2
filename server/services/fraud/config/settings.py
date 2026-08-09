"""Settings for fraud-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("fraud", port=8007))

INSTALLED_APPS = INSTALLED_APPS + ["fraud"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["fraud.handlers"]

EXTRA_METRIC_PROVIDERS = ["fraud.tasks.fraud_metrics"]

# Q2 schedules owned by this service; registered by
# `manage.py register_schedules`. (name, dotted-path, minutes)
SERVICE_SCHEDULES = [
    ("refresh_rule_cache", "fraud.tasks.refresh_rule_cache", 1),
    ("escalate_sla_breaches", "fraud.tasks.escalate_sla_breaches", 5),
    ("prune_screened_txns", "fraud.tasks.prune_screened_txns", 1440),
]
