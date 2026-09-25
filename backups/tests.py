import gzip
import io
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Notification, User
from accounts.services import hard_delete_personal_data
from backups import crypto, dumper, media, scheduler, service, trigger_cli
from backups.crypto import BackupCryptoError, decrypt_file, encrypt_file, generate_key, load_key
from backups.erasures import reapply
from backups.models import BackupRun, RestoreTestRecord
from backups.storage import B2Storage, BackupStorageError, LocalStorage, get_storage

KEY = generate_key()
TOKEN = 'test-trigger-token-0123456789'


def _utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


class TempDirMixin:
    def make_dir(self):
        path = Path(tempfile.mkdtemp(prefix='gerlyn-test-'))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path


# --- encryption ------------------------------------------------------------------

class CryptoTests(TempDirMixin, SimpleTestCase):
    def setUp(self):
        self.dir = self.make_dir()
        self.key = load_key(KEY)

    def _roundtrip(self, data):
        src, sealed, out = self.dir / 'in', self.dir / 'sealed', self.dir / 'out'
        src.write_bytes(data)
        encrypt_file(src, sealed, self.key)
        decrypt_file(sealed, out, self.key)
        return sealed, out.read_bytes()

    def test_roundtrip_for_empty_small_and_multi_chunk_files(self):
        with mock.patch('backups.crypto.CHUNK_SIZE', 64):
            for data in (b'', b'x', b'a' * 64, b'b' * 65, b'c' * 1000):
                with self.subTest(size=len(data)):
                    self.assertEqual(self._roundtrip(data)[1], data)

    def test_ciphertext_hides_the_plaintext_and_differs_every_time(self):
        secret = b'customer payment proof for juan'
        first = self._roundtrip(secret)[0].read_bytes()
        second = self._roundtrip(secret)[0].read_bytes()  # same path, so copy the first out before reuse
        self.assertNotIn(secret, first)
        self.assertNotEqual(first, second)

    def test_the_wrong_key_is_refused(self):
        sealed, _ = self._roundtrip(b'data')
        with self.assertRaises(BackupCryptoError):
            decrypt_file(sealed, self.dir / 'x', load_key(generate_key()))

    def test_a_flipped_byte_is_detected(self):
        sealed, _ = self._roundtrip(b'important data ' * 10)
        blob = bytearray(sealed.read_bytes())
        blob[-5] ^= 0xFF
        sealed.write_bytes(bytes(blob))
        with self.assertRaises(BackupCryptoError):
            decrypt_file(sealed, self.dir / 'x', self.key)

    def test_a_truncated_file_is_detected_not_accepted_as_smaller(self):
        with mock.patch('backups.crypto.CHUNK_SIZE', 64):
            sealed, _ = self._roundtrip(b'z' * 500)
            blob = sealed.read_bytes()
            sealed.write_bytes(blob[:len(blob) // 2])  # cut off mid-file
            with self.assertRaises(BackupCryptoError):
                decrypt_file(sealed, self.dir / 'x', self.key)

    def test_a_missing_final_chunk_is_detected(self):
        import struct

        with mock.patch('backups.crypto.CHUNK_SIZE', 64):
            sealed, _ = self._roundtrip(b'z' * 200)
            blob = sealed.read_bytes()
            pos, cuts = crypto.HEADER_LEN, []
            while pos < len(blob):
                (n,) = struct.unpack('>I', blob[pos:pos + 4])
                cuts.append(pos)
                pos += 4 + n
            sealed.write_bytes(blob[:cuts[-1]])  # exactly whole chunks, last one gone
            with self.assertRaises(BackupCryptoError):
                decrypt_file(sealed, self.dir / 'x', self.key)

    def test_data_appended_after_the_end_is_detected(self):
        sealed, _ = self._roundtrip(b'data')
        sealed.write_bytes(sealed.read_bytes() + b'\x00')
        with self.assertRaises(BackupCryptoError):
            decrypt_file(sealed, self.dir / 'x', self.key)

    def test_a_non_backup_file_is_refused(self):
        junk = self.dir / 'junk'
        junk.write_bytes(b'this is not a backup at all')
        with self.assertRaises(BackupCryptoError):
            decrypt_file(junk, self.dir / 'x', self.key)

    def test_key_validation(self):
        self.assertEqual(len(load_key(generate_key())), 32)
        for bad in ('', '   ', 'not base64 !!', 'c2hvcnQ='):
            with self.subTest(value=bad), self.assertRaises(BackupCryptoError):
                load_key(bad)


# --- storage ----------------------------------------------------------------------

class StorageTests(TempDirMixin, SimpleTestCase):
    def test_local_storage_put_get_list(self):
        root, src = self.make_dir(), self.make_dir() / 'f'
        src.write_bytes(b'hello')
        storage = LocalStorage(root)
        storage.put('daily/a.enc', src)
        storage.put('weekly/b.enc', src)

        self.assertEqual([k for k, _, _ in storage.list('daily/')], ['daily/a.enc'])
        self.assertEqual(len(storage.list('')), 2)
        out = root / 'out'
        storage.get('daily/a.enc', out)
        self.assertEqual(out.read_bytes(), b'hello')

    def test_local_storage_refuses_keys_that_escape_its_folder(self):
        storage = LocalStorage(self.make_dir())
        with self.assertRaises(BackupStorageError):
            storage.put('../evil', Path(__file__))

    def test_get_storage_follows_settings(self):
        with override_settings(BACKUP_STORAGE='local', BACKUP_LOCAL_DIR=self.make_dir()):
            self.assertIsInstance(get_storage(), LocalStorage)
        with override_settings(BACKUP_STORAGE='b2', B2_BUCKET='', B2_ENDPOINT='', B2_KEY_ID='', B2_APPLICATION_KEY=''):
            with self.assertRaisesMessage(BackupStorageError, 'not configured'):
                get_storage()
        with override_settings(BACKUP_STORAGE='ftp'):
            with self.assertRaises(BackupStorageError):
                get_storage()

    def test_a_bad_backblaze_setting_becomes_a_clean_storage_error(self):
        with override_settings(BACKUP_STORAGE='b2', B2_BUCKET='b', B2_ENDPOINT='https://', B2_KEY_ID='i',
                               B2_APPLICATION_KEY='k'),                 mock.patch('backups.storage.B2Storage', side_effect=ValueError('Invalid endpoint')):
            with self.assertRaisesMessage(BackupStorageError, 'Could not set up Backblaze: Invalid endpoint'):
                get_storage()

    def test_an_endpoint_pasted_without_https_still_works(self):
        storage = B2Storage('s3.eu-central-003.backblazeb2.com', 'id', 'key', 'bucket')
        self.assertEqual(storage.client.meta.endpoint_url, 'https://s3.eu-central-003.backblazeb2.com')
        self.assertEqual(storage.client.meta.region_name, 'eu-central-003')

    def test_b2_client_is_built_from_the_endpoint_without_touching_the_network(self):
        storage = B2Storage('https://s3.us-west-004.backblazeb2.com', 'id', 'key', 'bucket')
        self.assertEqual(storage.client.meta.region_name, 'us-west-004')
        self.assertEqual(storage.client.meta.endpoint_url, 'https://s3.us-west-004.backblazeb2.com')
        self.assertEqual(storage.bucket, 'bucket')


# --- media ------------------------------------------------------------------------

class MediaTests(TempDirMixin, SimpleTestCase):
    def _tree(self):
        root = self.make_dir()
        (root / 'payment_proofs').mkdir()
        (root / 'payment_proofs' / 'a.png').write_bytes(b'AAA')
        (root / 'b.txt').write_bytes(b'BBBB')
        return root

    def test_archive_fingerprint_counts_files_and_bytes(self):
        out = self.make_dir() / 'm.tgz'
        fp = media.archive_media(self._tree(), out)
        self.assertEqual((fp['files'], fp['bytes']), (2, 7))
        self.assertEqual(fp, media.fingerprint_archive(out))

    def test_changing_a_file_changes_the_fingerprint(self):
        tree, out = self._tree(), self.make_dir() / 'm.tgz'
        before = media.archive_media(tree, out)['tree_sha256']
        (tree / 'b.txt').write_bytes(b'BBBX')
        self.assertNotEqual(before, media.archive_media(tree, out)['tree_sha256'])

    def test_a_missing_folder_gives_an_empty_archive(self):
        fp = media.archive_media(self.make_dir() / 'nope', self.make_dir() / 'm.tgz')
        self.assertEqual((fp['files'], fp['bytes']), (0, 0))

    def test_extract_restores_identical_files(self):
        tree, out, dest = self._tree(), self.make_dir() / 'm.tgz', self.make_dir()
        media.archive_media(tree, out)
        media.extract_archive(out, dest)
        self.assertEqual((dest / 'payment_proofs' / 'a.png').read_bytes(), b'AAA')
        self.assertEqual((dest / 'b.txt').read_bytes(), b'BBBB')


# --- dumper -----------------------------------------------------------------------

class DumperTests(TempDirMixin, TestCase):
    def test_a_configured_path_with_spaces_is_found(self):
        spaced = self.make_dir() / 'My Tools'
        spaced.mkdir()
        tool = spaced / 'mysqldump.exe'
        tool.write_bytes(b'')
        self.assertEqual(dumper._tool(str(tool), 'mysqldump'), [str(tool)])

    def test_a_missing_tool_explains_what_to_do(self):
        with self.assertRaisesMessage(dumper.BackupToolError, 'BACKUP_MYSQLDUMP'):
            dumper._tool('definitely-not-installed-xyz', 'mysqldump')

    def _fake_dump(self, script):
        return mock.patch.object(dumper, 'dump_command', return_value=[sys.executable, '-c', script])

    def test_a_complete_dump_is_written_gzipped(self):
        dest = self.make_dir() / 'd.gz'
        with self._fake_dump('print("CREATE TABLE t;"); print("-- Dump completed on 2026")'):
            dumper.dump_database(dest)
        self.assertIn('CREATE TABLE t;', gzip.open(dest, 'rt').read())

    def test_a_dump_without_the_completion_marker_is_rejected(self):
        with self._fake_dump('print("CREATE TABLE t;")'):
            with self.assertRaisesMessage(dumper.BackupToolError, 'incomplete'):
                dumper.dump_database(self.make_dir() / 'd.gz')

    def test_a_failing_tool_reports_its_error(self):
        script = 'import sys; sys.stderr.write("Access denied"); sys.exit(2)'
        with self._fake_dump(script):
            with self.assertRaisesMessage(dumper.BackupToolError, 'Access denied'):
                dumper.dump_database(self.make_dir() / 'd.gz')

    def test_the_password_is_passed_in_the_environment_not_the_command_line(self):
        fake_db = {**settings.DATABASES['default'], 'PASSWORD': 'sup3r-secret'}
        with mock.patch.object(dumper, '_db', return_value=fake_db), \
                mock.patch.object(dumper, '_tool', return_value=['mysqldump']), \
                mock.patch.object(dumper, '_supports', return_value=False):
            args = dumper.dump_command()
            self.assertNotIn('sup3r-secret', ' '.join(args))
            self.assertEqual(dumper._env()['MYSQL_PWD'], 'sup3r-secret')

    def test_table_counts_reads_real_tables(self):
        from django.db import connection

        with connection.cursor() as cursor:
            counts = dumper.table_counts(cursor)
        self.assertIn('backups_backuprun', counts)
        self.assertIn('accounts_user', counts)


# --- the backup run -----------------------------------------------------------------

def _fake_database(workdir):
    path = workdir / 'database.plain'
    with gzip.open(path, 'wb') as handle:
        handle.write(b'-- fake dump\n-- Dump completed on 2026\n')
    return path, {'tables': {'accounts_user': 3, 'django_session': 5}}


@override_settings(BACKUP_ENCRYPTION_KEY=KEY, BACKUP_STORAGE='local')
class RunBackupTests(TempDirMixin, TestCase):
    def setUp(self):
        self.store = self.make_dir()
        self.storage = LocalStorage(self.store)
        self.media_root = self.make_dir()
        (self.media_root / 'proof.png').write_bytes(b'PROOF-BYTES')
        media_override = override_settings(MEDIA_ROOT=self.media_root)
        media_override.enable()
        self.addCleanup(media_override.disable)
        parts = tuple(
            (kind, ext, _fake_database if kind == BackupRun.Kind.DATABASE else builder)
            for kind, ext, builder in service.PARTS
        )
        patcher = mock.patch.object(service, 'PARTS', parts)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = User.objects.create_user(username='bk-owner', password='pw', role=User.Role.OWNER)

    def _keys(self, prefix=''):
        return [k for k, _, _ in self.storage.list(prefix)]

    def _manifest(self, key):
        sealed, plain = self.store / 'm.enc', self.store / 'm.json'
        self.storage.get(key, sealed)
        decrypt_file(sealed, plain, load_key(KEY))
        return json.loads(plain.read_text())

    def test_a_run_uploads_four_encrypted_files_and_records_them(self):
        now = _utc(2026, 9, 16, 19, 0, 0)  # a Wednesday
        runs = service.run_backup('cron', storage=self.storage, now=now)

        self.assertEqual({r.status for r in runs}, {'success'})
        self.assertEqual(
            sorted(self._keys('daily/')),
            ['daily/database-20260916-190000Z.sql.gz.enc', 'daily/erasures-20260916-190000Z.json.enc',
             'daily/manifest-20260916-190000Z.json.enc', 'daily/media-20260916-190000Z.tar.gz.enc'],
        )
        self.assertEqual(self._keys('weekly/') + self._keys('monthly/'), [])
        self.assertEqual(BackupRun.objects.filter(status='success').count(), 4)
        self.assertNotIn(b'PROOF-BYTES', (self.store / 'daily' / 'media-20260916-190000Z.tar.gz.enc').read_bytes())

    def test_sundays_also_write_weekly_and_the_first_also_monthly(self):
        service.run_backup(storage=self.storage, now=_utc(2026, 9, 20, 19, 0, 0))  # Sunday
        self.assertEqual(len(self._keys('weekly/')), 4)
        self.assertEqual(self._keys('monthly/'), [])

        service.run_backup(storage=self.storage, now=_utc(2026, 9, 1, 19, 0, 0))  # Tuesday the 1st
        self.assertEqual(len(self._keys('monthly/')), 4)

        self.assertEqual(service.prefixes_for(_utc(2026, 11, 1)), ['daily', 'weekly', 'monthly'])  # a Sunday 1st

    def test_the_manifest_describes_every_part_and_matches_what_was_stored(self):
        service.run_backup(storage=self.storage, now=_utc(2026, 9, 16, 19, 0, 0))
        manifest = self._manifest('daily/manifest-20260916-190000Z.json.enc')

        self.assertEqual(manifest['failed_parts'], [])
        self.assertEqual(set(manifest['parts']), {'database', 'media', 'erasures'})
        for name in manifest['parts']:
            part = manifest['parts'][name]
            stored = self.store / part['keys'][0]
            self.assertEqual(part['sha256'], service._sha256(stored))
            self.assertEqual(part['size'], stored.stat().st_size)
        self.assertEqual(manifest['parts']['database']['tables']['accounts_user'], 3)
        self.assertEqual(manifest['parts']['media']['media']['files'], 1)

    def test_one_failing_part_does_not_cost_the_others(self):
        def broken(workdir):
            raise RuntimeError('disk on fire')

        parts = tuple((k, e, broken if k == BackupRun.Kind.MEDIA else b) for k, e, b in service.PARTS)
        with mock.patch.object(service, 'PARTS', parts):
            runs = service.run_backup(storage=self.storage, now=_utc(2026, 9, 16, 19, 0, 0))

        by_kind = {r.kind: r for r in runs}
        self.assertEqual(by_kind['media'].status, 'failed')
        self.assertIn('disk on fire', by_kind['media'].error)
        self.assertEqual(by_kind['database'].status, 'success')
        manifest = self._manifest('daily/manifest-20260916-190000Z.json.enc')
        self.assertEqual(manifest['failed_parts'], ['media'])
        self.assertNotIn('media', manifest['parts'])
        self.assertTrue(Notification.objects.filter(user=self.owner, title='Backup failed').exists())

    def test_a_storage_failure_is_recorded_as_a_failed_part(self):
        with mock.patch.object(LocalStorage, 'put', side_effect=OSError('bucket unreachable')):
            runs = service.run_backup(storage=self.storage, now=_utc(2026, 9, 16, 19, 0, 0))
        self.assertTrue(all(r.status == 'failed' for r in runs))
        self.assertIn('bucket unreachable', runs[0].error)

    @override_settings(BACKUP_ENCRYPTION_KEY='')
    def test_a_missing_key_is_reported_not_raised(self):
        runs = service.run_backup(storage=self.storage)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, 'failed')
        self.assertIn('BACKUP_ENCRYPTION_KEY', runs[0].error)
        self.assertTrue(Notification.objects.filter(user=self.owner, title='Backup could not start').exists())

    def test_two_backups_cannot_run_at_once(self):
        BackupRun.objects.create(run_id='x', kind='database', status='running', started_at=timezone.now())
        with self.assertRaises(service.BackupInProgress):
            service.run_backup(storage=self.storage)

    def test_a_run_that_died_hours_ago_does_not_block_new_ones(self):
        stuck = BackupRun.objects.create(
            run_id='x', kind='database', status='running',
            started_at=timezone.now() - timedelta(hours=4),
        )
        runs = service.run_backup(storage=self.storage)
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, 'failed')
        self.assertIn('Interrupted', stuck.error)
        self.assertEqual({r.status for r in runs}, {'success'})

    def test_an_overdue_previous_backup_notifies_but_a_first_run_does_not(self):
        service.run_backup(storage=self.storage, now=_utc(2026, 9, 16, 19, 0, 0))
        self.assertFalse(Notification.objects.filter(title='Backups were overdue').exists())

        BackupRun.objects.all().update(finished_at=timezone.now() - timedelta(hours=40))
        service.run_backup(storage=self.storage)
        self.assertTrue(Notification.objects.filter(user=self.owner, title='Backups were overdue').exists())

    def test_the_erasure_list_holds_ids_only_never_personal_data(self):
        victim = User.objects.create_user(
            username='juan_private', password='pw', email='juan.private@example.com',
            first_name='Juan', role=User.Role.CUSTOMER,
        )
        hard_delete_personal_data(victim, performed_by=self.owner)
        service.run_backup(storage=self.storage, now=_utc(2026, 9, 16, 19, 0, 0))

        sealed, plain = self.store / 'e.enc', self.store / 'e.json'
        self.storage.get('daily/erasures-20260916-190000Z.json.enc', sealed)
        decrypt_file(sealed, plain, load_key(KEY))
        text = plain.read_text()
        ledger = json.loads(text)['erased_accounts']
        self.assertEqual([e['id'] for e in ledger], [victim.pk])
        for personal in ('juan', 'example.com', 'Juan'):
            self.assertNotIn(personal, text)

    def test_freshness(self):
        self.assertTrue(service.is_overdue('database'))
        service.run_backup(storage=self.storage)
        self.assertFalse(service.is_overdue('database'))
        self.assertTrue(service.is_overdue('database', now=timezone.now() + timedelta(hours=27)))


