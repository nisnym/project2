"""Test settings for ledger-svc."""

from config.settings import *  # noqa: F401,F403

from platform_common.service_settings import apply_test_database

apply_test_database(DATABASES, "ledger")  # noqa: F821

# Tasks run inline; no worker process needed.
Q_CLUSTER = {**Q_CLUSTER, "sync": True}  # noqa: F821
