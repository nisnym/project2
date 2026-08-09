"""Test settings for ops-svc."""

from config.settings import *  # noqa: F401,F403

from platform_common.service_settings import apply_test_database

apply_test_database(DATABASES, "ops")  # noqa: F821

Q_CLUSTER = {**Q_CLUSTER, "sync": True}  # noqa: F821