class SetupCheckTests(TestCase):
    @override_settings(
        BACKUP_ENCRYPTION_KEY=KEY, BACKUP_STORAGE='b2', B2_BUCKET='bucket',
        B2_ENDPOINT='https://s3.example.com', B2_KEY_ID='SECRET-KEY-ID',
        B2_APPLICATION_KEY='SECRET-APP-KEY', BACKUP_TRIGGER_TOKEN=TOKEN,
    )
    def test_reports_status_without_revealing_any_secret(self):
        checks = {label: (ok, detail) for label, ok, detail in service.setup_checks()}

        self.assertTrue(checks['Encryption key'][0])
        self.assertTrue(checks['Backblaze storage'][0])
        self.assertTrue(checks['Automatic schedule'][0])
        blob = json.dumps(checks)
        for secret in (KEY, 'SECRET-KEY-ID', 'SECRET-APP-KEY', TOKEN):
            self.assertNotIn(secret, blob)

    @override_settings(BACKUP_ENCRYPTION_KEY='', BACKUP_STORAGE='b2', B2_BUCKET='', B2_ENDPOINT='',
                       B2_KEY_ID='', B2_APPLICATION_KEY='', BACKUP_TRIGGER_TOKEN='')
    def test_flags_what_is_missing(self):
        checks = {label: ok for label, ok, _ in service.setup_checks()}
        self.assertFalse(checks['Encryption key'])
        self.assertFalse(checks['Backblaze storage'])

    @override_settings(BACKUP_SCHEDULER=False)
    def test_a_switched_off_schedule_is_flagged(self):
        checks = {label: ok for label, ok, _ in service.setup_checks()}
        self.assertFalse(checks['Automatic schedule'])

    @override_settings(BACKUP_STORAGE='local')
    def test_local_storage_is_flagged_as_not_off_site(self):
        checks = {label: ok for label, ok, _ in service.setup_checks()}
        self.assertFalse(checks['Off-site storage'])


