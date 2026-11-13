from django.apps import AppConfig


class GlConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gl"
    verbose_name = "GL bridge"

    def ready(self) -> None:
        from gl import handlers  # noqa: F401  (registers Campfire event handlers)
