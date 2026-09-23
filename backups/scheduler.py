"""The daily backup schedule, run by the web service itself.

A small background thread in each gunicorn worker wakes once a minute and asks
"is today's backup due?". If it is, it starts `manage.py run_backup` as a
separate process (the same detached spawn "Run backup now" uses), so the web
service needs no second Railway service, no cron, and no token.

Why this is safe to run in every worker: the decision is made from the database
(BackupRun rows), and the one process that wins an atomic cache lock is the only
one that spawns. It is also self-healing: the rule is "no successful backup since
today's scheduled time", not "it is exactly 19:00", so a deploy or restart at
19:00 just delays the backup until the app is back up.

Retries: a failed attempt is retried after 30 minutes, up to 3 attempts a day
(each failure has already notified the Owner).
"""
import logging
import os
import sys
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from . import service
from .models import BackupRun

logger = logging.getLogger(__name__)

CHECK_EVERY_SECONDS = 60
STARTUP_DELAY_SECONDS = 30
RETRY_AFTER = timedelta(minutes=30)
MAX_ATTEMPTS_PER_DAY = 3
LOCK_KEY = 'backups-scheduler-lock'
LOCK_SECONDS = 120


def slot_start(now):
    """The most recent scheduled time at or before `now`
    (BACKUP_SCHEDULE_HOUR_UTC, default 19:00 UTC = 03:00 Manila).

    "Most recent", not "today's": if the app is down across midnight, the missed
    backup is still owed and gets taken as soon as the app is back, rather than
    being forgotten until the next evening. It also means a brand-new install
    takes its first backup straight away instead of waiting for the next slot.
    """
    slot = now.replace(hour=settings.BACKUP_SCHEDULE_HOUR_UTC, minute=0, second=0, microsecond=0)
    return slot if now >= slot else slot - timedelta(days=1)


def next_run(now=None):
    now = now or timezone.now()
    return slot_start(now) + timedelta(days=1)


def due(now):
    slot = slot_start(now)
    attempts = BackupRun.objects.filter(kind=BackupRun.Kind.DATABASE, started_at__gte=slot)
    if attempts.filter(status__in=[BackupRun.Status.SUCCESS, BackupRun.Status.RUNNING]).exists():
        return False
    if attempts.count() >= MAX_ATTEMPTS_PER_DAY:
        return False
    last = attempts.order_by('-started_at').first()
    return last is None or now - last.started_at >= RETRY_AFTER


def tick(now=None):
    """One scheduling decision. Returns True if it started a backup."""
    now = now or timezone.now()
    service.reap_stuck_runs(now)
    if not due(now):
        return False
    if not cache.add(LOCK_KEY, '1', LOCK_SECONDS):
        return False  # another worker is handling it
    if not due(now):
        return False
    service.spawn_backup_process('scheduled')
    logger.info('Scheduled backup started')
    return True


def _loop():
    time.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            tick()
        except Exception:
            logger.exception('Backup scheduler tick failed')
        finally:
            connection.close()
        time.sleep(CHECK_EVERY_SECONDS)


def running_under_gunicorn():
    return os.path.basename(sys.argv[0]).startswith('gunicorn')


def start():
    thread = threading.Thread(target=_loop, name='backup-scheduler', daemon=True)
    thread.start()
    return thread