# --- the daily trigger ---------------------------------------------------------------

@override_settings(BACKUP_TRIGGER_TOKEN=TOKEN)
class TriggerViewTests(TestCase):
    url = '/monitoring/backups/trigger/'

    def _post(self, token=TOKEN, **extra):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {token}'} if token is not None else {}
        return Client(enforce_csrf_checks=True).post(self.url, **headers, **extra)

    def test_the_right_token_starts_a_backup_without_a_session_or_csrf_token(self):
        with mock.patch('backups.service.spawn_backup_process') as spawn:
            response = self._post()
        self.assertEqual(response.status_code, 202)
        spawn.assert_called_once_with('cron')

    def test_a_wrong_or_missing_token_is_refused_and_starts_nothing(self):
        with mock.patch('backups.service.spawn_backup_process') as spawn:
            for token in ('wrong', '', None):
                with self.subTest(token=token):
                    self.assertEqual(self._post(token).status_code, 403)
        spawn.assert_not_called()

    def test_a_token_in_the_query_string_is_not_accepted(self):
        with mock.patch('backups.service.spawn_backup_process') as spawn:
            response = Client().post(f'{self.url}?token={TOKEN}')
        self.assertEqual(response.status_code, 403)
        spawn.assert_not_called()

    def test_get_is_not_allowed(self):
        self.assertEqual(Client().get(self.url, HTTP_AUTHORIZATION=f'Bearer {TOKEN}').status_code, 405)

    def test_a_backup_already_running_answers_409(self):
        BackupRun.objects.create(run_id='x', kind='database', status='running', started_at=timezone.now())
        with mock.patch('backups.service.spawn_backup_process') as spawn:
            self.assertEqual(self._post().status_code, 409)
        spawn.assert_not_called()

    @override_settings(BACKUP_TRIGGER_TOKEN='')
    def test_with_no_token_configured_the_endpoint_does_not_exist(self):
        self.assertEqual(self._post('anything').status_code, 404)


