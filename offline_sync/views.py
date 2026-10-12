import hashlib
import json
import logging
import re

from django.db import transaction
from django.db.models import F
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from audit import context as audit_context
from core.permissions import INTERNAL_ROLES

from .models import OfflineOperation
from .registry import Conflict, get_spec, public_catalog
from .replay import Outcome, run_view

logger = logging.getLogger(__name__)

ENVELOPE_FIELD = '_offline'
# What the browser mints: a UUID with or without dashes. Anything else is not
# ours, and an unbounded id would be an unbounded index key.
OP_ID_RE = re.compile(r'^[A-Za-z0-9-]{16,64}$')


def _json(http_status, **body):
    response = JsonResponse(body, status=http_status)
    response['Cache-Control'] = 'no-store'
    return response


def _unauthenticated():
    return _json(401, status='unauthenticated',
                 message='Your session has ended. Sign in to continue syncing.')


def _fingerprint(post, files):
    digest = hashlib.sha256()
    for key in sorted(post):
        for value in post.getlist(key):
            digest.update(f'{key}={value}\x1f'.encode('utf-8', 'replace'))
    for key in sorted(files):
        for upload in files.getlist(key):
            digest.update(f'{key}:{upload.name}:{upload.size}\x1f'.encode('utf-8', 'replace'))
    return digest.hexdigest()


@never_cache
@require_GET
def ping(request):
    """Is the server reachable, and is this browser still signed in?

    `navigator.onLine` only says a network interface exists; this says the app
    itself answered. It also hands the page a current CSRF token and the
    identity the queue belongs to, so a stale cached page cannot sync a change
    as someone else.
    """
    authenticated = request.user.is_authenticated and request.user.role in INTERNAL_ROLES
    body = {
        'ok': True,
        'authenticated': bool(authenticated),
        'server_time': timezone.now().isoformat(),
    }
    if authenticated:
        body.update(
            user_id=request.user.pk,
            role=request.user.role,
            csrf=get_token(request),
            operations=public_catalog(),
        )
    return _json(200, **body)


def _result_payload(entry, **extra):
    return {
        'client_op_id': entry.client_op_id,
        'status': entry.status.lower(),
        'message': entry.message,
        'result': entry.result or {},
        **extra,
    }


@never_cache
@require_POST
def sync(request):
    """Apply ONE queued change. The client sends them in order, one per call.

    HTTP 200 always carries a definitive server answer in `status`:
      applied   the change is saved (or already was -- `duplicate: true`)
      rejected  the server refused it; nothing changed; it can be retried
      conflict  the data moved on since it was queued; nothing changed
    Anything else (401/403/400, 5xx, no response at all) means "not applied";
    the client keeps the change queued and shows it as not synced.
    """
    user = request.user
    if not user.is_authenticated:
        return _unauthenticated()
    if user.role not in INTERNAL_ROLES:
        return _json(403, status='forbidden', message='This account cannot sync offline changes.')

    try:
        envelope = json.loads(request.POST.get(ENVELOPE_FIELD, ''))
        if not isinstance(envelope, dict):
            raise ValueError
    except ValueError:
        return _json(400, status='invalid', message='The queued change was not readable.')

    op_id = str(envelope.get('client_op_id', ''))
    op_type = str(envelope.get('op_type', ''))
    if not OP_ID_RE.match(op_id):
        return _json(400, status='invalid', message='The queued change has no valid id.')

    # A queue belongs to whoever created it. If a different person has signed
    # in on this browser since, their session must not apply it as themselves.
    if str(envelope.get('client_user_id', '')) != str(user.pk):
        return _json(
            409, status='wrong_user', client_op_id=op_id,
            message='This change was queued by a different account on this device.',
        )

    spec = get_spec(op_type)
    if spec is None:
        return _json(
            400, status='online_only', client_op_id=op_id,
            message='This kind of change cannot be saved offline. Make it while online.',
        )
    if user.role not in spec.roles:
        return _json(
            403, status='forbidden', client_op_id=op_id,
            message=f'{spec.label} cannot be saved offline for your role. Make it while online.',
        )

    target_pk = envelope.get('target_pk')
    if spec.needs_target:
        if not str(target_pk or '').isdigit():
            return _json(400, status='invalid', client_op_id=op_id,
                         message='The queued change does not say which record it is for.')
        target_pk = int(target_pk)
    else:
        target_pk = None

    meta = envelope.get('meta') if isinstance(envelope.get('meta'), dict) else {}
    queued_at = parse_datetime(str(envelope.get('queued_at', ''))) if envelope.get('queued_at') else None

    # The form as the user filled it, minus our envelope.
    post = request.POST.copy()
    post.pop(ENVELOPE_FIELD, None)
    if spec.key_field:
        # The view already de-duplicates on this token. Making it the ledger
        # id ties the two layers together and rules out token collisions.
        post[spec.key_field] = op_id
    files = request.FILES
    fingerprint = _fingerprint(post, files)

    entry, _created = OfflineOperation.objects.get_or_create(
        user=user, client_op_id=op_id,
        defaults={
            'op_type': op_type, 'target_pk': target_pk,
            'fingerprint': fingerprint, 'queued_at': queued_at,
        },
    )
    if entry.fingerprint != fingerprint or entry.op_type != op_type:
        return _json(
            409, status='id_reused', client_op_id=op_id,
            message='This operation id was already used for different data.',
        )
    if entry.status == OfflineOperation.Status.APPLIED:
        return _json(200, **_result_payload(entry, duplicate=True))

    OfflineOperation.objects.filter(pk=entry.pk).update(attempts=F('attempts') + 1)

    started_at = timezone.now()
    outcome = None
    try:
        with transaction.atomic():
            locked = OfflineOperation.objects.select_for_update().get(pk=entry.pk)
            if locked.status == OfflineOperation.Status.APPLIED:
                # A concurrent request finished it while this one waited.
                return _json(200, **_result_payload(locked, duplicate=True))

            audit_context.set_action_label(f'Synced from offline queue: {spec.label}')

            conflict = spec.precheck(user, post, target_pk, meta) if spec.precheck else None
            if isinstance(conflict, Conflict):
                outcome = Outcome('CONFLICT', conflict.message, current=conflict.current)
            else:
                outcome = run_view(request, spec, post, files, target_pk, op_id, started_at)

            if outcome.status == 'APPLIED':
                locked.status = OfflineOperation.Status.APPLIED
                locked.message = outcome.message[:1000]
                locked.result = outcome.result
                locked.applied_at = timezone.now()
                locked.save(update_fields=['status', 'message', 'result', 'applied_at'])
            else:
                # Refused means refused: undo anything the view wrote before
                # it noticed, so a rejected change never half-applies.
                transaction.set_rollback(True)
    except Exception:
        logger.exception('Offline sync failed for %s (%s)', op_id, op_type)
        return _json(
            500, status='error', client_op_id=op_id,
            message='The server could not process this change yet. It stays queued.',
        )

    if outcome.status != 'APPLIED':
        OfflineOperation.objects.filter(pk=entry.pk).update(
            status=OfflineOperation.Status[outcome.status],
            message=outcome.message[:1000], result={},
        )
        entry.refresh_from_db()
        extra = {'current': outcome.current} if outcome.current else {}
        return _json(200, **_result_payload(entry, **extra))

    entry.refresh_from_db()
    return _json(200, **_result_payload(entry, duplicate=False))
