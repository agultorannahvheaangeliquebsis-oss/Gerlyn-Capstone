from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render

from accounts.models import User
from branches.models import Branch
from core.querystrings import list_filter_querystring
from core.timebounds import local_range_bounds, range_from_request

from .models import AuditLog
from .registry import REGISTRY, entity_choices, label_for

PAGE_SIZE = 50


def can_view_audit_log(user):
    """Owner accounts (the project's admin accounts are Owners) and Django
    superusers. Not a module: there is nothing to grant per employee."""
    return user.is_authenticated and (user.role == User.Role.OWNER or user.is_superuser)


def _actor_label(actor_id, role, users):
    if actor_id is None:
        return 'System' if not role else role
    user = users.get(actor_id)
    if user is None:
        return f'User #{actor_id} (removed)'
    if user.deleted_at:
        return f'User #{actor_id} (personal data erased)'
    return user.username


def _group_key(request_id, pk):
    """One request's rows share request_id and belong to one group. A
    system write (no request — the nightly sweep, dispatch_notifications)
    has request_id='' for every such row, so grouping on that verbatim
    would merge every unrelated system write into one bucket; each becomes
    its own singleton group instead, keyed off its own row. The 'row_'
    prefix can never collide with a real request_id (always a bare
    uuid4().hex, never containing an underscore)."""
    return request_id or f'row_{pk}'


@login_required(login_url='accounts:login')
def audit_log_list(request):
    if not can_view_audit_log(request.user):
        raise PermissionDenied('Only the Owner can view the audit log.')

    params = request.GET
    q = params.get('q', '').strip()
    entity = params.get('entity', '')
    action = params.get('action', '')
    # KATRINA-AUDIT-001: an Owner sees every branch here by design, but had
    # no way to narrow the list to one — every row still carried its own
    # branch_id, just nothing filtered on it.
    branch_filter = params.get('branch', '')
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''
    # The shared back-office range control (_daterange.html), same as the
    # Dashboard and Reports; `range_start` is None for "All time".
    range_start, range_end, active_range = range_from_request(request)

    logs = AuditLog.objects.all()
    if q:
        # The main search also matches the actor's username, so there is no
        # separate actor box to fill in.
        actor_ids = User.objects.filter(username__icontains=q).values_list('pk', flat=True)
        logs = logs.filter(
            Q(summary__icontains=q) | Q(record_repr__icontains=q) | Q(record_id=q)
            | Q(actor_id__in=list(actor_ids))
        )
    if entity in REGISTRY:
        logs = logs.filter(entity=entity)
    if action in AuditLog.Action.values:
        logs = logs.filter(action=action)
    if branch_filter:
        logs = logs.filter(branch_id=branch_filter)
    # local_range_bounds is inclusive of the whole end day.
    range_dt_start, range_dt_end = local_range_bounds(range_start or range_end, range_end)
    if range_start:
        logs = logs.filter(timestamp__gte=range_dt_start)
    logs = logs.filter(timestamp__lt=range_dt_end)

    # One action (an HTTP request) can write several rows — grouping turns
    # matching ROWS into matching ACTIONS before paginating. Matches are
    # already newest-first (the model's own Meta.ordering), so walking them
    # once and keeping only the first occurrence of each group key yields
    # groups ordered by their own most recent matching activity.
    group_keys = []
    seen = set()
    for log_id, request_id in logs.values_list('id', 'request_id'):
        key = _group_key(request_id, log_id)
        if key not in seen:
            seen.add(key)
            group_keys.append(key)

    page = Paginator(group_keys, PAGE_SIZE).get_page(params.get('page'))
    page_keys = list(page.object_list)

    # Deliberately re-queried WITHOUT the filters above: a group must show
    # in full even when only one of its rows matched what was searched for
    # — the whole action, not just the slice that happened to match.
    real_request_ids = [k for k in page_keys if not k.startswith('row_')]
    singleton_pks = [k[4:] for k in page_keys if k.startswith('row_')]
    group_logs = list(
        AuditLog.objects.filter(
            Q(request_id__in=real_request_ids) | Q(pk__in=singleton_pks)
        ).order_by('timestamp', 'id')
    )

    buckets = {}
    for log in group_logs:
        buckets.setdefault(_group_key(log.request_id, log.pk), []).append(log)

    users = User.objects.in_bulk({l.actor_id for l in group_logs if l.actor_id is not None})
    branch_names = Branch.objects.in_bulk({l.branch_id for l in group_logs if l.branch_id is not None})

    groups = []
    for key in page_keys:
        members = buckets.get(key, [])
        if not members:
            continue
        primary = members[0]
        label = next((m.action_label for m in members if m.action_label), '')
        actions = {m.action for m in members}
        groups.append({
            'key': key,
            'primary': primary,
            'actor': _actor_label(primary.actor_id, primary.actor_role, users),
            'entity_label': label_for(primary.entity),
            'branch': branch_names[primary.branch_id].branch_name if primary.branch_id in branch_names else '',
            'summary_text': label or primary.summary,
            'action_display': actions.pop() if len(actions) == 1 else 'mixed',
            'process_count': len(members),
        })

    filters = {'q': q, 'entity': entity, 'action': action, 'branch': branch_filter}
    range_params = {
        'range': active_range,
        'date_from': range_start.isoformat() if range_start else '',
        'date_to': range_end.isoformat(),
    }
    return render(request, 'audit/audit_log_list.html', {
        'groups': groups,
        'page': page,
        'filters': filters,
        'branches': Branch.objects.all().order_by('branch_name'),
        'branch_filter': branch_filter,
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        # Filters only: drives the "Clear" link and the empty-state wording.
        'querystring': list_filter_querystring(**filters),
        # Filters plus the range, so a pagination link keeps both.
        'page_querystring': list_filter_querystring(**filters, **range_params),
        # _daterange.html: a preset click must not drop the search/filters.
        'active_range': active_range,
        'range_start': range_start or range_end,
        'range_end': range_end,
        'daterange_carry': [(k, v) for k, v in filters.items()],
        'entity_choices': entity_choices(),
        'action_choices': AuditLog.Action.choices,
    })


