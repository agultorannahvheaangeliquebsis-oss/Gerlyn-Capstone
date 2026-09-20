"""Prove a backup can actually be restored — the restore test.

    python manage.py verify_backup

Downloads the newest backup (or --stamp), checks it against its manifest,
decrypts it, restores the database into a throwaway scratch database, compares
row counts, and checks the uploaded-files archive byte for byte. Never touches
the live database: the scratch database has its own name and is dropped
afterwards (unless --keep).

Run it on a laptop with the READ-ONLY Backblaze key in .env (B2_KEY_ID /
B2_APPLICATION_KEY), the same BACKUP_ENCRYPTION_KEY, and the MySQL command-line
tools installed. See docs/backup-and-restore.md.
"""
import gzip
import json
import shutil
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backups import dumper, media
from backups.crypto import BackupCryptoError, decrypt_file, load_key
from backups.service import _sha256
from backups.storage import BackupStorageError, get_storage

# Tables whose row counts legitimately move while a backup is being taken.
VOLATILE_TABLES = {
    'django_session', 'gerlyn_cache', 'accounts_usersession',
    'accounts_notification', 'audit_auditlog', 'backups_backuprun',
}


class Command(BaseCommand):
    help = 'Restore the newest backup into a scratch database and verify it.'

    def add_arguments(self, parser):
        parser.add_argument('--folder', default='daily', choices=['daily', 'weekly', 'monthly'])
        parser.add_argument('--stamp', help='A specific backup, e.g. 20260922-190000Z. Default: newest.')
        parser.add_argument('--scratch-db', help='Scratch database name (default: <live name>_restore_check).')
        parser.add_argument('--replace', action='store_true', help='Drop the scratch database first if it exists.')
        parser.add_argument('--keep', action='store_true', help='Keep the scratch database afterwards.')
        parser.add_argument('--media-dir', help='Also extract the uploaded files into this folder.')
        parser.add_argument('--ledger-out', help='Write the erased-accounts list to this file (for reapply_erasures).')

    def handle(self, *args, **options):
        started = time.monotonic()
        live = settings.DATABASES['default']['NAME']
        scratch = options['scratch_db'] or f'{live}_restore_check'
        if scratch == live:
            raise CommandError('The scratch database must not be the live database.')

        try:
            master_key = load_key(settings.BACKUP_ENCRYPTION_KEY)
            storage = get_storage()
            manifest_key = self._pick_manifest(storage, options['folder'], options['stamp'])
        except (BackupCryptoError, BackupStorageError) as exc:
            raise CommandError(str(exc))

        problems = []
        with tempfile.TemporaryDirectory(prefix='gerlyn-restore-') as tmp:
            work = Path(tmp)
            manifest = self._json(storage, master_key, manifest_key, None, work)
            self.stdout.write(f'Backup: {manifest_key}  (created {manifest["created_at"]})')
            for failed in manifest.get('failed_parts', []):
                problems.append(f'The backup itself recorded a failed part: {failed}.')

            parts = manifest['parts']
            if 'database' in parts:
                problems += self._check_database(storage, master_key, parts['database'], options, work, scratch)
            else:
                problems.append('This backup has no database part.')

            if 'media' in parts:
                problems += self._check_media(storage, master_key, parts['media'], options, work)
            else:
                problems.append('This backup has no uploaded-files part.')

            if 'erasures' in parts:
                ledger = self._json(storage, master_key, self._key_in(parts['erasures'], options['folder']),
                                    parts['erasures']['sha256'], work)
                self.stdout.write(
                    f'Erasure list: {len(ledger["erased_accounts"])} erased account(s) '
                    f'(re-apply after a real restore with: manage.py reapply_erasures)'
                )
                if options['ledger_out']:
                    Path(options['ledger_out']).write_text(json.dumps(ledger), encoding='utf-8')
                    self.stdout.write(f'Erasure list written to {options["ledger_out"]}')

        elapsed = time.monotonic() - started
        if problems:
            self.stdout.write(self.style.ERROR('\nRESTORE CHECK FAILED'))
            for problem in problems:
                self.stdout.write(self.style.ERROR(f'  - {problem}'))
            raise CommandError(f'Restore check failed after {elapsed:.0f}s.')
        self.stdout.write(self.style.SUCCESS(f'\nRESTORE CHECK PASSED in {elapsed:.0f}s'))

    # --- steps -------------------------------------------------------------------

    def _pick_manifest(self, storage, folder, stamp):
        wanted = f'{folder}/manifest-{stamp}.json.enc' if stamp else None
        keys = sorted(k for k, _, _ in storage.list(f'{folder}/manifest-') if k.endswith('.json.enc'))
        if not keys:
            raise CommandError(f'No backups found under {folder}/.')
        if wanted:
            if wanted not in keys:
                raise CommandError(f'No such backup: {wanted}')
            return wanted
        return keys[-1]

    @staticmethod
    def _key_in(part, folder):
        for key in part['keys']:
            if key.startswith(f'{folder}/'):
                return key
        raise CommandError(f'The manifest lists no {folder}/ copy for this part.')

    def _download(self, storage, master_key, key, expected_sha, work):
        """Fetch one encrypted file, check its checksum, return the decrypted path."""
        sealed = work / (Path(key).name)
        storage.get(key, sealed)
        if expected_sha and _sha256(sealed) != expected_sha:
            raise CommandError(f'{key}: checksum does not match the manifest (corrupted download or file).')
        plain = work / (Path(key).name + '.plain')
        try:
            decrypt_file(sealed, plain, master_key)
        except BackupCryptoError as exc:
            raise CommandError(f'{key}: {exc}')
        sealed.unlink()
        return plain

    def _json(self, storage, master_key, key, expected_sha, work):
        plain = self._download(storage, master_key, key, expected_sha, work)
        data = json.loads(plain.read_text(encoding='utf-8'))
        plain.unlink()
        return data

    def _check_database(self, storage, master_key, part, options, work, scratch):
        key = self._key_in(part, options['folder'])
        self.stdout.write(f'Downloading and decrypting {key} ...')
        plain = self._download(storage, master_key, key, part['sha256'], work)
        sql = work / 'restore.sql'
        with gzip.open(plain, 'rb') as source, open(sql, 'wb') as target:
            shutil.copyfileobj(source, target)
        plain.unlink()

        if dumper.database_exists(scratch):
            if not options['replace']:
                raise CommandError(f'Scratch database "{scratch}" already exists. Use --replace to drop it.')
            dumper.drop_database(scratch)
        dumper.create_database(scratch)
        problems = []
        try:
            self.stdout.write(f'Restoring into scratch database "{scratch}" ...')
            dumper.restore_sql(sql, scratch)
            restored = dumper.counts_for_database(scratch)
            problems += self._compare_counts(part['tables'], restored)
        except dumper.BackupToolError as exc:
            problems.append(str(exc))
        finally:
            if options['keep']:
                self.stdout.write(f'Scratch database "{scratch}" kept.')
            else:
                dumper.drop_database(scratch)
        return problems

    def _compare_counts(self, expected, restored):
        problems, moved = [], []
        for table, count in sorted(expected.items()):
            if table not in restored:
                problems.append(f'Table {table} is missing from the restored database.')
            elif restored[table] != count:
                if table in VOLATILE_TABLES:
                    moved.append(f'{table} {count} -> {restored[table]}')
                else:
                    problems.append(f'Table {table}: {count} rows at backup time, {restored[table]} restored.')
        self.stdout.write(f'Database: {len(expected)} tables checked, {len(problems)} problem(s).')
        if moved:
            self.stdout.write('  (busy tables that legitimately moved: ' + '; '.join(moved) + ')')
        return problems

    def _check_media(self, storage, master_key, part, options, work):
        key = self._key_in(part, options['folder'])
        self.stdout.write(f'Downloading and decrypting {key} ...')
        plain = self._download(storage, master_key, key, part['sha256'], work)
        actual = media.fingerprint_archive(plain)
        expected = part['media']
        problems = []
        if actual != expected:
            problems.append(f'Uploaded files differ from the manifest: expected {expected}, got {actual}.')
        self.stdout.write(f'Uploaded files: {actual["files"]} file(s), {actual["bytes"]:,} bytes, '
                          f'{"identical to the manifest" if actual == expected else "MISMATCH"}.')
        if options['media_dir']:
            media.extract_archive(plain, options['media_dir'])
            self.stdout.write(f'Extracted to {options["media_dir"]}')
        plain.unlink()
        return problems
