"""Settings for onboarding-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("onboarding", port=8002))

INSTALLED_APPS = INSTALLED_APPS + ["onboarding"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["onboarding.handlers"]

SERVICE_SCHEDULES = [
    ("sweep_stuck_applications", "onboarding.tasks.sweep_stuck_applications", 15),
]