class TriggerCliTests(SimpleTestCase):
    ENV = {'BACKUP_TRIGGER_URL': 'https://x.example/trigger/', 'BACKUP_TRIGGER_TOKEN': 't'}

    def _response(self, status=202):
        response = mock.MagicMock()
        response.status = status
        response.__enter__.return_value = response
        return response

    def _http_error(self, code):
        return urllib.error.HTTPError('u', code, 'err', {}, io.BytesIO())

    def test_success_exits_zero_and_sends_the_bearer_token(self):
        opener = mock.Mock(return_value=self._response())
        self.assertEqual(trigger_cli.main(self.ENV, opener, sleep=mock.Mock()), 0)
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer t')
        self.assertEqual(request.get_method(), 'POST')

    def test_missing_settings_exit_two(self):
        self.assertEqual(trigger_cli.main({}, mock.Mock(), sleep=mock.Mock()), 2)

    def test_a_refusal_is_not_retried(self):
        opener, sleep = mock.Mock(side_effect=self._http_error(403)), mock.Mock()
        self.assertEqual(trigger_cli.main(self.ENV, opener, sleep), 1)
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()

    def test_a_server_error_is_retried_then_succeeds(self):
        opener = mock.Mock(side_effect=[self._http_error(502), self._response()])
        self.assertEqual(trigger_cli.main(self.ENV, opener, sleep=mock.Mock()), 0)
        self.assertEqual(opener.call_count, 2)

    def test_an_unreachable_service_fails_after_the_last_attempt(self):
        opener = mock.Mock(side_effect=urllib.error.URLError('down'))
        self.assertEqual(trigger_cli.main(self.ENV, opener, sleep=mock.Mock()), 1)
        self.assertEqual(opener.call_count, trigger_cli.ATTEMPTS)

    def test_the_cli_needs_no_django(self):
        source = Path(trigger_cli.__file__).read_text()
        for forbidden in ('import django', 'from django', 'from backups', 'import backups'):
            self.assertNotIn(forbidden, source)


