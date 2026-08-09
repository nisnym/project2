"""Test settings for identity-svc."""

from config.settings import *  # noqa: F401,F403

from platform_common.service_settings import apply_test_database

apply_test_database(DATABASES, "identity")  # noqa: F821

Q_CLUSTER = {**Q_CLUSTER, "sync": True}  # noqa: F821

# Django's default PBKDF2 does ~1M iterations, which is correct in production
# and ~0.4s per hash in a test suite that registers and authenticates
# constantly. MD5 here is a *test-only* speed choice; production hashing is
# untouched and asserted by test_password_is_hashed_not_stored.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
