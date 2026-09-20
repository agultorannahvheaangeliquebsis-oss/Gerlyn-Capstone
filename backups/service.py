"""Running a backup: dump, archive, encrypt, upload, record.

One run makes four encrypted files and uploads each to `daily/`, plus to
`weekly/` on Sundays and `monthly/` on the 1st (the storage bucket's lifecycle
rules then expire each folder on its own schedule — nothing here deletes):

  database-<stamp>.sql.gz.enc     the whole MySQL database
  media-<stamp>.tar.gz.enc        MEDIA_ROOT (payment proofs)
  erasures-<stamp>.json.enc       ids of accounts whose personal data was erased
  manifest-<stamp>.json.enc       what the run produced, checksums, row counts

The erasures list exists for one reason: restoring an old backup would bring
back people who have since been erased. After any restore, the newest list is
re-applied (`manage.py reapply_erasures`).

A part failing does not stop the others — a broken media archive shouldn't cost
you the database backup. Failures are recorded and the Owner is notified.
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from . import dumper, media
from .crypto import BackupCryptoError, encrypt_file, load_key
from .models import BackupRun
from .storage import BackupStorageError, get_storage

logger = logging.getLogger(__name__)

# A run that has been "running" longer than this died (killed, redeployed).
STUCK_AFTER = timedelta(hours=3)


class BackupInProgress(Exception):
    pass


# --- freshness and concurrency ------------------------------------------------

def last_success(kind):
    return (
        BackupRun.objects.filter(kind=kind, status=BackupRun.Status.SUCCESS)
        .order_by('-finished_at').first()
    )


def is_overdue(kind=BackupRun.Kind.DATABASE, now=None):
    """True when there has been no successful backup of `kind` recently."""
    now = now or timezone.now()
    latest = last_success(kind)
    return latest is None or now - latest.finished_at > timedelta(hours=settings.BACKUP_STALE_HOURS)


def reap_stuck_runs(now=None):
    now = now or timezone.now()
    return BackupRun.objects.filter(
        status=BackupRun.Status.RUNNING, started_at__lt=now - STUCK_AFTER,
    ).update(
        status=BackupRun.Status.FAILED, finished_at=now,
        error='Interrupted: the process ended before this backup finished.',
    )


def active_run_exists(now=None):
    reap_stuck_runs(now)
    return BackupRun.objects.filter(status=BackupRun.Status.RUNNING).exists()


def spawn_backup_process(trigger):
    """Start `manage.py run_backup` detached from the calling web request, so a
    long backup neither blocks a worker nor is killed when the worker recycles."""
    subprocess.Popen(
        [sys.executable, str(settings.BASE_DIR / 'manage.py'), 'run_backup', f'--trigger={trigger}'],
        cwd=str(settings.BASE_DIR), stdin=subprocess.DEVNULL,
        start_new_session=(os.name != 'nt'),
    )


# --- the parts ------------------------------------------------------------------

def _build_database(workdir):
    path = workdir / 'database.plain'
    dumper.dump_database(path)
    with connection.cursor() as cursor:
        counts = dumper.table_counts(cursor)
    return path, {'tables': counts}


def _build_media(workdir):
    path = workdir / 'media.plain'
    return path, {'media': media.archive_media(settings.MEDIA_ROOT, path)}


def _build_erasures(workdir):
    from accounts.models import User

    ledger = [
        {'id': pk, 'deleted_at': when.isoformat()}
        for pk, when in User.objects.filter(deleted_at__isnull=False)
        .order_by('id').values_list('id', 'deleted_at')
    ]
    path = workdir / 'erasures.plain'
    path.write_text(json.dumps({'erased_accounts': ledger}), encoding='utf-8')
    return path, {'erased_accounts': len(ledger)}


PARTS = (
    (BackupRun.Kind.DATABASE, '.sql.gz', _build_database),
    (BackupRun.Kind.MEDIA, '.tar.gz', _build_media),
    (BackupRun.Kind.ERASURES, '.json', _build_erasures),
)


def prefixes_for(now):
    """daily always; weekly on Sundays; monthly on the 1st (all in UTC)."""
    prefixes = ['daily']
    if now.weekday() == 6:
        prefixes.append('weekly')
    if now.day == 1:
        prefixes.append('monthly')
    return prefixes


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _seal_and_upload(storage, master_key, plain_path, workdir, kind, extension, stamp, prefixes):
    sealed = workdir / f'{kind}.enc'
    encrypt_file(plain_path, sealed, master_key)
    keys = [f'{prefix}/{kind}-{stamp}{extension}.enc' for prefix in prefixes]
    for key in keys:
        storage.put(key, sealed)
    return keys, _sha256(sealed), sealed.stat().st_size


def _finish(run, **fields):
    for name, value in fields.items():
        setattr(run, name, value)
    run.finished_at = timezone.now()
    run.save()


# --- notifications --------------------------------------------------------------

def _ascii(text, limit=200):
    return str(text).encode('ascii', 'replace').decode()[:limit]


def _notify(priority, title, message):
    """Tell the Owner. Never allowed to break the backup it is reporting on."""
    try:
        from django.urls import reverse

        from core.notifications import CATEGORY_SYSTEM, notify

        notify(
            CATEGORY_SYSTEM, priority, title, message,
            action_url=reverse('backup_status'), action_label='View Backups',
        )
    except Exception:
        logger.exception('Could not send the backup notification')


# --- the run --------------------------------------------------------------------

def run_backup(trigger='manual', storage=None, now=None):
    """Perform one full backup. Returns the BackupRun rows it wrote."""
    from core.notifications import PRIORITY_HIGH

    now = now or timezone.now()
    if active_run_exists(now):
        raise BackupInProgress('A backup is already running.')

    run_id = uuid.uuid4().hex
    stamp = now.strftime('%Y%m%d-%H%M%SZ')

    if is_overdue(BackupRun.Kind.DATABASE, now) and last_success(BackupRun.Kind.DATABASE):
        _notify(
            PRIORITY_HIGH, 'Backups were overdue',
            f'No successful database backup in the last {settings.BACKUP_STALE_HOURS} hours '
            f'before this run. Check Backup Status.',
        )

    try:
        master_key = load_key(settings.BACKUP_ENCRYPTION_KEY)
        storage = storage or get_storage()
    except (BackupCryptoError, BackupStorageError) as exc:
        failed = BackupRun.objects.create(
            run_id=run_id, kind=BackupRun.Kind.MANIFEST, trigger=trigger, started_at=now,
            status=BackupRun.Status.FAILED, finished_at=timezone.now(), error=str(exc),
        )
        _notify(PRIORITY_HIGH, 'Backup could not start', _ascii(exc))
        return [failed]

    prefixes = prefixes_for(now)
    runs, manifest_parts, failures = [], {}, []

    with tempfile.TemporaryDirectory(prefix='gerlyn-backup-') as tmp:
        workdir = Path(tmp)
        for kind, extension, build in PARTS:
            run = BackupRun.objects.create(run_id=run_id, kind=kind, trigger=trigger, started_at=timezone.now())
            runs.append(run)
            try:
                plain_path, detail = build(workdir)
                keys, digest, size = _seal_and_upload(
                    storage, master_key, plain_path, workdir, kind.value, extension, stamp, prefixes,
                )
            except Exception as exc:
                logger.exception('Backup part %s failed', kind)
                _finish(run, status=BackupRun.Status.FAILED, error=str(exc)[:2000])
                failures.append((kind, exc))
            else:
                _finish(run, status=BackupRun.Status.SUCCESS, size_bytes=size, sha256=digest,
                        remote_keys=keys, detail=detail)
                manifest_parts[kind.value] = {
                    'keys': keys, 'sha256': digest, 'size': size, **detail,
                }
            finally:
                for leftover in (workdir / f'{kind.value}.plain', workdir / f'{kind.value}.enc'):
                    leftover.unlink(missing_ok=True)

        manifest_run = BackupRun.objects.create(
            run_id=run_id, kind=BackupRun.Kind.MANIFEST, trigger=trigger, started_at=timezone.now(),
        )
        runs.append(manifest_run)
        try:
            plain_path = workdir / 'manifest.plain'
            plain_path.write_text(json.dumps(_manifest(stamp, now, manifest_parts, failures), indent=1),
                                  encoding='utf-8')
            keys, digest, size = _seal_and_upload(
                storage, master_key, plain_path, workdir, 'manifest', '.json', stamp, prefixes,
            )
        except Exception as exc:
            logger.exception('Backup manifest failed')
            _finish(manifest_run, status=BackupRun.Status.FAILED, error=str(exc)[:2000])
            failures.append((BackupRun.Kind.MANIFEST, exc))
        else:
            _finish(manifest_run, status=BackupRun.Status.SUCCESS, size_bytes=size,
                    sha256=digest, remote_keys=keys)

    if failures:
        names = ', '.join(kind.label for kind, _ in failures)
        _notify(PRIORITY_HIGH, 'Backup failed',
                f'These parts did not back up: {names}. {_ascii(failures[0][1])}')
    return runs


def _manifest(stamp, now, parts, failures):
    try:
        from core.version import APP_VERSION
    except Exception:
        APP_VERSION = ''
    return {
        'format': 1,
        'stamp': stamp,
        'created_at': now.isoformat(),
        'app_version': APP_VERSION,
        'parts': parts,
        'failed_parts': [kind.value for kind, _ in failures],
    }


# --- the setup check (status page) ---------------------------------------------

def setup_checks():
    """What is and isn't configured — shown on the status page. Never reveals a
    secret, only whether one is set."""
    checks = []

    try:
        load_key(settings.BACKUP_ENCRYPTION_KEY)
        checks.append(('Encryption key', True, 'Set and valid.'))
    except BackupCryptoError as exc:
        checks.append(('Encryption key', False, str(exc)))

    if settings.BACKUP_STORAGE == 'b2':
        missing = [n for n in ('B2_ENDPOINT', 'B2_KEY_ID', 'B2_APPLICATION_KEY', 'B2_BUCKET')
                   if not getattr(settings, n)]
        checks.append((
            'Backblaze storage', not missing,
            'Configured.' if not missing else 'Not set: ' + ', '.join(missing) + '.',
        ))
    else:
        checks.append(('Off-site storage', False,
                       'Using a local folder — not off-site. Set the B2_* variables for Backblaze.'))

    try:
        checks.append(('mysqldump', True, dumper.tool_version(settings.BACKUP_MYSQLDUMP, 'mysqldump')))
    except (dumper.BackupToolError, OSError, subprocess.SubprocessError, IndexError) as exc:
        checks.append(('mysqldump', False, str(exc)))

    checks.append((
        'Automatic schedule', bool(settings.BACKUP_SCHEDULER),
        f'Runs daily at {settings.BACKUP_SCHEDULE_HOUR_UTC:02d}:00 UTC from the web service.'
        if settings.BACKUP_SCHEDULER else 'Off (BACKUP_SCHEDULER=False) — backups only run when started by hand.',
    ))
    checks.append(('Uploaded-files folder', Path(settings.MEDIA_ROOT).is_dir(),
                   str(settings.MEDIA_ROOT)))
    return checks
