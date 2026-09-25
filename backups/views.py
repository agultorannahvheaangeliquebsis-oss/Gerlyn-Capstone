import hmac
import logging
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts.models import User

from . import scheduler, service
from .models import BackupRun, RestoreTestRecord

logger = logging.getLogger(__name__)

RESTORE_TEST_OVERDUE_DAYS = 100


def can_manage_backups(user):
    """Owner accounts (the project's admin accounts are Owners) and Django superusers."""
    return user.is_authenticated and (user.role == User.Role.OWNER or user.is_superuser)


@csrf_exempt
@require_POST
def backup_trigger(request):
    """Start a backup on request from an external scheduler (optional).

    The web service normally schedules its own daily backup (backups/scheduler.py),
    so nothing needs to call this; it exists for anyone who prefers an external
    cron. Not session-based: it presents a shared secret (BACKUP_TRIGGER_TOKEN) as
    a Bearer token. With no token configured the endpoint doesn't exist. The
    backup runs in a detached process — a database dump can outlast a web
    request — and this returns immediately.
    """
    expected = settings.BACKUP_TRIGGER_TOKEN
    if not expected:
        raise Http404

    header = request.headers.get('Authorization', '')
    supplied = header[7:] if header.startswith('Bearer ') else ''
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        logger.warning('Backup trigger refused: bad token')
        return JsonResponse({'detail': 'Forbidden.'}, status=403)

    if service.active_run_exists():
        return JsonResponse({'detail': 'A backup is already running.'}, status=409)

    service.spawn_backup_process('cron')
    return JsonResponse({'detail': 'Backup started.'}, status=202)


@login_required(login_url='accounts:login')
def backup_status(request):
    if not can_manage_backups(request.user):
        messages.error(request, 'Only the Owner can view backups.')
        return redirect('accounts:home')

    now = timezone.now()
    tests = list(RestoreTestRecord.objects.select_related('recorded_by')[:5])
    latest = tests[0] if tests else None
    restore_overdue = latest is None or (now.date() - latest.tested_on) > timedelta(days=RESTORE_TEST_OVERDUE_DAYS)

    summaries = []
    for kind in (BackupRun.Kind.DATABASE, BackupRun.Kind.MEDIA):
        summaries.append({
            'label': kind.label,
            'last': service.last_success(kind),
            'overdue': service.is_overdue(kind, now),
        })

    # Failing checks first (False sorts before True; sorted() is stable), so the
    # top of the list answers "is anything broken?".
    checks = sorted(service.setup_checks(), key=lambda check: check[1])

    return render(request, 'backups/backup_status.html', {
        'checks': checks,
        'setup_issues': sum(1 for check in checks if not check[1]),
        'summaries': summaries,
        'runs': BackupRun.objects.all()[:40],
        'running': service.active_run_exists(now),
        'next_run': scheduler.next_run(now) if settings.BACKUP_SCHEDULER else None,
        'restore_tests': tests,
        'restore_overdue': restore_overdue,
        'stale_hours': settings.BACKUP_STALE_HOURS,
        'today': timezone.localdate().isoformat(),
        'outcomes': RestoreTestRecord.Outcome.choices,
    })


@login_required(login_url='accounts:login')
@require_POST
def backup_run_now(request):
    if not can_manage_backups(request.user):
        messages.error(request, 'Only the Owner can run a backup.')
        return redirect('accounts:home')

    if service.active_run_exists():
        messages.info(request, 'A backup is already running.')
    else:
        service.spawn_backup_process('manual')
        messages.success(request, 'Backup started. Refresh this page in a minute to see the result.')
    return redirect('backup_status')


@login_required(login_url='accounts:login')
@require_POST
def backup_record_restore_test(request):
    if not can_manage_backups(request.user):
        messages.error(request, 'Only the Owner can record a restore test.')
        return redirect('accounts:home')

    try:
        tested_on = date.fromisoformat(request.POST.get('tested_on', ''))
    except ValueError:
        tested_on = None
    backup_used = request.POST.get('backup_used', '').strip()
    outcome = request.POST.get('outcome', '')

    errors = []
    if tested_on is None:
        errors.append('Enter the date the restore test was done.')
    elif tested_on > timezone.localdate():
        errors.append('The restore test date cannot be in the future.')
    if not backup_used:
        errors.append('Say which backup was restored.')
    if outcome not in RestoreTestRecord.Outcome.values:
        errors.append('Choose whether the restore test passed or failed.')

    if errors:
        for error in errors:
            messages.error(request, error)
    else:
        RestoreTestRecord.objects.create(
            tested_on=tested_on, backup_used=backup_used[:200], outcome=outcome,
            notes=request.POST.get('notes', '').strip()[:2000], recorded_by=request.user,
        )
        messages.success(request, 'Restore test recorded.')
    return redirect('backup_status')
