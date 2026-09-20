from django.apps import AppConfig


class AuditAppConfig(AppConfig):
    name = 'audit'

    def ready(self):
        from . import capture

        capture.connect()
