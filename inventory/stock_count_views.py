"""Stock Count (cycle count) screens.

Thin on purpose: parse the request, call inventory.stock_count, show the
result. Every rule — snapshot, maker/checker, drift, negative stock — lives in
that module. The module/action gate (Stock Counts View/Add/Edit/Delete) is
applied before these run by accounts.middleware.ModulePermissionMiddleware;
the role, branch and location checks here are the authoritative ones on top.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from branches.models import Branch
from core.limits import MAX_BARCODE_LENGTH, MAX_NOTE_LENGTH, parse_quantity, parse_text
from core.module_permissions import (
    ADD, DELETE, STOCK_COUNT, user_can_perform_module_action,
)
from core.pagination import paginate
from core.refdata import cached_categories
from core.scoping import branch_for_user, scope_queryset

from .models import Batch, Product, ProductCategory, StockCount
from .stock_count import (
    INTERNAL_ROLES, StockCountAlreadyApproved, StockCountError,
    add_line, approval_block_reason, approve_stock_count, assess_lines,
    can_access_count, can_view_location, cancel_stock_count, maker_ids,
    recount_stock_count, record_scan, reject_stock_count, save_counts,
    start_stock_count, submit_stock_count,
)

Status = StockCount.Status
LOCATIONS = (Batch.Location.FLOOR, Batch.Location.WAREHOUSE)

STATUS_TONES = {
    Status.DRAFT: 'neutral',
    Status.COUNTING: 'info',
    Status.SUBMITTED: 'warn',
    Status.APPROVED: 'ok',
    Status.REJECTED: 'danger',
    Status.CANCELLED: 'neutral',
}

# The list's status tabs. Counts come from the whole branch-scoped set, only
# the table is narrowed by the tab.
STATUS_TABS = (
    ('open', 'In progress', (Status.DRAFT, Status.COUNTING)),
    ('submitted', 'Awaiting approval', (Status.SUBMITTED,)),
    ('approved', 'Approved', (Status.APPROVED,)),
    ('closed', 'Rejected / Cancelled', (Status.REJECTED, Status.CANCELLED)),
)


def _internal_only(request):
    # Suppliers and customers get the styled 403 page, never a redirect that
    # would hint the page exists for them.
    if request.user.role not in INTERNAL_ROLES:
        raise PermissionDenied('Stock counts are for store staff.')


def _visible_counts(user):
    qs = scope_queryset(StockCount.objects.all(), user)
    locations = [loc for loc in LOCATIONS if can_view_location(user, loc)]
    return qs.filter(location_type__in=locations)


def _get_count(request, pk):
    """The count, if this user's branch and location access cover it. A count
    from another branch is a 404, the same as a missing one."""
    _internal_only(request)
    count = get_object_or_404(
        _visible_counts(request.user).select_related(
            'branch', 'category', 'created_by', 'submitted_by', 'approved_by',
        ),
        pk=pk,
    )
    if not can_access_count(request.user, count):  # defensive; the qs already scopes
        raise PermissionDenied
    return count


def _who(user):
    if user is None:
        return '—'
    return user.get_full_name() or user.username


# ── list ───────────────────────────────────────────────────────────────────

@login_required(login_url='accounts:login')
def stock_count_list(request):
    _internal_only(request)
    user = request.user
    base = _visible_counts(user).select_related('branch', 'category', 'created_by')

    is_owner = user.role == 'OWNER'
    branch_filter = request.GET.get('branch', '') if is_owner else ''
    if branch_filter.isdigit():
        base = base.filter(branch_id=int(branch_filter))
    location_filter = request.GET.get('location', '')
    if location_filter in LOCATIONS:
        base = base.filter(location_type=location_filter)

    tab_counts = {
        key: base.filter(status__in=statuses).count() for key, _label, statuses in STATUS_TABS
    }
    active_tab = request.GET.get('status', '')
    rows = base
    for key, _label, statuses in STATUS_TABS:
        if key == active_tab:
            rows = rows.filter(status__in=statuses)
    rows = rows.annotate(
        total_lines=Count('lines'),
        counted_lines=Count('lines', filter=Q(lines__counted_qty__isnull=False)),
    ).order_by('-created_at', '-pk')
    page = paginate(request, rows)
    for count in page.object_list:
        count.tone = STATUS_TONES.get(count.status, 'neutral')

    can_start = (
        user_can_perform_module_action(user, STOCK_COUNT, ADD)
        and any(can_view_location(user, loc) for loc in LOCATIONS)
    )
    filter_branch = None
    if branch_filter.isdigit():
        filter_branch = Branch.objects.filter(pk=int(branch_filter)).first()
    return render(request, 'inventory/stock_count_list.html', {
        'active_page': 'stock_count',
        'page_obj': page,
        'counts': page.object_list,
        'tabs': [
            {'key': key, 'label': label, 'count': tab_counts[key], 'active': key == active_tab}
            for key, label, _s in STATUS_TABS
        ],
        'active_tab': active_tab,
        'all_count': sum(tab_counts.values()),
        'is_owner': is_owner,
        'branches': Branch.objects.order_by('branch_name') if is_owner else [],
        'branch_filter': branch_filter,
        'filter_branch': filter_branch,
        'user_branch': branch_for_user(user),
        'location_filter': location_filter,
        'locations': [loc for loc in LOCATIONS if can_view_location(user, loc)],
        'can_start': can_start,
    })


# ── start ──────────────────────────────────────────────────────────────────

@login_required(login_url='accounts:login')
def stock_count_start(request):
    _internal_only(request)
    user = request.user
    is_owner = user.role == 'OWNER'
    locations = [loc for loc in LOCATIONS if can_view_location(user, loc)]
    if not locations:
        raise PermissionDenied('You need Floor Stock or Warehouse Stock access to count stock.')
    pinned_branch = None if is_owner else branch_for_user(user)
    context = {
        'active_page': 'stock_count',
        'is_owner': is_owner,
        'branches': Branch.objects.order_by('branch_name') if is_owner else [],
        'pinned_branch': pinned_branch,
        'locations': locations,
        'categories': cached_categories(),
        'form': {},
    }
    if not is_owner and pinned_branch is None:
        messages.error(
            request,
            'No branch is assigned to your account, so you cannot start a count. '
            'Ask the Owner to assign you to a branch.',
        )
        return redirect('stock_count_list')

    if request.method != 'POST':
        context['form'] = {'location': request.GET.get('location', '')}
        return render(request, 'inventory/stock_count_start.html', context)

    errors = []
    # Only the Owner names the branch; everyone else is pinned to their own.
    if is_owner:
        raw_branch = request.POST.get('branch_id', '').strip()
        branch = Branch.objects.filter(pk=raw_branch).first() if raw_branch.isdigit() else None
        if branch is None:
            errors.append('Choose a branch.')
    else:
        branch = pinned_branch
    location = request.POST.get('location', '')
    if location not in locations:
        errors.append('Choose Floor or Warehouse stock.')
    raw_category = request.POST.get('category', '').strip()
    category = None
    if raw_category:
        category = (
            ProductCategory.objects.filter(pk=raw_category).first()
            if raw_category.isdigit() else None
        )
        if category is None:
            errors.append('Choose a valid category, or leave it on All categories.')
    notes, notes_error = parse_text(request.POST.get('notes'), 'Notes', max_length=MAX_NOTE_LENGTH)
    if notes_error:
        errors.append(notes_error)

    if not errors:
        try:
            count = start_stock_count(
                branch=branch, location_type=location, actor=user,
                category=category, notes=notes,
            )
        except StockCountError as exc:
            errors.append(str(exc))
        else:
            messages.success(
                request,
                f'{count.count_number} started: {count.lines.count()} items to count.',
            )
            return redirect('stock_count_count', pk=count.pk)

    for error in errors:
        messages.error(request, error)
    context['form'] = request.POST
    return render(request, 'inventory/stock_count_start.html', context)


# ── counting ───────────────────────────────────────────────────────────────

def _posted_counts(request, line_ids):
    """{line_id: (original, new)} from counted_<id> / orig_<id>, plus errors.
    A blank field means "not counted"."""
    changes, errors = {}, []
    for line_id in line_ids:
        raw = request.POST.get(f'counted_{line_id}')
        if raw is None:
            continue
        new, error = parse_quantity(raw, 'Counted quantity', required=False)
        if error:
            errors.append(error)
            continue
        original, _ = parse_quantity(request.POST.get(f'orig_{line_id}'), 'x', required=False)
        changes[line_id] = (original, new)
    return changes, errors


@login_required(login_url='accounts:login')
def stock_count_count(request, pk):
    count = _get_count(request, pk)
    if not count.is_countable:
        messages.info(request, f'{count.count_number} is {count.get_status_display()} — counting is closed.')
        return redirect('stock_count_detail', pk=count.pk)

    lines = list(count.lines.select_related('product').order_by('product__product_name', 'pk'))

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        changes, errors = _posted_counts(request, [line.pk for line in lines])
        add_product = None
        if action == 'add':
            raw = request.POST.get('add_product_id', '').strip()
            add_product = Product.objects.filter(pk=raw).first() if raw.isdigit() else None
            if add_product is None:
                errors.append('Choose the product to add.')
        if errors:
            for error in errors:
                messages.error(request, error)
            return _render_count(request, count, lines, posted=request.POST)
        try:
            changed = save_counts(count_id=count.pk, actor=request.user, changes=changes)
            if action == 'add':
                line, created = add_line(count_id=count.pk, actor=request.user, product=add_product)
                if created:
                    messages.success(request, f'{add_product.product_name} added to the count.')
                else:
                    messages.info(request, f'{add_product.product_name} is already in this count.')
                return redirect(f"{reverse('stock_count_count', args=[count.pk])}#line-{line.pk}")
            if action == 'submit':
                count = submit_stock_count(
                    count_id=count.pk, actor=request.user,
                    uncounted_policy=request.POST.get('uncounted_policy', ''),
                )
                messages.success(request, f'{count.count_number} submitted for approval.')
                if not approval_block_reason(request.user, count):
                    # The Owner (or a Manager reviewing someone else's
                    # count) goes straight to the review screen.
                    return redirect('stock_count_review', pk=count.pk)
                return redirect('stock_count_detail', pk=count.pk)
        except StockCountError as exc:
            messages.error(request, str(exc))
            count.refresh_from_db()
            if not count.is_countable:
                return redirect('stock_count_detail', pk=count.pk)
            return _render_count(request, count, lines, posted=request.POST)
        messages.success(
            request,
            f'Progress saved ({changed} item{"s" if changed != 1 else ""} updated).'
            if changed else 'Nothing changed since the last save.',
        )
        return redirect('stock_count_count', pk=count.pk)

    return _render_count(request, count, lines)


def _render_count(request, count, lines, posted=None):
    in_count = {line.product_id for line in lines}
    for line in lines:
        line.posted_value = posted.get(f'counted_{line.pk}') if posted is not None else None
    counted = sum(1 for line in lines if line.counted_qty is not None)
    return render(request, 'inventory/stock_count_count.html', {
        'active_page': 'stock_count',
        'count': count,
        'lines': lines,
        'counted_total': counted,
        'line_total': len(lines),
        'addable_products': (
            Product.objects.exclude(pk__in=in_count).order_by('product_name')
            .only('pk', 'product_name', 'sku_code')
        ),
        'can_cancel': (
            user_can_perform_module_action(request.user, STOCK_COUNT, DELETE)
            and (count.created_by_id == request.user.pk or request.user.role in ('OWNER', 'BRANCH_MANAGER'))
        ),
        'policies': StockCount.UncountedPolicy.choices,
        'posted_policy': posted.get('uncounted_policy') if posted is not None else '',
    })


@login_required(login_url='accounts:login')
@require_POST
def stock_count_scan(request, pk):
    """AJAX: resolve one scanned code against this count and log it. POST
    because every call writes a StockCountScanLog row (see record_scan)."""
    if request.user.role not in INTERNAL_ROLES:
        return JsonResponse({'ok': False, 'detail': 'Access denied.'}, status=403)
    count = get_object_or_404(_visible_counts(request.user), pk=pk)
    if not count.is_countable:
        return JsonResponse(
            {'ok': False, 'detail': f'{count.count_number} is no longer being counted.'},
            status=400,
        )
    code = (request.POST.get('code') or '').strip()
    if not code:
        return JsonResponse({'ok': False, 'detail': 'No code supplied.'}, status=400)
    if len(code) > MAX_BARCODE_LENGTH:
        return JsonResponse(
            {'ok': False, 'detail': f'A code cannot be longer than {MAX_BARCODE_LENGTH} characters.'},
            status=400,
        )
    return JsonResponse({'ok': True, **record_scan(count=count, actor=request.user, code=code)})


# ── review / approve ───────────────────────────────────────────────────────

def _review_summary(assessed):
    applicable = [a for a in assessed if a.state == 'variance']
    return {
        'items': len(assessed),
        'counted': sum(1 for a in assessed if a.counted is not None),
        'variances': len(applicable),
        'recount': sum(1 for a in assessed if a.state in ('recount', 'negative')),
        'units_net': sum(a.variance for a in applicable),
        'value_gain': sum((a.value_impact for a in applicable if a.variance > 0), Decimal('0.00')),
        'value_loss': sum((a.value_impact for a in applicable if a.variance < 0), Decimal('0.00')),
        'value_net': sum((a.value_impact for a in applicable), Decimal('0.00')),
    }


@login_required(login_url='accounts:login')
def stock_count_review(request, pk):
    count = _get_count(request, pk)
    if request.method == 'POST':
        accepted = {int(v) for v in request.POST.getlist('accept') if v.isdigit()}
        reasons = {}
        for key, value in request.POST.items():
            if key.startswith('reason_') and key[7:].isdigit():
                reasons[int(key[7:])] = value
        try:
            count, applied = approve_stock_count(
                count_id=count.pk, actor=request.user,
                expected_status=request.POST.get('expected_status', ''),
                accepted=accepted, reasons=reasons,
                default_reason=request.POST.get('default_reason', ''),
            )
        except StockCountAlreadyApproved as exc:
            messages.info(request, str(exc))
            return redirect('stock_count_detail', pk=count.pk)
        except StockCountError as exc:
            messages.error(request, str(exc))
            count.refresh_from_db()
            if count.status != Status.SUBMITTED:
                return redirect('stock_count_detail', pk=count.pk)
            return _render_review(request, count, posted=request.POST)
        messages.success(
            request,
            f'{count.count_number} approved: {applied} variance'
            f'{"s" if applied != 1 else ""} applied to {count.location_type.lower()} stock.',
        )
        return redirect('stock_count_detail', pk=count.pk)

    if count.status != Status.SUBMITTED:
        messages.info(request, f'{count.count_number} is {count.get_status_display()}, not waiting for approval.')
        return redirect('stock_count_detail', pk=count.pk)
    return _render_review(request, count)


def _render_review(request, count, posted=None):
    assessed = assess_lines(count)
    accepted = (
        {v for v in posted.getlist('accept')} if posted is not None else None
    )
    for a in assessed:
        key = str(a.line.pk)
        a.checked = (a.state == 'variance') if accepted is None else key in accepted
        a.posted_reason = posted.get(f'reason_{key}', '') if posted is not None else ''
    block = approval_block_reason(request.user, count)
    # Lines with a variance first, then the ones needing a recount, then the
    # rest — the approver's attention in that order.
    order = {'variance': 0, 'negative': 1, 'recount': 1, 'match': 2, 'uncounted': 3}
    assessed.sort(key=lambda a: (order[a.state], a.line.product.product_name))
    return render(request, 'inventory/stock_count_review.html', {
        'active_page': 'stock_count',
        'count': count,
        'assessed': assessed,
        'summary': _review_summary(assessed),
        'block_reason': block,
        'can_review': not block,
        'posted_default_reason': posted.get('default_reason', '') if posted is not None else '',
    })


@login_required(login_url='accounts:login')
@require_POST
def stock_count_reject(request, pk):
    count = _get_count(request, pk)
    try:
        reject_stock_count(
            count_id=count.pk, actor=request.user,
            expected_status=request.POST.get('expected_status', ''),
            reason=request.POST.get('reason', ''),
        )
    except StockCountError as exc:
        messages.error(request, str(exc))
        return redirect('stock_count_review' if count.status == Status.SUBMITTED else 'stock_count_detail', pk=count.pk)
    messages.success(request, f'{count.count_number} rejected. No stock was changed.')
    return redirect('stock_count_detail', pk=count.pk)


@login_required(login_url='accounts:login')
@require_POST
def stock_count_recount(request, pk):
    count = _get_count(request, pk)
    line_ids = {int(v) for v in request.POST.getlist('recount') if v.isdigit()}
    note, note_error = parse_text(request.POST.get('note'), 'Note', max_length=MAX_NOTE_LENGTH)
    if note_error:
        messages.error(request, note_error)
        return redirect('stock_count_review', pk=count.pk)
    try:
        count, n = recount_stock_count(
            count_id=count.pk, actor=request.user,
            expected_status=request.POST.get('expected_status', ''),
            line_ids=line_ids, note=note,
        )
    except StockCountError as exc:
        messages.error(request, str(exc))
        return redirect('stock_count_review' if count.status == Status.SUBMITTED else 'stock_count_detail', pk=count.pk)
    messages.success(
        request,
        f'{n} item{"s" if n != 1 else ""} sent back for a recount. {count.count_number} is open for counting again.',
    )
    return redirect('stock_count_detail', pk=count.pk)


@login_required(login_url='accounts:login')
@require_POST
def stock_count_cancel(request, pk):
    count = _get_count(request, pk)
    try:
        cancel_stock_count(
            count_id=count.pk, actor=request.user,
            reason=request.POST.get('reason', ''),
            expected_status=request.POST.get('expected_status') or None,
        )
    except StockCountError as exc:
        messages.error(request, str(exc))
        return redirect('stock_count_detail', pk=count.pk)
    messages.success(request, f'{count.count_number} cancelled. No stock was changed.')
    return redirect('stock_count_list')


# ── detail ─────────────────────────────────────────────────────────────────

@login_required(login_url='accounts:login')
def stock_count_detail(request, pk):
    count = _get_count(request, pk)
    lines = list(
        count.lines.select_related('product', 'counted_by', 'movement')
        .order_by('product__product_name', 'pk')
    )
    for line in lines:
        line.shown_variance = (
            line.applied_change if line.decision else line.variance
        )
    history = [
        {
            'from_label': event.from_status or 'Created',
            'label': event.get_to_status_display(),
            'tone': STATUS_TONES.get(event.to_status, 'neutral'),
            'actor': _who(event.actor),
            'at': event.created_at,
            'note': event.note,
        }
        for event in count.events.select_related('actor')
    ]
    scans = list(count.scan_logs.select_related('matched_product', 'scanned_by')[:50])
    user = request.user
    applied = [line for line in lines if line.decision == line.Decision.APPLIED]
    return render(request, 'inventory/stock_count_detail.html', {
        'active_page': 'stock_count',
        'count': count,
        'status_tone': STATUS_TONES.get(count.status, 'neutral'),
        'lines': lines,
        'history': history,
        'scans': scans,
        'scan_total': count.scan_logs.count(),
        'counted_total': sum(1 for line in lines if line.counted_qty is not None),
        'applied_total': len(applied),
        'applied_units': sum(line.applied_change or 0 for line in applied),
        'can_count': (
            count.is_countable and user_can_perform_module_action(user, STOCK_COUNT, ADD)
        ),
        'can_review': count.status == Status.SUBMITTED and not approval_block_reason(user, count),
        'review_block': approval_block_reason(user, count) if count.status == Status.SUBMITTED else '',
        'can_cancel': (
            count.is_open
            and user_can_perform_module_action(user, STOCK_COUNT, DELETE)
            and (count.created_by_id == user.pk or user.role in ('OWNER', 'BRANCH_MANAGER'))
        ),
        'created_by_name': _who(count.created_by),
        'submitted_by_name': _who(count.submitted_by),
        'approved_by_name': _who(count.approved_by),
        'is_maker': user.pk in maker_ids(count),
    })
