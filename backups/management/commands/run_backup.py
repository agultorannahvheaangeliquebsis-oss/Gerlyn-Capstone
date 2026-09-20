"""Back up the database and uploaded files, encrypted, to the configured storage.

    python manage.py run_backup

Normally started by the daily cron service via the trigger endpoint; also runnable
by hand (or from the Backup Status page). Exits non-zero if any part failed.
"""
from django.core.management.base import BaseCommand, CommandError

from backups.models import BackupRun
from backups.service import BackupInProgress, run_backup


class Command(BaseCommand):
    help = 'Run one full encrypted backup (database, uploaded files, erasure list).'

    def add_arguments(self, parser):
        parser.add_argument('--trigger', default='manual', help='Who started it: cron / manual.')

    def handle(self, *args, **options):
        try:
            runs = run_backup(trigger=options['trigger'][:10])
        except BackupInProgress as exc:
            raise CommandError(str(exc))

        failed = 0
        for run in runs:
            if run.status == BackupRun.Status.SUCCESS:
                size = f'{run.size_bytes:,} bytes' if run.size_bytes is not None else ''
                self.stdout.write(self.style.SUCCESS(f'OK      {run.get_kind_display()} {size}'))
            else:
                failed += 1
                self.stdout.write(self.style.ERROR(f'FAILED  {run.get_kind_display()}: {run.error}'))
        if failed:
            raise CommandError(f'{failed} backup part(s) failed.')
