from django.apps import AppConfig
import os


class BotappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'botapp'

    def ready(self):
        # Django/ASGI processes do not pass through ``runbot``.  Initialise the
        # same fail-open telemetry layer here so the private ops API has a
        # consistent context without making telemetry a startup dependency.
        if os.getenv("NOYA_OBSERVABILITY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
            try:
                from botapp.observability.logging import configure
                from botapp.observability.tracing import init_telemetry
                configure()
                init_telemetry()
            except Exception:
                pass
