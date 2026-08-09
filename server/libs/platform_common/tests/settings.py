"""Settings for platform_common's own tests.

A real settings module rather than settings.configure() in conftest, because
pytest-django needs to drive Django's setup itself -- configuring manually
leaves its test-case machinery uninitialised.

The library's tests need *a* configured service, not any particular one.
`payments` is used deliberately: publish() filters the publishing service out of
its own subscriber list, so running the tests *as* audit would hide the wildcard
subscription entirely and make those assertions vacuous.
"""

from platform_common.service_settings import apply_test_database, base_settings

globals().update(base_settings("payments", port=8005))

apply_test_database(DATABASES, "platform_common")  # noqa: F821

# Tasks run inline: no worker process needed to test the backbone.
Q_CLUSTER = {**Q_CLUSTER, "sync": True}  # noqa: F821

ROOT_URLCONF = "tests.urls"
EVENT_HANDLER_MODULES = []
