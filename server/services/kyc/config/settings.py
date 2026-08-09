"""Settings for kyc-svc.

Everything shared lives in platform_common.service_settings; only what is
genuinely specific to this service belongs here.
"""

from platform_common.service_settings import base_settings

globals().update(base_settings("kyc", port=8003))

INSTALLED_APPS = INSTALLED_APPS + ["kyc"]  # noqa: F821

# Imported by PlatformCommonConfig.ready() so @subscribe registrations exist in
# the web process AND in every Q2 worker process.
EVENT_HANDLER_MODULES = ["kyc.handlers"]

SERVICE_SCHEDULES = [
    ("sweep_stuck_cases", "kyc.tasks.sweep_stuck_cases", 15),
]
