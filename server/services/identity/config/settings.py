"""Settings for identity-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("identity", port=8001))

INSTALLED_APPS = INSTALLED_APPS + ["identity"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["identity.handlers"]

# Q2 schedules owned by this service; registered by
# `manage.py register_schedules`. (name, dotted-path, minutes)
SERVICE_SCHEDULES = [
    ("purge_expired_tokens", "identity.tasks.purge_expired_tokens", 1440),
    ("unlock_expired_lockouts", "identity.tasks.unlock_expired_lockouts", 5),
]
