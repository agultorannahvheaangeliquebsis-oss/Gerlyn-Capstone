"""Damage reports - the staff, Branch Manager and Owner pages.

File a report (any internal role, for their own branch), list and open reports
(scoped: Owner every branch, Manager their branch, Staff only their own), and -
for Owner and Branch Manager - review, approve, reject and resolve them. The
rules live in damage_reports.services; the customer side is orders.damage_views
and calls the same service.

Authorization is enforced here on the server, never by what the page shows:
a record outside the viewer's scope is a 404 (its existence is not confirmed),
and deciding a report needs both a Manager/Owner role and the Edit grant.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from audit import context as audit_context
from branches.models import Branch
from core.limits import MAX_DESCRIPTION_LENGTH, MAX_NOTE_LENGTH
from core.module_permissions import (
    ADD, DAMAGE_REPORTS, EDIT, PROCUREMENT, user_can_perform_module_action,
)
from core.pagination import paginate
from core.permissions import BRANCH_MANAGER, OWNER, STAFF, role_required
from core.querystrings import list_filter_querystring
from core.scoping import branch_for_user
from core.timebounds import local_range_bounds, range_from_request
from inventory.models import Batch
from procurement.access import procurements_visible_to
from procurement.models import Procurement
from sales.models import Refund

from . import services
from .errors import DamageReportError, DamageReportForbidden
from .models import DamageReport
from .photos import MAX_PHOTO_MB, MAX_PHOTOS

INTERNAL = (OWNER, BRANCH_MANAGER, STAFF)

# Labels that read better on the form than the model's own.
STAFF_DAMAGE_TYPES = [
    (t.value, t.label) for t in DamageReport.STAFF_DAMAGE_TYPES
]


def _can_decide(user):
    return (
        user.role in services.MANAGER_ROLES
        and user_can_perform_module_action(user, DAMAGE_REPORTS, EDIT)
    )


def _forbidden(exc):
    return PermissionDenied(str(exc))


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def _range_carry(active_range, range_start, range_end):
    """(name, value) pairs for the shared date-range control, so the status
    tabs and the topbar branch switcher keep the selected range. The same
    helper inventory.views and reports.views each carry privately."""
    carry = [('range', active_range)]
    if active_range == 'day':
        carry.append(('date', range_end.strftime('%Y-%m-%d')))
    elif active_range == 'custom':
        carry += [
            ('date_from', range_start.strftime('%Y-%m-%d') if range_start else ''),
            ('date_to', range_end.strftime('%Y-%m-%d') if range_end else ''),
        ]
    return carry


@role_required(*INTERNAL)
def damage_report_list(request):
    user = request.user
    visible = services.reports_visible_to(
        user, DamageReport.objects.select_related('branch', 'product', 'reported_by'),
    )
    # Open work is counted across every date: the range control below would
    # otherwise hide a report still waiting on a decision from last month.
    awaiting_total = visible.filter(status__in=DamageReport.OPEN_STATUSES).count()

    reports = visible
    search_query = request.GET.get('q', '').strip()
    if search_query:
        reports = reports.filter(
            Q(report_number__icontains=search_query)
            | Q(product_name__icontains=search_query)
            | Q(product__sku_code__icontains=search_query)
            | Q(sales_transaction__order_number__icontains=search_query)
            | Q(procurement__po_number__icontains=search_query)
            | Q(reported_by__username__icontains=search_query)
        )

    # Owner-only branch filter: a Manager/Staff account is already pinned to
    # its branch by reports_visible_to, so a selector would do nothing for it.
    branch_filter = request.GET.get('branch', '') if user.role == OWNER else ''
    if not branch_filter.isdigit():
        branch_filter = ''
    if branch_filter:
        reports = reports.filter(branch_id=branch_filter)

    product_options = list(
        visible.exclude(product__isnull=True)
        .values_list('product_id', 'product__product_name').distinct()
        .order_by('product__product_name')
    )
    product_filter = request.GET.get('product', '')
    if product_filter.isdigit():
        reports = reports.filter(product_id=product_filter)
    else:
        product_filter = ''

    # The shared back-office range control. created_at is a DateTimeField, so
    # local calendar days become aware bounds (core.timebounds).
    range_start, range_end, active_range = range_from_request(request)
    range_dt_start, range_dt_end = local_range_bounds(range_start or range_end, range_end)
    if range_start:
        reports = reports.filter(created_at__gte=range_dt_start)
    reports = reports.filter(created_at__lt=range_dt_end)

    counts = {value: 0 for value in DamageReport.Status.values}
    for status in reports.values_list('status', flat=True):
        counts[status] = counts.get(status, 0) + 1

    status_filter = request.GET.get('status', '')
    if status_filter in DamageReport.Status.values:
        reports = reports.filter(status=status_filter)
    else:
        status_filter = ''

    page = paginate(request, reports.order_by('-created_at', '-pk'))
    range_pairs = _range_carry(active_range, range_start, range_end)

    return render(request, 'damage_reports/report_list.html', {
        'reports': page.object_list,
        'page_obj': page,
        'counts': counts,
        'total_count': sum(counts.values()),
        'status_tabs': [
            (value, label, counts.get(value, 0))
            for value, label in DamageReport.Status.choices
        ],
        'status_filter': status_filter,
        'search_query': search_query,
        'branches': Branch.objects.filter(is_active=True).order_by('branch_name') if user.role == OWNER else [],
        'branch_filter': branch_filter,
        'product_options': product_options,
        'product_filter': product_filter,
        'awaiting_total': awaiting_total if user.role in services.MANAGER_ROLES else 0,
        'own_only': user.role == STAFF,
        'can_file': user_can_perform_module_action(user, DAMAGE_REPORTS, ADD),
        # _daterange.html: a preset click must not drop the other filters.
        'active_range': active_range,
        'range_start': range_start or range_end,
        'range_end': range_end,
        'daterange_carry': [
            ('q', search_query), ('status', status_filter),
            ('branch', branch_filter), ('product', product_filter),
        ],
        # One urlencoded filter string for the status tabs, so a tab's href
        # never drops a filter (core.querystrings).
        'filter_qs': list_filter_querystring(
            q=search_query, branch=branch_filter, product=product_filter,
            **dict(range_pairs),
        ),
        # partials/_branch_switch.html (the topbar chip): the Owner's branch
        # filter, carrying every other filter along when it changes.
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        'user_branch': branch_for_user(user),
        'branch_switch_carry': [
            ('q', search_query), ('status', status_filter), ('product', product_filter),
        ] + range_pairs,
        'active_page': 'damage_reports',
    })


# ---------------------------------------------------------------------------
# File a report
# ---------------------------------------------------------------------------

def _procurement_from_request(request):
    """(purchase order, error) for ?procurement= / the posted hidden field.
    Only one this user can already read; anything else is just ignored with a
    message, never an error page."""
    raw = request.POST.get('procurement_id') or request.GET.get('procurement') or ''
    if not str(raw).isdigit():
        return None, ''
    procurement = procurements_visible_to(
        request.user, Procurement.objects.select_related('branch').filter(pk=raw),
    ).first()
    if procurement is None:
        return None, 'That purchase order is not one you can report against.'
    return procurement, ''


def _target_branch(request, procurement):
    """The branch a new report is for. Staff and Managers are pinned to their
    own; an Owner chooses (or inherits it from the purchase order)."""
    if request.user.role != OWNER:
        return branch_for_user(request.user)
    if procurement is not None:
        return procurement.branch
    raw = request.POST.get('branch_id') or request.GET.get('branch') or ''
    if str(raw).isdigit():
        return Branch.objects.filter(pk=raw, is_active=True).first()
    return None


def _prefill(request, branch, options):
    """What the form should start on, from the link that opened it
    (?batch=, ?product=, ?location=, ?type=). Taken only if it names stock in
    this branch; a stale or foreign id just leaves the field blank."""
    prefill = {
        'key': '', 'batch': '', 'quantity': '', 'damage_type': '', 'description': '',
    }
    batch_id = request.GET.get('batch', '')
    if batch_id.isdigit():
        batch = Batch.objects.filter(pk=batch_id, branch=branch, quantity__gt=0).first()
        if batch:
            prefill['key'] = f'{batch.location}|{batch.product_id}'
            prefill['batch'] = str(batch.pk)
    elif request.GET.get('product', '').isdigit():
        location = request.GET.get('location', '')
        if location in DamageReport.StockLocation.values:
            prefill['key'] = f'{location}|{request.GET["product"]}'
    if request.GET.get('type') in [value for value, _ in STAFF_DAMAGE_TYPES]:
        prefill['damage_type'] = request.GET['type']
    return prefill


def _form_context(request, branch, procurement, *, posted=None, error=''):
    options = services.form_stock_options(branch, procurement) if branch else []
    posted = posted or {}
    if posted:
        prefill = {
            'key': posted.get('stock_key', ''),
            'batch': posted.get('batch_id', ''),
            'quantity': posted.get('quantity', ''),
            'damage_type': posted.get('damage_type', ''),
            'description': posted.get('description', ''),
        }
    else:
        prefill = _prefill(request, branch, options) if branch else {}
    if not prefill.get('damage_type') and procurement is not None:
        prefill['damage_type'] = DamageReport.DamageType.ARRIVAL
    return {
        'branch': branch,
        'branches': Branch.objects.filter(is_active=True).order_by('branch_name')
        if request.user.role == OWNER and branch is None else [],
        'stock_options': options,
        'has_stock': any(group['products'] for group in options),
        'procurement': procurement,
        'damage_types': STAFF_DAMAGE_TYPES,
        'prefill': prefill,
        'error': error,
        'max_photos': MAX_PHOTOS,
        'max_photo_mb': MAX_PHOTO_MB,
        'max_description': MAX_DESCRIPTION_LENGTH,
        'active_page': 'damage_reports',
    }


@role_required(*INTERNAL)
def damage_report_create(request):
    """GET: the Report Damaged Product form. POST: file the report."""
    procurement, procurement_error = _procurement_from_request(request)
    if procurement_error:
        messages.error(request, procurement_error)
    branch = _target_branch(request, procurement)

    if request.user.role != OWNER and branch is None:
        messages.error(request, 'No branch is assigned to your account, so it cannot file damage reports.')
        return redirect('damage_report_list')

    if request.method != 'POST':
        return render(request, 'damage_reports/report_form.html',
                      _form_context(request, branch, procurement))

    if branch is None:
        # An Owner who has not chosen a branch yet.
        return render(request, 'damage_reports/report_form.html',
                      _form_context(request, None, procurement,
                                    error='Choose the branch this report is for.'))

    posted = {
        'stock_key': request.POST.get('stock_key', ''),
        'batch_id': request.POST.get('batch_id', ''),
        'quantity': request.POST.get('quantity', ''),
        'damage_type': request.POST.get('damage_type', ''),
        'description': request.POST.get('description', ''),
    }
    location, _sep, product_id = posted['stock_key'].partition('|')
    audit_context.set_action_label('Filed a damaged product report')
    try:
        report = services.create_staff_report(
            request.user, branch_id=branch.pk, location=location, product_id=product_id,
            batch_id=posted['batch_id'], quantity=posted['quantity'],
            damage_type=posted['damage_type'], description=posted['description'],
            procurement_id=procurement.pk if procurement else None,
            photos=request.FILES.getlist('photos'),
        )
    except DamageReportForbidden as exc:
        raise _forbidden(exc)
    except DamageReportError as exc:
        return render(request, 'damage_reports/report_form.html',
                      _form_context(request, branch, procurement, posted=posted, error=str(exc)))

    messages.success(
        request,
        f'{report.report_number} filed. A manager will review it; no stock changes until it is approved.',
    )
    return redirect('damage_report_detail', pk=report.pk)


# ---------------------------------------------------------------------------
# Detail and decisions
# ---------------------------------------------------------------------------

def _visible_report(request, pk):
    return get_object_or_404(
        services.reports_visible_to(
            request.user,
            DamageReport.objects.select_related(
                'branch', 'reported_by', 'reviewed_by', 'product', 'inventory_batch',
                'sales_transaction', 'sales_item', 'procurement', 'withdrawal_slip',
                'refund',
            ),
        ),
        pk=pk,
    )


@role_required(*INTERNAL)
def damage_report_detail(request, pk):
    report = _visible_report(request, pk)
    can_decide = _can_decide(request.user)
    resolutions = [
        {
            'value': r.value,
            'label': r.label,
            'effect': services.effect_text(report, r),
        }
        for r in services.allowed_resolutions(report)
    ]
    return render(request, 'damage_reports/report_detail.html', {
        'report': report,
        'photos': list(report.photos.all()),
        'events': list(report.events.select_related('actor')),
        'is_open': report.is_open,
        'can_decide': can_decide and report.status in DamageReport.OPEN_STATUSES,
        'can_start_review': can_decide and report.status == DamageReport.Status.PENDING,
        'can_resolve': can_decide and report.status == DamageReport.Status.APPROVED,
        'resolutions': resolutions,
        'refund_methods': Refund.RefundMethod.choices,
        'available_now': services.available_stock(report) if report.is_open else None,
        # The supplier return is the existing Procurement workflow; it needs
        # the same role and Add grant it always has.
        'can_start_supplier_return': bool(
            report.flagged_for_supplier_return
            and report.status == DamageReport.Status.APPROVED
            and report.procurement_id
            and request.user.role in services.MANAGER_ROLES
            and user_can_perform_module_action(request.user, PROCUREMENT, ADD)
        ),
        'own_report': report.reported_by_id == request.user.pk,
        'max_note': MAX_NOTE_LENGTH,
        'active_page': 'damage_reports',
    })


def _decide(request, pk, action, success):
    """Shared shell for the four POST-only decision endpoints: scope the
    record (404 outside it), run the service, flash the outcome, go back."""
    report = _visible_report(request, pk)
    try:
        result = action(report)
    except DamageReportForbidden as exc:
        raise _forbidden(exc)
    except DamageReportError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, success(result))
    return redirect('damage_report_detail', pk=report.pk)


@role_required(*INTERNAL)
@require_POST
def damage_report_review(request, pk):
    audit_context.set_action_label('Started reviewing a damage report')
    return _decide(
        request, pk, lambda r: services.start_review(r.pk, request.user),
        lambda r: f'{r.report_number} is now under review.',
    )


@role_required(*INTERNAL)
@require_POST
def damage_report_approve(request, pk):
    audit_context.set_action_label('Approved a damage report')
    return _decide(
        request, pk,
        lambda r: services.approve_report(
            r.pk, request.user, resolution=request.POST.get('resolution', ''),
            note=request.POST.get('note', ''),
            refund_method=request.POST.get('refund_method', ''),
        ),
        lambda r: f'{r.report_number} approved. {services.effect_text(r, r.resolution, done=True)}',
    )


@role_required(*INTERNAL)
@require_POST
def damage_report_reject(request, pk):
    audit_context.set_action_label('Rejected a damage report')
    return _decide(
        request, pk,
        lambda r: services.reject_report(r.pk, request.user, reason=request.POST.get('reason', '')),
        lambda r: f'{r.report_number} rejected. Its photos stay on file.',
    )


@role_required(*INTERNAL)
@require_POST
def damage_report_resolve(request, pk):
    audit_context.set_action_label('Resolved a damage report')
    return _decide(
        request, pk,
        lambda r: services.resolve_report(r.pk, request.user, note=request.POST.get('note', '')),
        lambda r: f'{r.report_number} marked resolved.',
    )


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------

def photo_response(photo):
    """Stream one stored photo. 404 for anything that is missing or is not a
    JPG/PNG, so a file that somehow got in under another type is never served
    as a page."""
    if photo.content_type not in ('image/jpeg', 'image/png') or not photo.image:
        raise Http404
    try:
        handle = photo.image.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    response = FileResponse(handle, content_type=photo.content_type)
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


@role_required(*INTERNAL)
@require_GET
def damage_report_photo(request, pk, photo_pk):
    report = _visible_report(request, pk)
    photo = get_object_or_404(report.photos, pk=photo_pk)
    return photo_response(photo)