# --- the status page -------------------------------------------------------------------

class StatusPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='st-owner', password='pw', role=User.Role.OWNER)
        self.client.force_login(self.owner)
        self.url = reverse('backup_status')

    def test_the_owner_and_a_superuser_can_view_it(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        admin = User.objects.create_user(username='root', password='pw', role=User.Role.STAFF, is_superuser=True)
        self.client.force_login(admin)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_nobody_else_can_view_or_act(self):
        for role in (User.Role.BRANCH_MANAGER, User.Role.STAFF, User.Role.CUSTOMER):
            user = User.objects.create_user(username=f'x-{role}', password='pw', role=role)
            self.client.force_login(user)
            with self.subTest(role=role), mock.patch('backups.service.spawn_backup_process') as spawn:
                self.assertEqual(self.client.get(self.url).status_code, 302)
                self.client.post(reverse('backup_run_now'))
                self.client.post(reverse('backup_record_restore_test'), {
                    'tested_on': '2026-09-01', 'backup_used': 'x', 'outcome': 'passed'})
                spawn.assert_not_called()
        self.assertEqual(RestoreTestRecord.objects.count(), 0)
        self.client.logout()
        self.assertIn('/login', self.client.get(self.url)['Location'])

    def test_a_fresh_install_says_never_run(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Never run')
        self.assertContains(response, 'None recorded')

    def test_the_setup_check_lists_failures_first_and_counts_them(self):
        checks = [('Good one', True, 'fine.'), ('Bad one', False, 'broken.'),
                  ('Good two', True, 'fine.'), ('Bad two', False, 'broken too.')]
        with mock.patch('backups.service.setup_checks', return_value=checks):
            response = self.client.get(self.url)
        self.assertEqual([c[0] for c in response.context['checks']],
                         ['Bad one', 'Bad two', 'Good one', 'Good two'])
        self.assertEqual(response.context['setup_issues'], 2)
        self.assertContains(response, '2 of 4 need fixing')

        with mock.patch('backups.service.setup_checks', return_value=[('Good', True, 'fine.')]):
            response = self.client.get(self.url)
        self.assertContains(response, 'All OK')
        self.assertNotContains(response, 'need fixing')

    def test_an_empty_history_shows_a_compact_empty_state(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'No backups yet')
        self.assertNotContains(response, 'Latest 0 runs')

    def test_it_lists_runs_and_shows_freshness(self):
        BackupRun.objects.create(
            run_id='a', kind='database', status='success', size_bytes=2048,
            started_at=timezone.now(), finished_at=timezone.now(),
        )
        BackupRun.objects.create(run_id='a', kind='media', status='failed', error='disk on fire',
                                 started_at=timezone.now(), finished_at=timezone.now())
        response = self.client.get(self.url)
        self.assertContains(response, 'Up to date')
        self.assertContains(response, 'disk on fire')
        self.assertContains(response, '2.0')  # 2048 bytes -> "2.0 KB"

    def test_an_old_backup_is_flagged_overdue(self):
        BackupRun.objects.create(
            run_id='a', kind='database', status='success', started_at=timezone.now(),
            finished_at=timezone.now() - timedelta(hours=40),
        )
        self.assertContains(self.client.get(self.url), 'Overdue')

    def test_run_now_starts_a_backup_once(self):
        with mock.patch('backups.service.spawn_backup_process') as spawn:
            response = self.client.post(reverse('backup_run_now'))
        self.assertRedirects(response, self.url)
        spawn.assert_called_once_with('manual')

        BackupRun.objects.create(run_id='x', kind='database', status='running', started_at=timezone.now())
        with mock.patch('backups.service.spawn_backup_process') as spawn:
            self.client.post(reverse('backup_run_now'))
        spawn.assert_not_called()

    def test_run_now_needs_post(self):
        self.assertEqual(self.client.get(reverse('backup_run_now')).status_code, 405)

    def test_recording_a_restore_test(self):
        self.client.post(reverse('backup_record_restore_test'), {
            'tested_on': date.today().isoformat(), 'backup_used': '2026-09-22 daily',
            'outcome': 'passed', 'notes': 'All 50 tables matched.',
        })
        record = RestoreTestRecord.objects.get()
        self.assertEqual((record.outcome, record.recorded_by), ('passed', self.owner))
        response = self.client.get(self.url)
        self.assertContains(response, '2026-09-22 daily')
        self.assertNotContains(response, 'None recorded')

    def test_bad_restore_test_input_is_refused(self):
        bad_inputs = [
            {'tested_on': 'nope', 'backup_used': 'x', 'outcome': 'passed'},
            {'tested_on': (date.today() + timedelta(days=3)).isoformat(), 'backup_used': 'x', 'outcome': 'passed'},
            {'tested_on': date.today().isoformat(), 'backup_used': '  ', 'outcome': 'passed'},
            {'tested_on': date.today().isoformat(), 'backup_used': 'x', 'outcome': 'maybe'},
        ]
        for data in bad_inputs:
            with self.subTest(data=data):
                self.client.post(reverse('backup_record_restore_test'), data)
        self.assertEqual(RestoreTestRecord.objects.count(), 0)

    def test_an_old_restore_test_is_flagged_as_due(self):
        RestoreTestRecord.objects.create(
            tested_on=date.today() - timedelta(days=120), backup_used='old', outcome='passed')
        self.assertContains(self.client.get(self.url), 'Due again')

    def test_the_sidebar_link_is_owner_only(self):
        self.assertContains(self.client.get(reverse('reports_dashboard')), self.url)
        manager = User.objects.create_user(username='m', password='pw', role=User.Role.BRANCH_MANAGER)
        self.client.force_login(manager)
        self.assertNotContains(self.client.get(reverse('reports_dashboard')), self.url)


# --- commands ----------------------------------------------------------------------------

class EraseAgainTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user(username='era-a', password='pw', email='a@example.com', role=User.Role.CUSTOMER)
        self.b = User.objects.create_user(username='era-b', password='pw', email='b@example.com', role=User.Role.CUSTOMER)
        self.owner = User.objects.create_user(username='era-o', password='pw', role=User.Role.OWNER)
        hard_delete_personal_data(self.b, performed_by=self.owner)

    def _ledger(self, *ids):
        return {'erased_accounts': [{'id': i, 'deleted_at': '2026-09-01T00:00:00+00:00'} for i in ids]}

    def test_reapplies_only_what_is_still_present(self):
        result = reapply(self._ledger(self.a.pk, self.b.pk, 987654))

        self.assertEqual(result, {'reapplied': [self.a.pk], 'already_erased': 1, 'not_present': 1})
        self.a.refresh_from_db()
        self.assertIsNotNone(self.a.deleted_at)
        self.assertNotEqual(self.a.email, 'a@example.com')

    def test_dry_run_changes_nothing(self):
        result = reapply(self._ledger(self.a.pk), dry_run=True)

        self.assertEqual(result['reapplied'], [self.a.pk])
        self.a.refresh_from_db()
        self.assertIsNone(self.a.deleted_at)
        self.assertEqual(self.a.email, 'a@example.com')

    def test_running_it_twice_is_harmless(self):
        reapply(self._ledger(self.a.pk))
        self.assertEqual(reapply(self._ledger(self.a.pk))['reapplied'], [])

    def test_the_command_reads_a_ledger_file(self):
        path = Path(tempfile.mkdtemp()) / 'ledger.json'
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text(json.dumps(self._ledger(self.a.pk)))
        out = io.StringIO()
        call_command('reapply_erasures', ledger=str(path), stdout=out)
        self.assertIn('Erased 1 account(s)', out.getvalue())
        self.a.refresh_from_db()
        self.assertIsNotNone(self.a.deleted_at)


@override_settings(BACKUP_ENCRYPTION_KEY=KEY, BACKUP_STORAGE='local')
class CommandTests(TempDirMixin, TestCase):
    """run_backup and verify_backup, with the database tools stubbed out."""

    def setUp(self):
        self.store = self.make_dir()
        storage_override = override_settings(BACKUP_LOCAL_DIR=self.store, MEDIA_ROOT=self._media())
        storage_override.enable()
        self.addCleanup(storage_override.disable)
        parts = tuple((k, e, _fake_database if k == BackupRun.Kind.DATABASE else b) for k, e, b in service.PARTS)
        patcher = mock.patch.object(service, 'PARTS', parts)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _media(self):
        root = self.make_dir()
        (root / 'proof.png').write_bytes(b'PROOF')
        return root

    def _backup(self, stamp_day=16):
        service.run_backup(now=_utc(2026, 9, stamp_day, 19, 0, 0))

    def _verify(self, counts=None, **options):
        restored = counts if counts is not None else {'accounts_user': 3, 'django_session': 5}
        out = io.StringIO()
        with mock.patch.object(dumper, 'database_exists', return_value=options.pop('exists', False)), \
                mock.patch.object(dumper, 'create_database'), mock.patch.object(dumper, 'drop_database') as drop, \
                mock.patch.object(dumper, 'restore_sql'), \
                mock.patch.object(dumper, 'counts_for_database', return_value=restored):
            call_command('verify_backup', stdout=out, **options)
        return out.getvalue(), drop

    def test_run_backup_command_reports_each_part(self):
        out = io.StringIO()
        call_command('run_backup', stdout=out)
        text = out.getvalue()
        for label in ('Database', 'Uploaded files', 'Erased-accounts list', 'Manifest'):
            self.assertIn(f'OK      {label}', text)

    def test_run_backup_command_exits_non_zero_when_a_part_fails(self):
        with mock.patch.object(LocalStorage, 'put', side_effect=OSError('nope')):
            with self.assertRaisesMessage(CommandError, 'failed'):
                call_command('run_backup', stdout=io.StringIO())

    def test_an_unexpected_crash_is_recorded_so_it_shows_on_the_status_page(self):
        with mock.patch('backups.management.commands.run_backup.run_backup', side_effect=RuntimeError('boom')):
            with self.assertRaisesMessage(CommandError, 'boom'):
                call_command('run_backup', stdout=io.StringIO())
        failed = BackupRun.objects.get()
        self.assertEqual(failed.status, 'failed')
        self.assertIn('RuntimeError: boom', failed.error)

    def test_run_backup_command_refuses_to_overlap(self):
        BackupRun.objects.create(run_id='x', kind='database', status='running', started_at=timezone.now())
        with self.assertRaisesMessage(CommandError, 'already running'):
            call_command('run_backup', stdout=io.StringIO())

    def test_verify_passes_on_a_good_backup_and_cleans_up(self):
        self._backup()
        text, drop = self._verify()

        self.assertIn('RESTORE CHECK PASSED', text)
        self.assertIn('identical to the manifest', text)
        drop.assert_called()

    def test_busy_tables_may_differ_but_business_tables_may_not(self):
        self._backup()
        text, _ = self._verify(counts={'accounts_user': 3, 'django_session': 9})
        self.assertIn('RESTORE CHECK PASSED', text)
        self.assertIn('django_session 5 -> 9', text)

        with self.assertRaisesMessage(CommandError, 'failed'):
            self._verify(counts={'accounts_user': 2, 'django_session': 5})
        with self.assertRaisesMessage(CommandError, 'failed'):
            self._verify(counts={'django_session': 5})  # a table went missing

    def test_a_corrupted_download_is_caught_by_the_checksum(self):
        self._backup()
        target = self.store / 'daily' / 'database-20260916-190000Z.sql.gz.enc'
        blob = bytearray(target.read_bytes())
        blob[-3] ^= 0xFF
        target.write_bytes(bytes(blob))
        with self.assertRaisesMessage(CommandError, 'checksum'):
            self._verify()

    def test_the_wrong_key_is_reported_clearly(self):
        self._backup()
        with override_settings(BACKUP_ENCRYPTION_KEY=generate_key()):
            with self.assertRaisesMessage(CommandError, 'wrong BACKUP_ENCRYPTION_KEY'):
                self._verify()

    def test_changed_uploaded_files_fail_the_check(self):
        self._backup()
        sealed, plain = self.store / 'm.enc', self.store / 'm.tgz'
        # Swap in an archive with different contents but the manifest's checksum
        # left alone would be caught earlier; here alter the manifest's promise.
        manifest_key = 'daily/manifest-20260916-190000Z.json.enc'
        storage = LocalStorage(self.store)
        storage.get(manifest_key, sealed)
        decrypt_file(sealed, plain, load_key(KEY))
        data = json.loads(plain.read_text())
        data['parts']['media']['media']['tree_sha256'] = '0' * 64
        plain.write_text(json.dumps(data))
        encrypt_file(plain, sealed, load_key(KEY))
        storage.put(manifest_key, sealed)
        with self.assertRaisesMessage(CommandError, 'failed'):
            self._verify()

    def test_it_refuses_to_use_the_live_database_as_scratch(self):
        self._backup()
        with self.assertRaisesMessage(CommandError, 'must not be the live database'):
            call_command('verify_backup', scratch_db=settings.DATABASES['default']['NAME'], stdout=io.StringIO())

    def test_an_existing_scratch_database_needs_replace(self):
        self._backup()
        with self.assertRaisesMessage(CommandError, '--replace'):
            self._verify(exists=True)
        text, _ = self._verify(exists=True, replace=True)
        self.assertIn('PASSED', text)

    def test_no_backups_and_unknown_stamp(self):
        with self.assertRaisesMessage(CommandError, 'No backups found'):
            self._verify()
        self._backup()
        with self.assertRaisesMessage(CommandError, 'No such backup'):
            self._verify(stamp='19990101-000000Z')

    def test_the_erasure_list_can_be_written_out_for_a_real_restore(self):
        self._backup()
        target = self.make_dir() / 'ledger.json'
        self._verify(ledger_out=str(target))
        self.assertEqual(json.loads(target.read_text()), {'erased_accounts': []})

    def test_media_can_be_extracted(self):
        self._backup()
        dest = self.make_dir()
        self._verify(media_dir=str(dest))
        self.assertEqual((dest / 'proof.png').read_bytes(), b'PROOF')


def _tools_available():
    try:
        dumper._tool(settings.BACKUP_MYSQLDUMP, 'mysqldump')
        dumper._tool(settings.BACKUP_MYSQL, 'mysql')
        return True
    except dumper.BackupToolError:
        return False


@override_settings(BACKUP_ENCRYPTION_KEY=KEY, BACKUP_STORAGE='local')
class RealDatabaseRoundTripTests(TempDirMixin, TestCase):
    """The real mysqldump -> encrypt -> decrypt -> mysql restore path against the
    actual test database. Skipped where the MySQL command-line tools aren't
    installed (set BACKUP_MYSQLDUMP / BACKUP_MYSQL to their paths to run it)."""

    def setUp(self):
        if not _tools_available():
            self.skipTest('mysqldump / mysql are not installed')
        media_root = self.make_dir()
        (media_root / 'proof.png').write_bytes(b'PROOF')
        overrides = override_settings(BACKUP_LOCAL_DIR=self.make_dir(), MEDIA_ROOT=media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def test_backup_then_restore_check_passes_against_the_real_database(self):
        runs = service.run_backup(now=_utc(2026, 9, 16, 19, 0, 0))
        self.assertEqual({r.status for r in runs}, {'success'}, [r.error for r in runs])

        out = io.StringIO()
        call_command('verify_backup', replace=True, stdout=out)
        self.assertIn('RESTORE CHECK PASSED', out.getvalue())
        self.assertFalse(dumper.database_exists(f'{settings.DATABASES["default"]["NAME"]}_restore_check'))


# --- the built-in daily schedule -----------------------------------------------------------

class SchedulerTests(TestCase):
    slot = _utc(2026, 9, 22, 19, 0, 0)  # 03:00 the next morning, Manila time

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _attempt(self, status, started, kind='database'):
        return BackupRun.objects.create(
            run_id='x', kind=kind, status=status, started_at=started, finished_at=started,
        )

    def test_next_run_is_todays_slot_until_it_passes_then_tomorrows(self):
        self.assertEqual(scheduler.next_run(self.slot - timedelta(hours=1)), self.slot)
        self.assertEqual(scheduler.next_run(self.slot + timedelta(minutes=1)), self.slot + timedelta(days=1))

    def test_before_todays_slot_only_a_missed_run_from_the_previous_slot_matters(self):
        yesterday = self.slot - timedelta(days=1)
        self.assertTrue(scheduler.due(self.slot - timedelta(minutes=1)))  # nothing ever ran: catch up
        self._attempt('success', yesterday + timedelta(minutes=5))
        self.assertFalse(scheduler.due(self.slot - timedelta(minutes=1)))

    def test_due_from_the_slot_onward_when_nothing_has_run(self):
        for offset in (timedelta(0), timedelta(hours=5)):  # the second is a catch-up after downtime
            with self.subTest(offset=offset):
                self.assertTrue(scheduler.due(self.slot + offset))

    def test_a_success_or_a_running_backup_since_the_slot_means_not_due(self):
        self._attempt('success', self.slot + timedelta(minutes=2))
        self.assertFalse(scheduler.due(self.slot + timedelta(hours=1)))
        BackupRun.objects.all().delete()
        self._attempt('running', self.slot + timedelta(minutes=2))
        self.assertFalse(scheduler.due(self.slot + timedelta(hours=1)))

    def test_yesterdays_backup_does_not_count_for_today(self):
        self._attempt('success', self.slot - timedelta(hours=20))
        self.assertTrue(scheduler.due(self.slot))

    def test_a_manual_backup_taken_before_the_slot_does_not_replace_it(self):
        self._attempt('success', self.slot - timedelta(hours=3))
        self.assertTrue(scheduler.due(self.slot + timedelta(minutes=1)))

    def test_a_failed_attempt_is_retried_after_thirty_minutes_up_to_three_times(self):
        self._attempt('failed', self.slot + timedelta(minutes=1))
        self.assertFalse(scheduler.due(self.slot + timedelta(minutes=10)))
        self.assertTrue(scheduler.due(self.slot + timedelta(minutes=32)))

        self._attempt('failed', self.slot + timedelta(minutes=33))
        self._attempt('failed', self.slot + timedelta(minutes=70))
        self.assertFalse(scheduler.due(self.slot + timedelta(hours=6)))  # three tries used up

    def test_tick_starts_one_backup_and_a_second_worker_does_not_double_it(self):
        with mock.patch('backups.service.spawn_backup_process') as spawn:
            self.assertTrue(scheduler.tick(self.slot + timedelta(minutes=1)))
            self.assertFalse(scheduler.tick(self.slot + timedelta(minutes=1, seconds=5)))
        spawn.assert_called_once_with('scheduled')

    def test_tick_does_nothing_when_the_latest_slot_is_already_done(self):
        self._attempt('success', self.slot - timedelta(days=1) + timedelta(minutes=5))
        with mock.patch('backups.service.spawn_backup_process') as spawn:
            self.assertFalse(scheduler.tick(self.slot - timedelta(hours=2)))
        spawn.assert_not_called()

    def test_a_run_that_died_hours_ago_is_cleared_and_todays_backup_still_happens(self):
        stuck = BackupRun.objects.create(
            run_id='x', kind='database', status='running', started_at=self.slot - timedelta(hours=4),
        )
        with mock.patch('backups.service.spawn_backup_process') as spawn:
            self.assertTrue(scheduler.tick(self.slot + timedelta(minutes=1)))
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, 'failed')
        spawn.assert_called_once()

    @override_settings(BACKUP_SCHEDULE_HOUR_UTC=2)
    def test_the_hour_is_configurable(self):
        self.assertEqual(scheduler.slot_start(_utc(2026, 9, 22, 12)).hour, 2)

    def test_the_thread_only_starts_inside_gunicorn(self):
        with mock.patch.object(sys, 'argv', ['/opt/venv/bin/gunicorn', 'gerlyn_is.wsgi']):
            self.assertTrue(scheduler.running_under_gunicorn())
        for argv in (['manage.py', 'run_backup'], ['manage.py', 'test'], ['python']):
            with mock.patch.object(sys, 'argv', argv):
                self.assertFalse(scheduler.running_under_gunicorn())

    def test_no_scheduler_thread_runs_during_tests_or_manage_commands(self):
        import threading

        self.assertNotIn('backup-scheduler', [t.name for t in threading.enumerate()])
