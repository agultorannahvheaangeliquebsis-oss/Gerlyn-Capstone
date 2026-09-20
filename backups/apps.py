from django.apps import AppConfig


class BackupsConfig(AppConfig):
    name = 'backups'

    def ready(self):
        from django.conf import settings

        from . import scheduler

        # Only inside the web server's worker processes — never for manage.py
        # commands (including the run_backup process this schedules), tests or
        # migrations.
        if settings.BACKUP_SCHEDULER and scheduler.running_under_gunicorn():
            scheduler.start()
