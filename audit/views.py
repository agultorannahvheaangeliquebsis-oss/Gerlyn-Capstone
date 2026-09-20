from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from core.querystrings import list_filter_querystring

from .models import AuditLog
from .registry import REGISTRY, entity_choices, label_for

PAGE_SIZE = 50


def can_view_audit_log(user):
    """Owner accounts (the project's admin accounts are Owners) and Django
    superusers. Not a module: there is nothing to grant per employee."""
    return user.is_authenticated and (user.role == User.Role.OWNER or user.is_superuser)


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _day_start(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def _actor_label(actor_id, role, users):
    if actor_id is None:
        return 'System' if not role else role
    user = users.get(actor_id)
    if user is None:
        return f'User #{actor_id} (removed)'
    if user.deleted_at:
        return f'User #{actor_id} (personal data erased)'
    return user.username


@login_required(login_url='accounts:login')
def audit_log_list(request):
    if not can_view_audit_log(request.user):
        messages.error(request, 'Only the Owner can view the audit log.')
        return redirect('accounts:home')

    params = request.GET
    q = params.get('q', '').strip()
    entity = params.get('entity', '')
    action = params.get('action', '')
    actor = params.get('actor', '').strip()
    date_from = _parse_date(params.get('from'))
    date_to = _parse_date(params.get('to'))

    logs = AuditLog.objects.all()
    if q:
        logs = logs.filter(
            Q(summary__icontains=q) | Q(record_repr__icontains=q) | Q(record_id=q)
        )
    if entity in REGISTRY:
        logs = logs.filter(entity=entity)
    if action in AuditLog.Action.values:
        logs = logs.filter(action=action)
    if actor:
        ids = list(User.objects.filter(username__icontains=actor).values_list('pk', flat=True))
        logs = logs.filter(actor_id__in=ids)
    if date_from:
        logs = logs.filter(timestamp__gte=_day_start(date_from))
    if date_to:
        # Inclusive of the whole "to" day.
        logs = logs.filter(timestamp__lt=_day_start(date_to) + timedelta(days=1))

    page = Paginator(logs, PAGE_SIZE).get_page(params.get('page'))

    users = User.objects.in_bulk({r.actor_id for r in page if r.actor_id is not None})
    branches = Branch.objects.in_bulk({r.branch_id for r in page if r.branch_id is not None})
    rows = [{
        'log': r,
        'actor': _actor_label(r.actor_id, r.actor_role, users),
        'entity_label': label_for(r.entity),
        'branch': branches[r.branch_id].branch_name if r.branch_id in branches else '',
        'changes': _changes(r),
    } for r in page]

    filters = {
        'q': q, 'entity': entity, 'action': action, 'actor': actor,
        'from': params.get('from', ''), 'to': params.get('to', ''),
    }
    return render(request, 'audit/audit_log_list.html', {
        'rows': rows,
        'page': page,
        'filters': filters,
        'querystring': list_filter_querystring(**filters),
        'entity_choices': entity_choices(),
        'action_choices': AuditLog.Action.choices,
    })


def _changes(log):
    """(field, before, after) rows for the expandable detail."""
    before, after = log.before or {}, log.after or {}
    return [(key, before.get(key), after.get(key)) for key in sorted(set(before) | set(after))]
