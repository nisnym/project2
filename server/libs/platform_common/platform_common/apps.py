from django.apps import AppConfig


class PlatformCommonConfig(AppConfig):
    name = "platform_common"
    label = "platform_common"
    verbose_name = "Platform Common"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Import each service's handlers so @subscribe registrations exist in
        # every process (web AND worker). Services declare the module path in
        # settings.EVENT_HANDLER_MODULES.
        import importlib

        from django.conf import settings

        for module_path in getattr(settings, "EVENT_HANDLER_MODULES", []):
            importlib.import_module(module_path)