@login_required(login_url='accounts:login')
def audit_action_detail(request, group_key):
    """Every row one action (one HTTP request, or one unrequested system
    write) produced — what the grouped list's eye icon opens."""
    if not can_view_audit_log(request.user):
        raise PermissionDenied('Only the Owner can view the audit log.')

    if group_key.startswith('row_'):
        pk_part = group_key[4:]
        if not pk_part.isdigit():
            raise Http404('No such action.')
        logs = list(AuditLog.objects.filter(pk=pk_part, request_id=''))
    else:
        logs = list(
            AuditLog.objects.filter(request_id=group_key).order_by('timestamp', 'id')
        )
    if not logs:
        raise Http404('No such action.')

    primary = logs[0]
    label = next((l.action_label for l in logs if l.action_label), '')
    actions = {l.action for l in logs}

    users = User.objects.in_bulk({l.actor_id for l in logs if l.actor_id is not None})
    branches = Branch.objects.in_bulk({l.branch_id for l in logs if l.branch_id is not None})

    rows = [{
        'log': l,
        'actor': _actor_label(l.actor_id, l.actor_role, users),
        'entity_label': label_for(l.entity),
        'branch': branches[l.branch_id].branch_name if l.branch_id in branches else '',
        'changes': _changes(l),
    } for l in logs]

    return render(request, 'audit/audit_action_detail.html', {
        'summary_text': label or primary.summary,
        'actor': _actor_label(primary.actor_id, primary.actor_role, users),
        'branch': branches[primary.branch_id].branch_name if primary.branch_id in branches else '',
        'action_display': actions.pop() if len(actions) == 1 else 'mixed',
        'started_at': logs[0].timestamp,
        'ended_at': logs[-1].timestamp,
        'ip_address': primary.ip_address,
        'rows': rows,
    })


def _changes(log):
    """(field, before, after) rows for the expandable detail."""
    before, after = log.before or {}, log.after or {}
    return [(key, before.get(key), after.get(key)) for key in sorted(set(before) | set(after))]
