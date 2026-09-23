"""Re-erase accounts whose personal data was erased after a backup was taken.

Run this on a database restored from a backup (see docs/backup-and-restore.md):

    python manage.py reapply_erasures            # newest list from storage
    python manage.py reapply_erasures --ledger erasures.json --dry-run
"""
import json
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backups.crypto import decrypt_file, load_key
from backups.erasures import reapply
from backups.storage import get_storage


class Command(BaseCommand):
    help = 'Apply the erased-accounts list to a restored database.'

    def add_arguments(self, parser):
        parser.add_argument('--ledger', help='A decrypted erasures JSON file. Default: newest from storage.')
        parser.add_argument('--folder', default='daily', help='Storage folder to take the newest list from.')
        parser.add_argument('--dry-run', action='store_true', help='Report only; change nothing.')

    def handle(self, *args, **options):
        if options['ledger']:
            ledger = json.loads(Path(options['ledger']).read_text(encoding='utf-8'))
        else:
            ledger = self._newest_from_storage(options['folder'])

        result = reapply(ledger, dry_run=options['dry_run'])
        verb = 'Would erase' if options['dry_run'] else 'Erased'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {len(result["reapplied"])} account(s); '
            f'{result["already_erased"]} already erased; '
            f'{result["not_present"]} not in this database.'
        ))

    def _newest_from_storage(self, folder):
        storage = get_storage()
        keys = sorted(k for k, _, _ in storage.list(f'{folder}/erasures-') if k.endswith('.json.enc'))
        if not keys:
            raise CommandError(f'No erasures list found under {folder}/.')
        master_key = load_key(settings.BACKUP_ENCRYPTION_KEY)
        with tempfile.TemporaryDirectory() as tmp:
            sealed, plain = Path(tmp) / 'ledger.enc', Path(tmp) / 'ledger.json'
            storage.get(keys[-1], sealed)
            decrypt_file(sealed, plain, master_key)
            self.stdout.write(f'Using {keys[-1]}')
            return json.loads(plain.read_text(encoding='utf-8'))
