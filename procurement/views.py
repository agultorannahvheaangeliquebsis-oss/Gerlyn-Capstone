# procurement/views.py
# Structure:
#   SECTION 1 — original Django template views (untouched, kept for reference)
#   SECTION 2 — new DRF API views (what React calls)

import io
from datetime import datetime, timedelta
import uuid
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse, HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from django.db.models import Count, F, Prefetch, Q

from xhtml2pdf import pisa

from core.scoping import (
    branch_ids_for_user, scope_queryset, user_can_access_branch, branch_for_user,
)
from core.people import person_display_name
from core.querystrings import list_filter_querystring
from core.timebounds import local_range_bounds, range_from_request
from core.permissions import (
    BRANCH_MANAGER, HasModuleAccess, OWNER, STAFF, SUPPLIER, role_required,
)
from core.module_permissions import (
    ADD, DELETE, EDIT, DELIVERIES, PROCUREMENT,
    user_can_perform_module_action,
)

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import (
    Procurement, ProcurementItem, SupplierProduct, SupplierProductRequest,
    SupplierReturn, DeliveryReceipt, DeliveryReceiptLine, DeliveryScanLog,
)
from .access import (
    STAFF_PENDING_DELIVERY_STATUSES, procurements_visible_to,
    user_can_read_procurement,
)
from .services import (
    DeliveryError, ExternalOrderError, FundingError, OfferReviewError, RescheduleError, ReturnError,
    SupplierRemovalBlocked, SupplierTransferError,
    active_supplier_product_links, apply_agreed_terms,
    cancel_return, clean_funding, create_supplier_return,
    receive_replacement, record_delivery, remove_supplier_product, reschedule_delivery,
    record_external_order, respond_to_return, returnable_lines, review_offer, review_product_request,
    set_funding, settle_with_credit, ship_return, transfer_supplier_products,
    validate_expected_delivery_date,
)
from .serializers import (
    ProcurementReadSerializer,
    ProcurementExternalOrderSerializer,
    ProcurementWriteSerializer,
    ProcurementStatusSerializer,
    DeliveryEventSerializer,
)
from inventory import pricing
from inventory.models import (
    Batch, Product, ProductCategory, StockMovement, Warehouse, WarehouseStock,
)
from accounts.models import Notification, SupplierCategory, User


# =============================================================================
# SECTION 1 — Original Django template views (unchanged)
# These still work for any admin/template-based pages.
# =============================================================================

def _procurement_list_context(request):
    """Everything procurement_list.html needs, in one place so procurement_add's
    error re-render (the Create PO modal reopened with what was typed) cannot
    drift from the initial GET."""
    from branches.models import Branch

    procurements = Procurement.objects.select_related('branch', 'supplier', 'owner').all()

    procurements = procurements_visible_to(request.user, procurements)

    search_query = request.GET.get('q', '')
    if search_query:
        procurements = procurements.filter(
            Q(po_number__icontains=search_query) |
            Q(supplier__supplier_profile__supplier_name__icontains=search_query) |
            Q(supplier__username__icontains=search_query)
        )

    # Owner-only branch filter — Manager/Staff are already pinned to their
    # own branch's orders (and Supplier to their own orders) above, so a
    # selector would do nothing for them.
    show_branch_filter = request.user.role == User.Role.OWNER
    branch_filter = request.GET.get('branch', '') if show_branch_filter else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''
    if branch_filter:
        procurements = procurements.filter(branch_id=branch_filter)

    # The shared back-office range control (_daterange.html), same as
    # Billing/the Order Console. order_date is a plain DateField (no
    # timezone conversion needed, unlike a DateTimeField — see
    # core.timebounds), so the resolved dates are compared directly.
    range_start, range_end, active_range = range_from_request(request)
    if range_start:
        procurements = procurements.filter(order_date__gte=range_start)
    procurements = procurements.filter(order_date__lte=range_end)

    procurements = procurements.order_by('-order_date')

    status_filter = request.GET.get('status', '')
    counts = {
        'all': procurements.count(),
        'draft': procurements.filter(status=Procurement.Status.DRAFT).count(),
        'approved': procurements.filter(status=Procurement.Status.APPROVED).count(),
        # One tab for all three negotiation statuses (Pending Owner/Supplier
        # Review, Terms Agreed) rather than three more tabs — they're all
        # "in progress toward Incoming" from this list's point of view; the
        # exact step is what procurement_detail's negotiation card is for.
        'negotiating': procurements.filter(
            status__in=Procurement.NEGOTIATION_STATUSES
        ).count(),
        # Of the negotiating count above, how many are sitting in the
        # buyer's own inbox right now — Pending Supplier Review and Terms
        # Agreed are both "waiting on supplier" from here, so only Pending
        # Owner Review is ever the buyer's turn. Lets the Negotiating tab
        # say "N for you" instead of making the buyer open every row in the
        # tab to find out whose turn it is.
        'pending_owner_review': procurements.filter(
            status=Procurement.Status.PENDING_OWNER_REVIEW
        ).count(),
        'incoming': procurements.filter(status=Procurement.Status.INCOMING).count(),
        'communicated': procurements.filter(
            status=Procurement.Status.COMMUNICATED
        ).count(),
        'partially_received': procurements.filter(
            status=Procurement.Status.PARTIALLY_RECEIVED
        ).count(),
        'received': procurements.filter(status=Procurement.Status.RECEIVED).count(),
        'cancelled': procurements.filter(status=Procurement.Status.CANCELLED).count(),
    }
    if status_filter == 'Negotiating':
        procurements = procurements.filter(status__in=Procurement.NEGOTIATION_STATUSES)
    elif status_filter:
        procurements = procurements.filter(status=status_filter)

    # Staff can now view this list (see core.module_permissions), but
    # creating a PO stays Owner/Manager-only — procurement_add's own role
    # check has always enforced that; this just stops offering a button
    # Staff would only get rejected from now that they can reach the page.
    can_create = (
        request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
        and user_can_perform_module_action(request.user, PROCUREMENT, ADD)
    )

    switch_branches = Branch.objects.all() if show_branch_filter else []

    context = {
        'procurements': procurements,
        'counts': counts,
        'status_filter': status_filter,
        'search_query': search_query,
        'today': timezone.localdate(),
        'awaiting_delivery_statuses': Procurement.AWAITING_DELIVERY_STATUSES,
        'can_create': can_create,
        'po_errors': None,
        'po_field_errors': None,
        'po_form_data': None,
        'po_restore_rows': None,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Warehouse Stock, Product Master List, Billing and Receive Delivery.
        'filter_branch': next((b for b in switch_branches if str(b.id) == branch_filter), None),
        'user_branch': branch_for_user(request.user),
        # `branches` is reserved for the Create Purchase Order page's own
        # branch picker (see _procurement_form_context below) — the topbar
        # switcher gets its own key so the two can never collide.
        'switch_branches': switch_branches,
        # _daterange.html: a preset click must not drop the search/filters.
        'active_range': active_range,
        'range_start': range_start or range_end,
        'range_end': range_end,
        'daterange_carry': [('q', search_query), ('status', status_filter), ('branch', branch_filter)],
        # O-3 — one urlencoded filter string for the status tabs, same fix
        # as Billing/the Order Console: a tab's href used to be hand-built
        # and silently dropped the branch filter on every click.
        'filter_qs': list_filter_querystring(q=search_query, branch=branch_filter),
        # Every other active filter, for the topbar branch switcher
        # (partials/_branch_switch.html) to carry along.
        'branch_switch_carry': [('q', search_query), ('status', status_filter)],
    }
    if can_create:
        # suppliers/products/branches/user_branch, plus the (more correct,
        # localdate-based) 'today' the Create Purchase Order page's Order
        # Date field defaults to. Computed here too (not just by
        # procurement_add) so this same function can back both the list
        # page's Create PO link and procurement_add's own GET/error render.
        context.update(_procurement_form_context(request.user))
        submission_key = request.session.get('po_submission_key')
        if not submission_key:
            submission_key = uuid.uuid4().hex
            request.session['po_submission_key'] = submission_key
        context['po_submission_key'] = submission_key
    return context


@role_required(OWNER, BRANCH_MANAGER, STAFF, SUPPLIER)
def procurement_list(request):
    return render(request, 'procurement/procurement_list.html', _procurement_list_context(request))


def _can_read_procurement(user, proc):
    """Scoping the list is not enough — every per-order page must check too,
    or anyone can read another branch's (or a rival supplier's) order by
    editing the URL."""
    return user_can_read_procurement(user, proc)


# Order Progress: a display-only simplification of Procurement's 9-state
# status graph into 5 milestones, the same role as
# sales.views._order_progress/ORDER_PROGRESS_STEPS — purely what the
# stepper on the detail page draws. Never read by can_transition_to or any
# POST handler, so it can never drift into gating anything.
PROCUREMENT_PROGRESS_STEPS = ['Draft', 'Approved', 'Terms Agreed', 'Incoming', 'Received']

_PROGRESS_STEP_BY_STATUS = {
    Procurement.Status.DRAFT: 0,
    Procurement.Status.APPROVED: 1,
    # The three negotiation sub-statuses and the manual Communicated/Ordered
    # bypass all mean the same thing on the stepper: the two sides are
    # settling how the order ships. Approved reads as done the moment
    # negotiation starts, whichever path got there.
    Procurement.Status.PENDING_OWNER_REVIEW: 2,
    Procurement.Status.PENDING_SUPPLIER_REVIEW: 2,
    Procurement.Status.TERMS_AGREED: 2,
    Procurement.Status.COMMUNICATED: 2,
    # Partially Received still reads as "Incoming" on the track — the items
    # table and receipt_flag pill already carry the partial-vs-full
    # distinction, so the stepper doesn't need a sixth dot for it.
    Procurement.Status.INCOMING: 3,
    Procurement.Status.PARTIALLY_RECEIVED: 3,
    Procurement.Status.RECEIVED: 4,
}


def _procurement_progress(proc):
    cancelled = proc.status == Procurement.Status.CANCELLED
    current = 0 if cancelled else _PROGRESS_STEP_BY_STATUS.get(proc.status, 0)
    return {
        'cancelled': cancelled,
        'steps': [
            {
                'name': name,
                'done': (not cancelled) and i < current,
                'current': (not cancelled) and i == current,
            }
            for i, name in enumerate(PROCUREMENT_PROGRESS_STEPS)
        ],
    }


def _procurement_timeline(proc, receipts):
    """Status History for the detail page: a derived, read-only timeline
    built from timestamps Procurement/DeliveryReceipt already store. There
    is no OrderStatusHistory-equivalent table for purchase orders, and no
    cancelled_at/cancel_reason field either — so a cancelled order gets no
    row here. The status pill above already says Cancelled; inventing a
    timestamp nothing recorded would be worse than leaving it out.
    """
    created_at = timezone.make_aware(datetime.combine(proc.order_date, datetime.min.time()))
    timeline = [{'when': created_at, 'label': 'Created as Draft', 'note': None}]

    if proc.authorized_at:
        timeline.append({'when': proc.authorized_at, 'label': 'Approved', 'note': None})

    if proc.communicated_at:
        timeline.append({
            'when': proc.communicated_at,
            'label': f'Communicated/Ordered ({proc.get_communication_method_display()})',
            'note': proc.communication_note or None,
        })

    if proc.supplier_acknowledged_at:
        timeline.append({
            'when': proc.supplier_acknowledged_at,
            'label': 'Supplier acknowledged', 'note': None,
        })

    for i, r in enumerate(sorted(receipts, key=lambda r: r.received_at), start=1):
        timeline.append({
            'when': r.received_at,
            'label': f'Receipt #{i} recorded',
            'note': f'Received by {r.received_by_name}' if r.received_by_name else None,
        })

    timeline.sort(key=lambda step: step['when'])
    return timeline


@login_required(login_url='accounts:login')
def procurement_detail(request, pk):
    proc = get_object_or_404(
        Procurement.objects.select_related('branch', 'supplier'), pk=pk
    )

    if not _can_read_procurement(request.user, proc):
        messages.error(request, 'That purchase order is not available to you.')
        return redirect('procurement_list')

    items = proc.items.select_related('product').all()
    is_supplier_party = (
        request.user.role == User.Role.SUPPLIER and proc.supplier_id == request.user.id
    )
    receipts = list(proc.receipts.select_related('received_by').prefetch_related(
        'lines__item__product'
    ))
    return render(request, 'procurement/procurement_detail.html', {
        'progress': _procurement_progress(proc),
        'status_timeline': _procurement_timeline(proc, receipts),
        'supplier_returns': proc.supplier_returns.select_related('supplier'),
        # Whether the negotiation card has a form for *this* viewer to act on
        # right now, vs. just a status message — see the template's
        # NEGOTIATION block. Kept in one place so the "waiting" banner
        # styling and the actionable form's card styling never fall out of
        # sync with who can actually do what.
        'negotiation_actionable': (
            (is_supplier_party and proc.status in (
                Procurement.Status.APPROVED, Procurement.Status.PENDING_SUPPLIER_REVIEW,
            ))
            or (
                proc.status == Procurement.Status.PENDING_OWNER_REVIEW
                and request.user.role in (User.Role.OWNER, User.Role.BRANCH_MANAGER)
            )
            or (is_supplier_party and proc.status == Procurement.Status.TERMS_AGREED)
        ),
        'can_return_goods': (
            request.user.role in (User.Role.OWNER, User.Role.BRANCH_MANAGER)
            and user_can_perform_module_action(request.user, PROCUREMENT, ADD)
            and proc.status in (
                Procurement.Status.RECEIVED, Procurement.Status.PARTIALLY_RECEIVED,
            )
        ),
        'days_overdue': proc.days_overdue(),
        'can_reschedule': (
            request.user.role in (User.Role.OWNER, User.Role.BRANCH_MANAGER)
            and user_can_perform_module_action(request.user, PROCUREMENT, EDIT)
            and proc.status in Procurement.AWAITING_DELIVERY_STATUSES
        ),
        'can_mark_communicated': (
            request.user.role in (
                User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF,
            )
            and user_can_perform_module_action(request.user, DELIVERIES, EDIT)
            and proc.can_transition_to(Procurement.Status.COMMUNICATED)
        ),
        'communication_methods': Procurement.CommunicationMethod.choices,
        'today': timezone.localdate(),
        'funding_choices': Procurement.FundingSource.choices,
        'can_edit_funding': (
            request.user.role in (User.Role.OWNER, User.Role.BRANCH_MANAGER)
            and user_can_perform_module_action(request.user, PROCUREMENT, EDIT)
            and not proc.funding_locked
        ),
        'procurement': proc, 'items': items,
        'receipts': receipts,
    })


def _procurement_document_context(proc, request):
    """Everything the printable PO / downloadable PDF need, in one place so
    the two output paths (po_print.html via the browser, po_pdf.html via
    xhtml2pdf) can never drift apart on what a purchase order document says."""
    items = proc.items.select_related('product').all()

    # total_cost is the stored figure and stays authoritative on the document.
    # The line sum is computed alongside it purely so the template can show a
    # mismatch rather than print a total that doesn't add up — that happens
    # with legacy orders whose items were edited after the header was saved.
    line_sum = sum((i.line_total for i in items), 0)

    # A second, separate figure: what actually arrived, valued at each line's
    # confirmed cost. Only meaningful once the order has been received, so it
    # is gated on the two receipt statuses — on an Approved PO nothing has
    # arrived yet, and a document being sent TO the supplier must not
    # announce "actual received ₱0.00". Deliberately not gated on
    # receipt_flag: that field is set by the receiving workflow and can lag
    # or be absent, while this is always derivable from the items themselves.
    received_states = (
        Procurement.Status.RECEIVED, Procurement.Status.PARTIALLY_RECEIVED,
    )
    actual_received_cost = proc.actual_received_cost
    received_differs = (
        proc.status in received_states and actual_received_cost != proc.total_cost
    )

    supplier_profile = getattr(proc.supplier, 'supplier_profile', None)

    return {
        'procurement': proc,
        'items': items,
        'line_sum': line_sum,
        'totals_disagree': line_sum != proc.total_cost,
        'actual_received_cost': actual_received_cost,
        'received_differs': received_differs,
        'supplier_profile': supplier_profile,
        # Signature-block names, pre-resolved so the template never chains a
        # lookup through a FK that may be null. Any of the three can legally
        # be absent: created_by postdates the older orders, owner is only set
        # when an Owner raised the PO, and a supplier may have no named
        # contact person.
        'prepared_by_name': person_display_name(proc.created_by),
        'authorized_by_name': person_display_name(proc.owner),
        'supplier_signatory': (
            (supplier_profile.contact_person or supplier_profile.supplier_name)
            if supplier_profile else ''
        ),
        'printed_on': timezone.localdate(),
        'printed_by': person_display_name(request.user),
        'receipts': proc.receipts.select_related('received_by'),
    }


def _get_procurement_for_document(request, pk):
    """Shared fetch + access check for procurement_print and procurement_pdf.
    Returns (proc, None) on success or (None, redirect_response) if the
    caller should bail out immediately."""
    proc = get_object_or_404(
        Procurement.objects.select_related(
            'branch', 'supplier', 'supplier__supplier_profile',
            'owner', 'owner__owner_profile',
            'created_by',
        ),
        pk=pk,
    )
    if not _can_read_procurement(request.user, proc):
        messages.error(request, 'That purchase order is not available to you.')
        return None, redirect('procurement_list')
    return proc, None


@login_required(login_url='accounts:login')
def procurement_print(request, pk):
    """The printable purchase order handed to (or emailed to) the supplier.

    Read-only and server-rendered — the browser's own print-to-PDF (or the
    "Download PDF" button next to it, see procurement_pdf) does the output.
    """
    proc, bail = _get_procurement_for_document(request, pk)
    if bail:
        return bail

    return render(request, 'procurement/po_print.html',
                  _procurement_document_context(proc, request))


@login_required(login_url='accounts:login')
def procurement_pdf(request, pk):
    """Generate an actual .pdf file for this purchase order and send it as a
    download, rather than relying on the user's browser print dialog.

    Uses xhtml2pdf (pure-Python, no system libraries like GTK/Cairo needed —
    unlike WeasyPrint, which is what would normally be reached for here but
    doesn't install cleanly on Windows or a typical Railway build). Its CSS
    support is a CSS2.1-ish subset with no flexbox/grid, so it renders its
    own standalone template (po_pdf.html) rather than po_print.html, which
    depends on gerlyn.css and flex/grid for the on-screen view.
    """
    proc, bail = _get_procurement_for_document(request, pk)
    if bail:
        return bail

    html = render_to_string(
        'procurement/po_pdf.html', _procurement_document_context(proc, request)
    )

    buffer = io.BytesIO()
    result = pisa.CreatePDF(src=html, dest=buffer)
    if result.err:
        messages.error(
            request,
            'Could not generate the PDF for this purchase order. Try the Print '
            'option instead, or contact support.'
        )
        return redirect('procurement_detail', pk=pk)

    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{proc.po_number}.pdf"'
    return response


def _po_suppliers_for_user(user):
    """Active suppliers the user may address a new purchase order to."""
    user_branch = branch_for_user(user)
    suppliers = User.objects.filter(
        role=User.Role.SUPPLIER,
        is_active=True,
    )

    if user_branch:
        return suppliers.filter(
            Q(supplier_profile__branch=user_branch)
            | Q(supplier_profile__branch__isnull=True)
        )
    if user.role == User.Role.OWNER:
        return suppliers
    return suppliers.none()


def _procurement_form_context(user):
    """The suppliers/products/branches the Create Purchase Order page needs,
    shared by _procurement_list_context's initial GET and procurement_add's
    error re-render so the two cannot drift apart."""
    from branches.models import Branch

    user_branch = branch_for_user(user)  # None for Owner; the manager's branch otherwise

    if user_branch:
        # Pinned to one branch: only suppliers assigned to it (or unassigned,
        # i.e. available to every branch) belong in this dropdown — a
        # Branch Manager creating a PO for their branch should never see,
        # or be able to pick, a supplier scoped to a different one.
        branches = Branch.objects.filter(pk=user_branch.pk)
        suppliers = _po_suppliers_for_user(user)
    elif user.role == User.Role.OWNER:
        branches = Branch.objects.all()
        suppliers = _po_suppliers_for_user(user)
    else:
        # branch_for_user() is also None for a Manager/Staff whose profile
        # branch is NULL. They have no branch to raise a PO for, so the modal
        # gets nothing to pick — not the Owner's every-branch list.
        branches = Branch.objects.none()
        suppliers = _po_suppliers_for_user(user)

    # Keep each relationship row instead of flattening to Product because
    # default cost belongs to a supplier/product pair. The PO picker uses
    # these links to put preferred products first and show known costs, while
    # still offering the full product list for manual selection.
    links = list(
        SupplierProduct.objects.approved().filter(supplier__in=suppliers)
        .select_related('product')
        .prefetch_related('product__price_tiers')
        .order_by('supplier_id', 'product__product_name')
    )
    supplier_product_options = [
        {
            'supplier_id': link.supplier_id,
            'product_id': link.product_id,
            'name': link.product.product_name,
            'sku': link.product.sku_code,
            'cost': str(
                link.default_cost_price
                if link.default_cost_price is not None
                else link.product.bulk_floor_price
            ),
        }
        for link in links
    ]

    supplier_catalog = {}
    for link in SupplierProduct.objects.approved().filter(
        supplier__in=suppliers
    ).values('supplier_id', 'product_id', 'current_cost_price'):
        supplier_catalog.setdefault(str(link['supplier_id']), {})[
            str(link['product_id'])
        ] = (
            str(link['current_cost_price'])
            if link['current_cost_price'] is not None else None
        )

    suppliers = suppliers.select_related('supplier_profile', 'supplier_profile__category')
    return {
        'funding_choices': Procurement.FundingSource.choices,
        'products': Product.objects.all(),
        # Every product, by id and name, for the page's JS to rebuild each
        # item row's dropdown from (see procurement_form.html).
        'product_options': [
            {'id': str(pid), 'name': name}
            for pid, name in Product.objects.order_by('product_name').values_list(
                'pk', 'product_name')
        ],
        'suppliers': suppliers,
        # The optional "Supplier category" filter above the supplier dropdown:
        # only categories at least one selectable supplier is filed under.
        'supplier_categories': SupplierCategory.objects.filter(
            suppliers__user__in=suppliers
        ).distinct(),
        'supplier_catalog': supplier_catalog,
        'supplier_product_options': supplier_product_options,
        'branches': branches,
        'user_branch': user_branch,
        # localdate(), not now(): the order-date field defaults to this, and
        # now() is UTC under USE_TZ, so late evening in Asia/Manila would
        # prefill tomorrow's date.
        'today': timezone.localdate(),
    }


@login_required(login_url='accounts:login')
def procurement_add(request):
    """The "Create Purchase Order" page — its own page (see
    templates/procurement/procurement_form.html), not a modal on the
    Purchase Orders list. GET renders the form (branch/supplier/date fields
    and the item rows, via _procurement_list_context). POST handles the
    submit, redisplaying that same page on a validation error so the picked
    branch/supplier and entered rows aren't lost — same pattern as Walk-in
    Sale's transaction_add.
    """
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to create procurement orders.')
        return redirect('procurement_list')

    if request.method != 'POST':
        return render(request, 'procurement/procurement_form.html', _procurement_list_context(request))

    submission_key = (
        request.POST.get('submission_key', '').strip()
        or request.session.get('po_submission_key')
        or uuid.uuid4().hex
    )
    prior = Procurement.objects.filter(submission_key=submission_key).first()
    if prior:
        if prior.created_by_id == request.user.id:
            messages.info(request, f'{prior.po_number} was already created; no duplicate was added.')
            return redirect('procurement_detail', pk=prior.pk)
        messages.error(request, 'That purchase-order submission token belongs to another user.')
        return redirect('procurement_list')

    if request.user.role == User.Role.BRANCH_MANAGER:
        try:
            branch_id = request.user.manager_profile.branch.id
        except Exception:
            messages.error(request, 'No branch assigned to your account.')
            return redirect('procurement_list')
    else:
        branch_id = request.POST.get('branch')

    supplier_id  = request.POST.get('supplier')
    order_date_text = request.POST.get('order_date', '').strip()
    product_ids  = request.POST.getlist('product_id')
    quantities   = request.POST.getlist('quantity_ordered')

    # Where the money comes from is required up front (P19) — checked before
    # anything else so a missing source can't be masked by a different error.
    try:
        funding_source, funding_note = clean_funding(
            request.POST.get('funding_source'), request.POST.get('funding_note'),
        )
    except FundingError as exc:
        context = _procurement_list_context(request)
        context.update({
            'po_errors': [str(exc)],
            'po_form_data': request.POST,
            'po_restore_rows': [
                {'product_id': pid, 'quantity': qty}
                for pid, qty in zip(product_ids, quantities) if pid
            ],
        })
        return render(request, 'procurement/procurement_form.html', context)

    # Neither cost nor delivery date is accepted from this form — both are
    # negotiated with the supplier after the order is raised (see
    # procurement_supplier_propose / procurement_review_terms), never set by
    # whoever creates the PO.
    errors = []
    field_errors = {}
    try:
        order_date = parse_date(order_date_text) if order_date_text else None
    except ValueError:
        order_date = None
    if order_date is None:
        field_errors['order_date'] = 'Enter a valid order date.'
        errors.append(field_errors['order_date'])
    items = []
    for product_id, qty in zip(product_ids, quantities):
        if not product_id and not qty:
            continue
        if not product_id or not qty:
            errors.append('Every item must include a product and quantity.')
            continue
        try:
            parsed_product_id = int(product_id)
            parsed_qty = int(qty)
        except (TypeError, ValueError):
            errors.append('Every item needs a valid product and quantity.')
            continue
        if parsed_qty < 1:
            errors.append('Item quantity must be at least 1.')
            continue
        items.append((parsed_product_id, parsed_qty))

    if not items and not errors:
        errors.append(
            'The order was not created — add at least one complete item.'
        )

    suppliers = _po_suppliers_for_user(request.user)
    supplier = suppliers.filter(pk=supplier_id).first()
    if supplier is None:
        errors.append('Choose an active supplier available to this branch.')

    parsed_product_ids = [product_id for product_id, _ in items]
    if len(parsed_product_ids) != len(set(parsed_product_ids)):
        errors.append('Each product may appear only once on a purchase order.')

    existing_product_ids = set(
        Product.objects.filter(pk__in=parsed_product_ids).values_list(
            'pk', flat=True
        )
    )
    if set(parsed_product_ids) - existing_product_ids:
        errors.append('Choose a valid product for every order item.')

    if errors:
        context = _procurement_list_context(request)
        context.update({
            'po_errors': errors,
            'po_field_errors': field_errors,
            'po_form_data': request.POST,
            'po_restore_rows': [
                {'product_id': pid, 'quantity': qty}
                for pid, qty in zip(product_ids, quantities) if pid
            ],
        })
        return render(request, 'procurement/procurement_form.html', context)

    is_owner = request.user.role == User.Role.OWNER

    try:
        with transaction.atomic():
            # Who raises the order decides whether it is already authorized.
            # Every PO used to be created Ordered no matter who submitted this
            # form, so a Branch Manager could commit spend against a supplier
            # with no Owner ever in the loop — and `owner` (the authorizer FK)
            # was left null, so there wasn't even a trace of who hadn't approved
            # it (finding C-1). An Owner raising a PO still authorizes it in the
            # same act, exactly as before; the only change for them is that the
            # authorization is now written down rather than implied. Either way
            # it lands on Approved, never further — price and delivery date are
            # negotiated with the supplier from here, not set at creation.
            proc = Procurement.objects.create(
                branch_id=branch_id,
                supplier=supplier,
                owner=request.user if is_owner else None,
                created_by=request.user,
                order_date=order_date,
                total_cost=0,
                status=Procurement.Status.APPROVED if is_owner else Procurement.Status.DRAFT,
                authorized_at=timezone.now() if is_owner else None,
                funding_source=funding_source,
                funding_note=funding_note,
                submission_key=submission_key,
            )

            for product_id, qty in items:
                ProcurementItem.objects.create(
                    procurement=proc,
                    product_id=product_id,
                    quantity_ordered=qty,
                )
    except IntegrityError:
        # The unique token is the final guard when two identical requests
        # race between the lookup above and INSERT. Treat the loser as the
        # same successful submission instead of returning a server error.
        prior = Procurement.objects.filter(submission_key=submission_key).first()
        if prior and prior.created_by_id == request.user.id:
            messages.info(request, f'{prior.po_number} was already created; no duplicate was added.')
            return redirect('procurement_detail', pk=prior.pk)
        raise

    if is_owner:
        from .notifications import notify_po_approved

        notify_po_approved(proc, request.user)
        # Same opt-in bottom-sheet as the Cancelled confirmation below and in
        # procurement_approve's Draft -> Approved step — the Owner raising a
        # PO authorizes it in the same act (see the comment above), so it
        # deserves the same confirmation, landing on the tab
        # that shows it rather than back on All.
        messages.success(
            request, f'{proc.po_number} is now {proc.status}.',
            extra_tags='sheet',
        )
        request.session.pop('po_submission_key', None)
        return redirect(f"{reverse('procurement_list')}?status={proc.status}")

    messages.success(
        request,
        f'{proc.po_number} was created as a draft and is waiting for '
        f'Owner approval before it can be ordered or received.'
    )
    request.session.pop('po_submission_key', None)
    return redirect('procurement_list')


@login_required(login_url='accounts:login')
def procurement_update_status(request, pk):
    proc = get_object_or_404(Procurement, pk=pk)

    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.SUPPLIER]:
        messages.error(request, 'You do not have permission to update this order.')
        return redirect('procurement_list')

    # Whether this user may act on THIS record at all — checked before the
    # Received special case, C-3's transition guard and the atomic block,
    # because it's independent of what they're trying to change it to. Every
    # other per-order procurement view already gates here (delivery_receive,
    # the DRF ProcurementStatusUpdateAPIView); this door was the exception,
    # so a Branch Manager could POST a status onto another branch's PO
    # (finding C-6). _can_read_procurement is the same supplier-aware wrapper
    # over core.scoping.user_can_access_branch that procurement_detail uses —
    # Owner unrestricted, Manager/Staff their branch only, supplier their own
    # order only.
    if not _can_read_procurement(request.user, proc):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('procurement_list')

    if request.method == 'POST':
        new_status = request.POST.get('status')

        # Negotiation targets carry their own payload (a price/date offer,
        # or who is accepting what) that a bare status POST has none of —
        # routed to the dedicated views instead, same idea as the Received
        # special case below. Incoming is included: it's reachable only
        # through the supplier's own final confirmation once Terms Agreed,
        # never as a status this generic form can set directly (that used
        # to be a way to skip negotiation entirely by POSTing a bare status).
        if new_status in (
            Procurement.Status.PENDING_OWNER_REVIEW,
            Procurement.Status.PENDING_SUPPLIER_REVIEW,
            Procurement.Status.TERMS_AGREED,
            Procurement.Status.COMMUNICATED,
            Procurement.Status.INCOMING,
        ):
            messages.error(
                request,
                f'{proc.po_number}: price and delivery date are negotiated '
                f'with the supplier, not set directly — see the order for '
                f'the current step.'
            )
            return redirect('procurement_detail', pk=pk)

        # Receiving moves stock, so it goes through the Deliveries screen where
        # actual quantities are recorded. This guard also stops a repeat POST
        # from adding the same delivery to stock twice. delivery_receive has
        # its own atomic block that re-locks and rejects an already-Received or
        # Cancelled PO, so the transition is still validated — just not here.
        if new_status in (
            Procurement.Status.PARTIALLY_RECEIVED,
            Procurement.Status.RECEIVED,
        ):
            if proc.status == Procurement.Status.RECEIVED:
                messages.error(request, f'{proc.po_number} has already been received.')
                return redirect('procurement_detail', pk=pk)
            return redirect('delivery_receive', pk=pk)

        # UIUX-006 — a Supplier's writes here are view + confirm-delivery-date
        # only (see the SUPPLIER PORTAL block's own docstring above
        # supplier_set_delivery_date): they deliberately cannot self-confirm
        # Received, and cancelling their own order is the same class of
        # self-serving write — it would let a supplier back out of a
        # commitment the store, not the supplier, is meant to control.
        if new_status == Procurement.Status.CANCELLED and request.user.role == User.Role.SUPPLIER:
            messages.error(request, 'You do not have permission to cancel this order.')
            return redirect('procurement_detail', pk=pk)

        # Every other target: validate against the LOCKED record's current
        # status. This view used to write request.POST['status'] straight onto
        # the row with no check — a Received PO could be forced back to Ordered,
        # or set to an arbitrary string. Same Procurement.ALLOWED_TRANSITIONS
        # the DRF endpoint enforces (finding C-3). select_for_update mirrors
        # billing_confirm / transaction_set_status.
        with transaction.atomic():
            locked = Procurement.objects.select_for_update().get(pk=pk)
            if not locked.can_transition_to(new_status):
                messages.error(
                    request,
                    f'{locked.po_number}: cannot change a {locked.status} order to '
                    f'{new_status or "an empty status"}.'
                )
                return redirect('procurement_detail', pk=pk)

            # Draft -> Ordered IS a legal transition on the map, but it is the
            # authorization step and belongs to the Owner alone. Without this
            # the C-1 gate would be one locked door beside an open one: a
            # Branch Manager whose PO was forced to Draft could simply pick
            # "Ordered" from this form and approve their own spend. Routed to
            # procurement_approve instead, which records owner/authorized_at —
            # this endpoint records that same audit pair below for an Owner.
            if (locked.status == Procurement.Status.DRAFT
                    and new_status == Procurement.Status.APPROVED
                    and request.user.role != User.Role.OWNER):
                messages.error(
                    request,
                    f'{locked.po_number} is a draft — only the Owner can '
                    f'approve it into an order.'
                )
                return redirect('procurement_detail', pk=pk)

            is_approval = (
                locked.status == Procurement.Status.DRAFT
                and new_status == Procurement.Status.APPROVED
            )
            locked.status = new_status
            if is_approval:
                locked.owner = request.user
                locked.authorized_at = timezone.now()
            locked.save()

        # Cancellation gets the bottom-sheet (extra_tags='sheet', same opt-in
        # internal_base.html already gives Deliveries) instead of the banner:
        # it's a terminal state reached from a confirm dialog, so the
        # follow-up should read as one dismissable "done", not a persistent
        # strip sitting above a PO that no longer has anything to update.
        messages.success(
            request, f'{proc.po_number} is now {new_status}.',
            extra_tags='sheet' if new_status == Procurement.Status.CANCELLED else '',
        )

        if is_approval:
            from .notifications import notify_po_approved

            notify_po_approved(locked, request.user)

        if new_status == Procurement.Status.CANCELLED:
            from core.module_permissions import PROCUREMENT
            from core.notifications import (
                CATEGORY_PROCUREMENT, PRIORITY_HIGH, notify, notify_supplier,
            )

            notify(
                CATEGORY_PROCUREMENT, PRIORITY_HIGH,
                f'{proc.po_number} cancelled',
                f'{proc.po_number} to {proc.supplier} was cancelled by '
                f'{request.user.username}.',
                branch=proc.branch,
                action_url=reverse('procurement_detail', kwargs={'pk': proc.pk}),
                action_label='View PO',
                required_module=PROCUREMENT,
            )

            # Only if the supplier had already seen this order (i.e. it was
            # visible to the supplier, not still an internal Draft) — `proc` was fetched
            # before the transaction above and still holds the pre-change
            # status, so this reflects what the supplier's portal showed
            # them a moment ago.
            if proc.status in Procurement.COMMITTED_OPEN_STATUSES:
                notify_supplier(
                    proc.supplier, PRIORITY_HIGH,
                    f'{proc.po_number} cancelled',
                    f'GERLYN cancelled {proc.po_number}. No '
                    f'delivery is needed for this order.',
                    action_url=reverse('supplier_portal'),
                    action_label='View order',
                )

            # Cancelling is terminal — there's nothing left to do on this PO's
            # own detail page, so land back on the list, already filtered to
            # the Cancelled tab, instead of reopening the order that was just
            # closed out. The bottom-sheet above still confirms the result.
            return redirect(f"{reverse('procurement_list')}?status={Procurement.Status.CANCELLED}")

    return redirect('procurement_detail', pk=pk)


@login_required(login_url='accounts:login')
@require_POST
def procurement_set_funding(request, pk):
    """Change where a PO's money comes from, until its terms are agreed.
    Owner, or the Branch Manager of the PO's own branch."""
    if request.user.role not in (User.Role.OWNER, User.Role.BRANCH_MANAGER):
        messages.error(request, 'You do not have permission to change this.')
        return redirect('procurement_list')

    proc = get_object_or_404(Procurement, pk=pk)
    if not user_can_access_branch(request.user, proc.branch_id):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('procurement_list')

    try:
        set_funding(
            proc, request.POST.get('funding_source'), request.POST.get('funding_note'),
        )
    except FundingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Funding source updated for {proc.po_number}.')
    return redirect('procurement_detail', pk=pk)


@login_required(login_url='accounts:login')
@require_POST
def procurement_reschedule(request, pk):
    """Give an Incoming / Partially Received order a new delivery date — the
    manual answer to an overdue delivery (the other is cancelling it). Clears
    the overdue flags so the new date is tracked afresh, and tells the
    supplier."""
    from core.notifications import PRIORITY_MEDIUM, notify_supplier

    if request.user.role not in (User.Role.OWNER, User.Role.BRANCH_MANAGER):
        messages.error(request, 'You do not have permission to reschedule deliveries.')
        return redirect('procurement_list')

    proc = get_object_or_404(Procurement, pk=pk)
    if not user_can_access_branch(request.user, proc.branch_id):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('procurement_list')

    raw = request.POST.get('delivery_date', '').strip()
    try:
        updated = reschedule_delivery(proc, parse_date(raw) if raw else None)
    except (RescheduleError, ValueError) as exc:
        messages.error(request, str(exc) or 'Enter a valid delivery date.')
        return redirect('procurement_detail', pk=pk)

    notify_supplier(
        updated.supplier, PRIORITY_MEDIUM,
        f'{updated.po_number} — new delivery date',
        f'GERLYN moved the delivery of {updated.po_number} to '
        f'{updated.delivery_date:%Y-%m-%d}.',
        action_url=reverse('supplier_portal'), action_label='View order',
    )
    messages.success(
        request, f'{updated.po_number} rescheduled to {updated.delivery_date:%Y-%m-%d}.'
    )
    return redirect('procurement_detail', pk=pk)


@login_required(login_url='accounts:login')
def procurement_approve(request, pk):
    """The Owner's authorization step: Draft -> Approved or Incoming.

    Purchase orders raised by a Branch Manager are created as drafts and
    commit no spend until this runs (finding C-1). Kept as its own view
    rather than another branch of procurement_update_status because it is
    the only status change that writes an authorizer — a plain status bump
    must never be able to set owner/authorized_at.
    """
    proc = get_object_or_404(Procurement, pk=pk)

    # Role first: approving is the Owner's alone, and a Manager must not be
    # able to authorize their own order.
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can approve a purchase order.')
        return redirect('procurement_detail', pk=pk)

    # Then scope, before the transition check and the lock — same ordering as
    # procurement_update_status (finding C-6), so an out-of-scope request is
    # refused for being out of scope rather than for the order's status.
    if not _can_read_procurement(request.user, proc):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('procurement_list')

    if request.method != 'POST':
        return redirect('procurement_detail', pk=pk)

    with transaction.atomic():
        locked = Procurement.objects.select_for_update().get(pk=pk)

        # Draft is the only status an approval can act on, and that is exactly
        # what ALLOWED_TRANSITIONS already says: Draft is the one state whose
        # legal targets include Ordered. Reusing can_transition_to keeps this
        # endpoint on the same map as the other two (finding C-3) instead of
        # testing `status == DRAFT` on its own.
        if not locked.can_transition_to(Procurement.Status.APPROVED):
            messages.error(
                request,
                f'{locked.po_number} cannot be approved — it is already '
                f'{locked.status}.'
            )
            return redirect('procurement_detail', pk=pk)

        # Price and delivery date are negotiated with the supplier from
        # here — approval no longer snapshots a catalog cost or jumps
        # straight to Incoming.
        locked.status = Procurement.Status.APPROVED
        locked.owner = request.user
        locked.authorized_at = timezone.now()
        locked.save()

    messages.success(
        request,
        f'{proc.po_number} approved and is now {locked.status}.'
    )

    from .notifications import notify_po_approved

    # This approval is the moment the order first becomes real to the
    # supplier — supplier_portal excludes Draft POs entirely, so before now
    # they had no way to know this order existed.
    notify_po_approved(proc, request.user)

    return redirect('procurement_detail', pk=pk)


# =============================================================================
# NEGOTIATION — price and delivery date, agreed between the two sides on an
# Approved PO. Neither is ever set by whoever raises the order (procurement_
# add/procurement_approve above). See Procurement.Status and
# ALLOWED_TRANSITIONS for the full state shape:
#
#   Approved -> [supplier proposes] -> Pending Owner Review
#     -> [buyer accepts] -> Terms Agreed
#     -> [buyer counters] -> Pending Supplier Review
#       -> [supplier accepts] -> Terms Agreed
#       -> [supplier counters] -> Pending Owner Review (repeats)
#   Terms Agreed -> [supplier's own final confirmation, always required,
#                    even when THEY were the one who just accepted] -> Incoming
#
# Either side may cancel at any point via the existing procurement_update_
# status (Cancelled was never removed from ALLOWED_TRANSITIONS for these
# statuses — only the negotiation-progress targets need a dedicated view,
# since only those carry a price/date payload update_status has no form
# field for).
# =============================================================================

def _read_proposed_terms(request, proc):
    """Parse+validate a delivery date and one cost per real (non-unexpected)
    line from a negotiation POST. Returns (delivery_date, {item_id: cost})
    on success, or (None, [error strings]) — the caller tells the two
    apart by whether the first element is a string list or a date.
    """
    errors = []

    raw_date = request.POST.get('delivery_date', '').strip()
    try:
        delivery_date = parse_date(raw_date) if raw_date else None
    except ValueError:
        delivery_date = None
    if not delivery_date:
        errors.append('Choose a delivery date.')
    else:
        try:
            validate_expected_delivery_date(delivery_date)
        except ValidationError as exc:
            errors.append(f'Delivery date: {exc.messages[0]}')

    items = list(proc.items.filter(is_unexpected=False))
    costs = {}
    for item in items:
        raw_cost = request.POST.get(f'unit_cost_{item.pk}', '').strip()
        try:
            cost = Decimal(raw_cost)
        except (InvalidOperation, ValueError):
            cost = None
        if cost is None or cost <= 0:
            errors.append(
                f'Enter a valid cost for {item.product.product_name if item.product else "a removed product"}.'
            )
        else:
            costs[item.pk] = cost

    if errors:
        return None, errors
    return (delivery_date, costs), []


@login_required(login_url='accounts:login')
@require_POST
def procurement_supplier_respond(request, pk):
    """The supplier's side of the negotiation: submit an offer (the first
    proposal, or a counter to the buyer's own counter), or accept the
    buyer's counter as-is. Mirrors procurement_owner_respond below — kept
    as two views rather than one branching on role, since each side has a
    different set of legal source statuses and a different next status."""
    proc = get_object_or_404(Procurement, pk=pk)

    if request.user.role != User.Role.SUPPLIER or proc.supplier_id != request.user.id:
        messages.error(request, 'That purchase order is not available to you.')
        return _supplier_next_redirect(request)

    action = request.POST.get('action')

    with transaction.atomic():
        locked = Procurement.objects.select_for_update().get(pk=pk)

        if action == 'acknowledge':
            if locked.status != Procurement.Status.APPROVED:
                messages.error(
                    request,
                    f'{locked.po_number} is not waiting for an acknowledgement.',
                )
                return _supplier_next_redirect(request)
            if locked.supplier_acknowledged_at is None:
                locked.supplier_acknowledged_at = timezone.now()
                locked.save(update_fields=['supplier_acknowledged_at'])
            messages.success(
                request,
                f'{locked.po_number} acknowledged. You can submit delivery '
                f'and pricing terms when ready.',
            )
            return _supplier_next_redirect(request)

        if action == 'accept':
            if locked.status != Procurement.Status.PENDING_SUPPLIER_REVIEW:
                messages.error(
                    request,
                    f'{locked.po_number} is not waiting on your acceptance.'
                )
                return _supplier_next_redirect(request)
            locked.status = Procurement.Status.TERMS_AGREED
            locked.save(update_fields=['status'])
            messages.success(
                request,
                f'Terms agreed on {locked.po_number} — confirm below to start preparing it.'
            )
            return _supplier_next_redirect(request)

        if locked.status not in (
            Procurement.Status.APPROVED, Procurement.Status.PENDING_SUPPLIER_REVIEW,
        ):
            messages.error(request, f'{locked.po_number} is not open for a proposal.')
            return _supplier_next_redirect(request)

        parsed, errors = _read_proposed_terms(request, locked)
        if errors:
            for error in errors:
                messages.error(request, error)
            return _supplier_next_redirect(request)
        delivery_date, costs = parsed

        was_countering = locked.status == Procurement.Status.PENDING_SUPPLIER_REVIEW
        locked.proposed_delivery_date = delivery_date
        locked.proposed_by = request.user
        locked.status = Procurement.Status.PENDING_OWNER_REVIEW
        locked.save(update_fields=['proposed_delivery_date', 'proposed_by', 'status'])
        for item in locked.items.filter(is_unexpected=False):
            item.proposed_unit_cost = costs[item.pk]
            item.save(update_fields=['proposed_unit_cost'])

    messages.success(
        request,
        f'{locked.po_number}: your terms were sent to {locked.branch.branch_name} for review.'
    )
    from .notifications import notify_terms_for_buyer

    notify_terms_for_buyer(locked, request.user, countered=was_countering)
    return _supplier_next_redirect(request)


@login_required(login_url='accounts:login')
@require_POST
def procurement_owner_respond(request, pk):
    """The buyer's (Owner/Manager) side: accept the supplier's current
    proposal outright, or counter with different terms."""
    proc = get_object_or_404(Procurement, pk=pk)

    if request.user.role not in (User.Role.OWNER, User.Role.BRANCH_MANAGER):
        messages.error(request, 'You do not have permission to respond to this order.')
        return redirect('procurement_detail', pk=pk)
    if not _can_read_procurement(request.user, proc):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('procurement_list')

    action = request.POST.get('action')

    with transaction.atomic():
        locked = Procurement.objects.select_for_update().get(pk=pk)
        if locked.status != Procurement.Status.PENDING_OWNER_REVIEW:
            messages.error(request, f'{locked.po_number} is not waiting on your review.')
            return redirect('procurement_detail', pk=pk)

        if action == 'accept':
            locked.status = Procurement.Status.TERMS_AGREED
            locked.save(update_fields=['status'])
            messages.success(
                request,
                f'{locked.po_number}: terms accepted — waiting on the supplier '
                f'to confirm and start preparing it.'
            )
            from .notifications import notify_terms_for_supplier

            notify_terms_for_supplier(locked, request.user, agreed=True)
            return redirect('procurement_detail', pk=pk)

        parsed, errors = _read_proposed_terms(request, locked)
        if errors:
            for error in errors:
                messages.error(request, error)
            return redirect('procurement_detail', pk=pk)
        delivery_date, costs = parsed

        locked.proposed_delivery_date = delivery_date
        locked.proposed_by = request.user
        locked.status = Procurement.Status.PENDING_SUPPLIER_REVIEW
        locked.save(update_fields=['proposed_delivery_date', 'proposed_by', 'status'])
        for item in locked.items.filter(is_unexpected=False):
            item.proposed_unit_cost = costs[item.pk]
            item.save(update_fields=['proposed_unit_cost'])

    messages.success(
        request,
        f'{locked.po_number}: your counter-terms were sent to the supplier.'
    )
    from .notifications import notify_terms_for_supplier

    notify_terms_for_supplier(locked, request.user, agreed=False)
    return redirect('procurement_detail', pk=pk)


@login_required(login_url='accounts:login')
@require_POST
def procurement_supplier_confirm(request, pk):
    """The supplier's final, explicit commitment once terms are agreed —
    always required, even when the supplier's own acceptance was what
    reached Terms Agreed. Locks the negotiated price/date onto the order
    and moves it to Incoming."""
    proc = get_object_or_404(Procurement, pk=pk)

    if request.user.role != User.Role.SUPPLIER or proc.supplier_id != request.user.id:
        messages.error(request, 'That purchase order is not available to you.')
        return _supplier_next_redirect(request)

    with transaction.atomic():
        locked = Procurement.objects.select_for_update().get(pk=pk)
        if locked.status not in (
            Procurement.Status.TERMS_AGREED,
            Procurement.Status.COMMUNICATED,
        ):
            messages.error(request, f'{locked.po_number} is not ready to confirm.')
            return _supplier_next_redirect(request)

        if locked.status == Procurement.Status.TERMS_AGREED:
            try:
                apply_agreed_terms(locked)
            except ValidationError as exc:
                messages.error(
                    request,
                    f'Delivery date: {exc.messages[0]} Submit updated terms first.',
                )
                return redirect('supplier_portal')
        locked.status = Procurement.Status.INCOMING
        locked.supplier_acknowledged_at = timezone.now()
        locked.save(update_fields=['status', 'supplier_acknowledged_at'])

    messages.success(
        request,
        f'{locked.po_number} confirmed — delivery on {locked.delivery_date}.'
    )
    from .notifications import notify_incoming_delivery

    notify_incoming_delivery(locked, request.user)
    return _supplier_next_redirect(request)


@login_required(login_url='accounts:login')
@require_POST
def procurement_mark_communicated(request, pk):
    """Record an approved PO sent to a supplier outside the portal.

    Supplier login is intentionally not involved. Owner, Manager, or Staff
    with Deliveries edit access may record the call/message, agreed delivery
    date, and line costs; the PO then enters the receiving queue immediately.
    """
    if request.user.role not in (
        User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF,
    ):
        messages.error(request, 'You do not have permission to communicate this order.')
        return redirect('delivery_list')
    if not user_can_perform_module_action(request.user, DELIVERIES, EDIT):
        messages.error(request, 'You do not have permission to communicate this order.')
        return redirect('delivery_list')

    proc = get_object_or_404(
        procurements_visible_to(request.user, Procurement.objects.all()), pk=pk,
    )
    if not user_can_access_branch(request.user, proc.branch_id):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('delivery_list')

    parsed, errors = _read_proposed_terms(request, proc)
    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect('delivery_detail', pk=pk)
    delivery_date, costs = parsed

    try:
        with transaction.atomic():
            locked = Procurement.objects.select_for_update().get(pk=pk)
            record_external_order(
                locked,
                actor=request.user,
                delivery_date=delivery_date,
                unit_costs=costs,
                method=request.POST.get('communication_method', ''),
                note=request.POST.get('communication_note', ''),
            )
    except ExternalOrderError as exc:
        messages.error(request, str(exc))
        return redirect('delivery_detail', pk=pk)

    messages.success(
        request,
        f'{locked.po_number} marked Communicated/Ordered. It can now be '
        f'received without supplier portal acknowledgement.',
        extra_tags='sheet',
    )
    from .notifications import notify_incoming_delivery
    notify_incoming_delivery(locked, request.user)
    return redirect('delivery_detail', pk=pk)


# =============================================================================
# DELIVERIES — receive a purchase order into stock.
# Business rule 13: a procurement transaction updates branch inventory only
# after a formal receipt validation, so digital stock matches physical goods.
# =============================================================================

def _not_receivable_reason(proc):
    """Why this PO cannot be received, or None when it can.

    Receiving used to reject only an already-Received or Cancelled order, so
    a Draft one sailed through and credited warehouse stock for goods nobody
    had authorized buying. Harmless while Draft was a state almost nothing
    reached; with C-1 routing every Manager-raised PO through Draft it is the
    common case, so the rule is stated positively — Ordered is the only
    receivable status — instead of blacklisting the two known-bad ones.
    """
    if proc.status == Procurement.Status.RECEIVED:
        return f'{proc.po_number} was already received.'
    if proc.status == Procurement.Status.CANCELLED:
        return f'{proc.po_number} was cancelled.'
    if proc.status == Procurement.Status.DRAFT:
        return (
            f'{proc.po_number} is still in Draft and has not been approved — '
            f'it cannot be received until the Owner approves it.'
        )
    if proc.status in Procurement.NEGOTIATION_STATUSES or proc.status == Procurement.Status.APPROVED:
        return (
            f'{proc.po_number} has no agreed price or delivery date yet — '
            f'it cannot be received until the supplier and the buyer agree '
            f'on terms.'
        )
    if proc.status not in (
        Procurement.Status.COMMUNICATED,
        Procurement.Status.INCOMING,
        Procurement.Status.PARTIALLY_RECEIVED,
    ):
        return f'{proc.po_number} cannot be received while it is {proc.status}.'
    return None


class _DeliveryStatusError(Exception):
    """Raised inside delivery_receive's atomic block (already processed, or
    a bad/out-of-range received quantity) so the whole block rolls back
    instead of partially crediting warehouse stock for a rejected receipt."""


def _receiving_exit(request, proc):
    """Where the receiving flow sends someone once it's done with a PO.

    The PO's own page when they can open it. Otherwise the Deliveries list,
    on the tab the PO now sits in: procurement_detail is gated to the
    PROCUREMENT module, which default Staff accounts don't have (only
    DELIVERIES), so sending them there bounced every receipt to a "no access"
    page instead of showing the result.
    """
    from urllib.parse import urlencode

    from core.module_permissions import PROCUREMENT, user_can_access_module

    if user_can_access_module(request.user, PROCUREMENT):
        return redirect('procurement_detail', pk=proc.pk)
    return redirect(f"{reverse('delivery_list')}?{urlencode({'status': proc.status})}")


def _safe_nonneg_int(value):
    """int(value) if it's a clean non-negative integer, else None."""
    if value is None:
        return None
    value = str(value).strip()
    return int(value) if value.isdigit() else None

@login_required(login_url='accounts:login')
def delivery_list(request):
    """Every purchase order that has left Draft — Ordered, Received or
    Cancelled — as one filter-tabs + table page, same pattern as Purchase
    Orders and Billing. Receiving History used to be a separate page; it's
    now just the Received tab here."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    deliveries = procurements_visible_to(
        request.user,
        Procurement.objects.select_related('branch', 'supplier')
        # actual_received_cost/unrecorded_cost_item_count (rendered per row
        # for Received/Partially Received) walk procurement.items.all() —
        # without this, that's a fresh query per row on this list.
        .prefetch_related('items')
        .exclude(status=Procurement.Status.DRAFT),
    )
    staff_delivery_view = request.user.role == User.Role.STAFF

    user_branch = branch_for_user(request.user)
    can_access_multiple_branches = branch_ids_for_user(request.user) is None

    search_query = request.GET.get('q', '')
    if search_query:
        deliveries = deliveries.filter(
            Q(po_number__icontains=search_query) |
            Q(supplier__supplier_profile__supplier_name__icontains=search_query)
        )

    # Owner-only branch filter — Manager/Staff are already pinned to their own
    # branch's deliveries by scope_queryset() above, so a selector would do
    # nothing for them.
    branch_filter = (
        request.GET.get('branch', '') if can_access_multiple_branches else ''
    )
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''
    if branch_filter:
        deliveries = deliveries.filter(branch_id=branch_filter)

    # The shared back-office range control (_daterange.html), same as
    # Purchase Orders/Billing. order_date is a plain DateField (no timezone
    # conversion needed, unlike a DateTimeField — see core.timebounds).
    range_start, range_end, active_range = range_from_request(request)
    if range_start:
        deliveries = deliveries.filter(order_date__gte=range_start)
    deliveries = deliveries.filter(order_date__lte=range_end)

    deliveries = deliveries.order_by('-order_date')

    status_filter = request.GET.get('status', '')
    if staff_delivery_view and status_filter not in (
        Procurement.Status.APPROVED,
        'Negotiating',
        Procurement.Status.COMMUNICATED,
        Procurement.Status.INCOMING,
        Procurement.Status.PARTIALLY_RECEIVED,
        Procurement.Status.RECEIVED,
    ):
        status_filter = ''
    counts = {
        'all': deliveries.count(),
        'active': deliveries.filter(
            status__in=STAFF_PENDING_DELIVERY_STATUSES
        ).count(),
        'approved': deliveries.filter(status=Procurement.Status.APPROVED).count(),
        'communicated': deliveries.filter(
            status=Procurement.Status.COMMUNICATED
        ).count(),
        'negotiating': deliveries.filter(
            status__in=Procurement.NEGOTIATION_STATUSES
        ).count(),
        # See procurement_list's matching count — the negotiating slice
        # that's actually the buyer's turn to respond to.
        'pending_owner_review': deliveries.filter(
            status=Procurement.Status.PENDING_OWNER_REVIEW
        ).count(),
        'incoming': deliveries.filter(status=Procurement.Status.INCOMING).count(),
        'partially_received': deliveries.filter(
            status=Procurement.Status.PARTIALLY_RECEIVED
        ).count(),
        'received': deliveries.filter(status=Procurement.Status.RECEIVED).count(),
        'cancelled': deliveries.filter(status=Procurement.Status.CANCELLED).count(),
    }
    if staff_delivery_view and not status_filter:
        deliveries = deliveries.filter(status__in=STAFF_PENDING_DELIVERY_STATUSES)
    elif status_filter == 'Negotiating':
        deliveries = deliveries.filter(status__in=Procurement.NEGOTIATION_STATUSES)
    elif status_filter:
        deliveries = deliveries.filter(status=status_filter)

    from branches.models import Branch
    branches = Branch.objects.all() if can_access_multiple_branches else []

    # Receive Delivery is a modal on this list now (see delivery_receive's
    # GET branch, which is what actually lands here with ?receive=<pk> —
    # that view has already done the branch-access and status checks, so
    # this just re-resolves the same PO to render. A tampered/stale
    # ?receive= (wrong branch, already received) is treated the same as not
    # being there at all: no modal, no error — the list itself is still a
    # valid page to be looking at.
    receive_procurement = None
    receive_items = None
    addable_products = None
    can_receive = user_can_perform_module_action(request.user, DELIVERIES, EDIT)
    receive_draft = None
    receive_submission_key = None
    receive_pk = request.GET.get('receive')
    if can_receive and receive_pk and receive_pk.isdigit():
        candidate = procurements_visible_to(
            request.user,
            Procurement.objects.select_related('branch', 'supplier'),
        ).filter(
            pk=receive_pk,
            status__in=[
                Procurement.Status.COMMUNICATED,
                Procurement.Status.INCOMING,
                Procurement.Status.PARTIALLY_RECEIVED,
            ],
        ).first()
        if candidate and user_can_access_branch(request.user, candidate.branch_id):
            receive_procurement = candidate
            receive_items = candidate.items.select_related('product').all()
            # The "add unexpected item" picker. Excludes what's already on
            # the order, so the only thing it can offer is a genuinely new line.
            addable_products = Product.objects.exclude(
                pk__in=receive_items.values_list('product_id', flat=True)
            ).order_by('product_name')
            # A receipt that failed validation comes back here with what was
            # typed (see _stash_receive_draft), so the modal reopens with the
            # user's counts rather than the defaults. Popped: shown once.
            receive_draft = request.session.pop(_receive_draft_key(candidate.pk), None)
            if receive_draft:
                draft_rows = receive_draft.get('rows') or {}
                receive_items = list(receive_items)
                for item in receive_items:
                    item.draft = draft_rows.get(str(item.pk))
            session_key = f'receive_submission_key_{candidate.pk}'
            receive_submission_key = request.session.get(session_key)
            if not receive_submission_key:
                receive_submission_key = uuid.uuid4().hex
                request.session[session_key] = receive_submission_key

    return render(request, 'procurement/delivery_list.html', {
        'deliveries': deliveries,
        'counts': counts,
        'status_filter': status_filter,
        'today': timezone.localdate(),
        'search_query': search_query,
        'branches': branches,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Warehouse Stock, Product Master List and Stock Adjustments.
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        'user_branch': user_branch,
        # _daterange.html: a preset click must not drop the search/filters.
        'active_range': active_range,
        'range_start': range_start or range_end,
        'range_end': range_end,
        'daterange_carry': [('q', search_query), ('status', status_filter), ('branch', branch_filter)],
        # O-3 — one urlencoded filter string for the status tabs, same fix as
        # Purchase Orders/Billing: a tab's href used to be hand-built and
        # silently dropped other filters.
        'filter_qs': list_filter_querystring(q=search_query, branch=branch_filter),
        # Every other active filter, for the topbar branch switcher
        # (partials/_branch_switch.html) to carry along — same idea as
        # _daterange.html's daterange_carry.
        'branch_switch_carry': [('q', search_query), ('status', status_filter)],
        'receive_procurement': receive_procurement,
        'receive_items': receive_items,
        'addable_products': addable_products,
        'show_receive_modal': receive_procurement is not None,
        'can_receive': can_receive,
        'can_add_unexpected': user_can_perform_module_action(
            request.user, DELIVERIES, ADD
        ),
        'staff_delivery_view': staff_delivery_view,
        'show_branch_column': can_access_multiple_branches,
        'receive_draft': receive_draft,
        'receive_submission_key': receive_submission_key,
    })


def _receive_draft_key(pk):
    return f'receive_draft_{pk}'


def _stash_receive_draft(request, pk):
    """Keep a rejected receipt's typed values for the reopened Receive modal.

    Only what the form showed — nothing here is trusted or saved; the next
    Confirm Receipt posts and validates it all again.
    """
    item_ids = request.POST.getlist('item_id')
    qtys = request.POST.getlist('quantity_received')
    expiries = request.POST.getlist('expiry_date')
    rows = {}
    for idx, item_id in enumerate(item_ids):
        rows[str(item_id)[:20]] = {
            'qty': (qtys[idx] if idx < len(qtys) else '').strip()[:20],
            'expiry': (expiries[idx] if idx < len(expiries) else '').strip()[:10],
        }
    receipt_date = request.POST.get('actual_receipt_date', '').strip()
    try:
        valid_date = bool(receipt_date) and parse_date(receipt_date) is not None
    except ValueError:
        valid_date = False
    request.session[_receive_draft_key(pk)] = {
        'rows': rows,
        # An unparseable date would only blank the date input; the default
        # (today) is the better thing to reopen with.
        'date': receipt_date if valid_date else '',
        'delivered_by': request.POST.get('delivered_by', '').strip()[:100],
    }


@login_required(login_url='accounts:login')
def delivery_receive(request, pk):
    """Record the quantities actually delivered against a PO.

    The form itself is rendered inline by delivery_list/_receive_modal.html
    (as a modal, opened via ?receive=<pk> — see that view). This view does
    the branch/status guards and, on GET, redirects there with that param
    set; the POST branch (an explicit form action, not the current URL) is
    unchanged.
    """
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    proc = get_object_or_404(
        procurements_visible_to(
            request.user,
            Procurement.objects.select_related('branch', 'supplier'),
        ),
        pk=pk,
    )

    if not user_can_access_branch(request.user, proc.branch_id):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('delivery_list')

    if proc.status == Procurement.Status.RECEIVED:
        messages.info(request, f'{proc.po_number} was already received.')
        return _receiving_exit(request, proc)
    unreceivable = _not_receivable_reason(proc)
    if unreceivable:
        messages.error(request, unreceivable)
        return _receiving_exit(request, proc)

    if request.method == 'POST':
        receipt_date_raw = request.POST.get('actual_receipt_date', '').strip()
        try:
            receipt_date = parse_date(receipt_date_raw) if receipt_date_raw else timezone.localdate()
        except ValueError:
            receipt_date = None
        if receipt_date is None:
            messages.error(request, 'Enter a valid actual receipt date.')
            _stash_receive_draft(request, pk)
            return redirect('delivery_receive', pk=pk)
        if receipt_date > timezone.localdate():
            messages.error(request, 'Actual receipt date cannot be in the future.')
            _stash_receive_draft(request, pk)
            return redirect('delivery_receive', pk=pk)

        rejected = request.POST.get('delivery_outcome') == 'rejected'
        lines = {}
        try:
            if not rejected:
                item_ids = request.POST.getlist('item_id')
                quantities = request.POST.getlist('quantity_received')
                expiries = request.POST.getlist('expiry_date')
                if len(item_ids) != len(quantities) or len(set(item_ids)) != len(item_ids):
                    raise DeliveryError('The submitted delivery lines are incomplete or duplicated.')
                for index, (item_id, quantity_text) in enumerate(zip(item_ids, quantities)):
                    if not str(item_id).isdigit():
                        raise DeliveryError('One or more submitted delivery lines are invalid.')
                    quantity = _safe_nonneg_int(quantity_text)
                    if quantity is None:
                        raise DeliveryError('Enter whole-number received quantities of zero or more.')
                    expiry_text = expiries[index].strip() if index < len(expiries) else ''
                    try:
                        expiry = parse_date(expiry_text) if expiry_text else None
                    except ValueError as exc:
                        raise DeliveryError('Enter a valid expiry date.') from exc
                    if expiry_text and expiry is None:
                        raise DeliveryError('Enter a valid expiry date.')
                    lines[int(item_id)] = (quantity, expiry)

            submission_key = (
                request.POST.get('submission_key', '').strip()
                or request.session.get(f'receive_submission_key_{pk}')
                or uuid.uuid4().hex
            )
            receipt, updated, duplicate = record_delivery(
                proc, request.user, lines=lines, receipt_date=receipt_date,
                delivered_by=request.POST.get('delivered_by', ''),
                idempotency_key=submission_key,
                rejection_reason=request.POST.get('rejection_reason', '') if rejected else '',
            )
        except DeliveryError as exc:
            messages.error(request, str(exc))
            _stash_receive_draft(request, pk)
            return redirect('delivery_receive', pk=pk)

        request.session.pop(f'receive_submission_key_{pk}', None)
        if duplicate:
            messages.info(request, 'This delivery submission was already recorded; stock was not changed again.')
        elif receipt.outcome == DeliveryReceipt.Outcome.REJECTED:
            messages.warning(request, f'Failed/rejected delivery recorded for {updated.po_number}. No stock was added.')
        elif updated.status == Procurement.Status.PARTIALLY_RECEIVED:
            messages.warning(request, f'{updated.po_number} was partially received. Remaining quantities stay open.')
        else:
            messages.success(request, f'{updated.po_number} received in full. Warehouse stock updated.', extra_tags='sheet')
        return _receiving_exit(request, updated)

        # Kept below as unreachable historical context while callers migrate
        # to the shared record_delivery service.
        # A received delivery stocks the WAREHOUSE, not the branch floor —
        # Inventory (floor stock) only increases via an approved, released
        # withdrawal slip (see transfer_approve). This is what the
        # Inventory model's own docstring already documents; delivery_receive
        # previously credited floor stock directly, silently orphaning the
        # Warehouse -> StockTransfer -> Floor pipeline.
        warehouse = Warehouse.objects.filter(branch=proc.branch).first()
        if not warehouse:
            messages.error(
                request,
                f'No warehouse is configured for {proc.branch.branch_name} — cannot receive stock.'
            )
            return redirect('delivery_list')

        received_map = request.POST.getlist('quantity_received')
        item_ids = request.POST.getlist('item_id')
        # One expiry_date input per table row, same DOM order as item_ids —
        # optional per line, since not every product's lot needs tracking.
        expiry_map = request.POST.getlist('expiry_date')
        receipt_date_raw = request.POST.get('actual_receipt_date', '').strip()
        receipt_date = (
            parse_date(receipt_date_raw) if receipt_date_raw
            else timezone.localdate()
        )
        if receipt_date_raw and receipt_date is None:
            messages.error(request, 'Enter a valid actual receipt date.')
            _stash_receive_draft(request, pk)
            return redirect('delivery_receive', pk=pk)

        try:
            with transaction.atomic():
                # Re-fetch and lock so a double-submit (double-click, retry)
                # can't both pass the RECEIVED/CANCELLED guard above (read
                # before this transaction opened) and both credit warehouse
                # stock for the same delivery.
                locked_proc = get_object_or_404(
                    Procurement.objects.select_for_update(), pk=pk
                )
                locked_unreceivable = _not_receivable_reason(locked_proc)
                if locked_unreceivable:
                    raise _DeliveryStatusError(locked_unreceivable)

                # Created up front (not after the loop) so each item's
                # DeliveryReceiptLine below can point at it; a raise anywhere
                # in this block rolls the whole atomic transaction back,
                # including this row, so an empty/invalid submission never
                # leaves an orphaned receipt.
                receipt = DeliveryReceipt.objects.create(
                    procurement=locked_proc,
                    received_by=request.user,
                    receipt_date=receipt_date,
                    delivered_by=request.POST.get('delivered_by', '').strip()[:100],
                )

                any_received = False
                for idx, (item_id, qty_str) in enumerate(zip(item_ids, received_map)):
                    item = locked_proc.items.select_related('product').get(pk=item_id)
                    expiry_str = expiry_map[idx].strip() if idx < len(expiry_map) else ''
                    qty_received = _safe_nonneg_int(qty_str)
                    if qty_received is None:
                        raise _DeliveryStatusError(
                            f'Enter a valid received quantity for {item.product.product_name}.'
                        )
                    # A quantity greater than what was ordered is rejected
                    # rather than silently inflating stock — under-delivery
                    # is a normal short shipment (flagged below), but
                    # over-delivery past what was ordered needs a human to
                    # look at it, not a silent accept.
                    #
                    # Unexpected items are the sanctioned way to record goods
                    # nobody ordered: they carry quantity_ordered=0 by
                    # construction, so this guard would reject every one of
                    # them. They are flagged as a discrepancy below instead,
                    # which is the "a human looks at it" the guard is for.
                    remaining = max(
                        item.quantity_ordered - item.quantity_received, 0
                    )
                    if not item.is_unexpected and qty_received > remaining:
                        raise _DeliveryStatusError(
                            f'Received quantity for {item.product.product_name} '
                            f'({qty_received}) cannot exceed the remaining quantity '
                            f'({remaining}). If this product arrived '
                            f'without being ordered, add it as an unexpected item.'
                        )

                    if qty_received <= 0:
                        continue

                    any_received = True
                    item.quantity_received += qty_received
                    item.save(update_fields=['quantity_received'])

                    ws, _ = WarehouseStock.objects.select_for_update().get_or_create(
                        product=item.product, warehouse=warehouse,
                        defaults={'quantity_on_hand': 0, 'reorder_level': 0},
                    )
                    before = ws.quantity_on_hand
                    ws.quantity_on_hand += qty_received
                    ws.save()

                    # Audit trail — every stock change is attributable.
                    StockMovement.objects.create(
                        warehouse_stock=ws,
                        location_type=StockMovement.LocationType.WAREHOUSE,
                        movement_type=StockMovement.MovementType.PROCUREMENT_RECEIVED,
                        quantity_change=qty_received,
                        quantity_before=before,
                        quantity_after=ws.quantity_on_hand,
                        reference_id=locked_proc.pk,
                        notes=f'Delivery for {locked_proc.po_number}',
                        created_by=request.user,
                    )

                    # A new Batch, always — even a blank expiry_date is its
                    # own lot rather than being folded into an existing
                    # batch that happens to share one. Two deliveries of the
                    # same product landing the same week are still two
                    # separate lots; batches only merge when FEFO actually
                    # ties them together at consumption time, never here.
                    Batch.objects.create(
                        product=item.product,
                        branch=locked_proc.branch,
                        location=Batch.Location.WAREHOUSE,
                        procurement=locked_proc,
                        quantity=qty_received,
                        received_date=receipt_date,
                        expiry_date=parse_date(expiry_str) if expiry_str else None,
                    )

                    # Which receipt this quantity came in on — the piece that
                    # was missing for a multi-shipment PO (see DeliveryReceiptLine's
                    # own docstring).
                    DeliveryReceiptLine.objects.create(
                        receipt=receipt,
                        item=item,
                        quantity_received=qty_received,
                        expiry_date=parse_date(expiry_str) if expiry_str else None,
                    )

                if not any_received:
                    raise _DeliveryStatusError(
                        'Enter at least one received quantity greater than zero.'
                    )

                has_remaining = locked_proc.items.filter(
                    is_unexpected=False,
                    quantity_received__lt=F('quantity_ordered'),
                ).exists()
                has_unexpected = locked_proc.items.filter(
                    is_unexpected=True, quantity_received__gt=0,
                ).exists()
                locked_proc.status = (
                    Procurement.Status.PARTIALLY_RECEIVED
                    if has_remaining else Procurement.Status.RECEIVED
                )
                locked_proc.receipt_flag = (
                    Procurement.ReceiptFlag.PARTIAL
                    if has_remaining else (
                        Procurement.ReceiptFlag.DISCREPANCY
                        if has_unexpected else Procurement.ReceiptFlag.NONE
                    )
                )
                locked_proc.actual_receipt_date = receipt_date
                locked_proc.save(update_fields=[
                    'status', 'receipt_flag', 'actual_receipt_date',
                ])
        except _DeliveryStatusError as e:
            messages.error(request, str(e))
            # A count problem (too many, none, not a number) is fixed in the
            # Receive modal, so reopen it (delivery_receive's GET does) rather
            # than leave the receiver on a page they'd have to navigate back
            # from. A PO that stopped being receivable goes where it always
            # would once finished.
            proc.refresh_from_db(fields=['status'])
            if _not_receivable_reason(proc):
                return _receiving_exit(request, proc)
            _stash_receive_draft(request, pk)
            return redirect('delivery_receive', pk=pk)

        if has_remaining:
            messages.warning(
                request,
                f'{proc.po_number} was partially received. The remaining '
                f'quantities stay in Incoming Deliveries for the next shipment.'
            )
            from core.module_permissions import DELIVERIES
            from core.notifications import (
                CATEGORY_PROCUREMENT, PRIORITY_HIGH, notify, notify_supplier,
            )

            notify(
                CATEGORY_PROCUREMENT, PRIORITY_HIGH,
                f'{proc.po_number} received with a discrepancy',
                f'{proc.po_number} from {proc.supplier} ({proc.branch.branch_name}) '
                f'was received with delivered quantities that differ from what '
                f'was ordered.',
                branch=proc.branch,
                action_url=reverse('procurement_detail', kwargs={'pk': proc.pk}),
                action_label='View PO',
                required_module=DELIVERIES,
            )
            notify_supplier(
                proc.supplier, PRIORITY_HIGH,
                f'{proc.po_number} — delivery discrepancy',
                f'{proc.branch.branch_name} recorded different quantities '
                f'from what was ordered on {proc.po_number}. Please check '
                f'this delivery with them.',
                action_url=reverse('supplier_portal'),
                action_label='View order',
            )
        else:
            messages.success(
                request,
                f'{proc.po_number} received in full. Warehouse stock updated — '
                f'create a Withdrawal Slip to move it onto the floor.',
                extra_tags='sheet',
            )
            from core.notifications import PRIORITY_LOW, notify_supplier

            notify_supplier(
                proc.supplier, PRIORITY_LOW,
                f'{proc.po_number} delivered & received',
                f'{proc.branch.branch_name} confirmed receipt of {proc.po_number} '
                f'in full.',
                action_url=reverse('supplier_portal'),
                action_label='View order',
            )
        proc.refresh_from_db(fields=['status'])
        return _receiving_exit(request, proc)

    # Receive Delivery is a modal on the Receive Delivery list now — a
    # direct hit (bookmark, the "Receive" link on a dashboard, the back
    # button) lands there with this PO's modal already open. See
    # delivery_list/_receive_modal.html.
    return redirect(f"{reverse('delivery_list')}?receive={pk}")


@login_required(login_url='accounts:login')
@require_POST
def delivery_scan(request, pk):
    """AJAX: resolve one scanned barcode against this purchase order's lines.

    POST rather than GET despite reading like a lookup — every call writes a
    DeliveryScanLog row, and the point of that log is that it records what
    actually happened at the loading bay, including scans on a receipt that
    was later abandoned. Batching the log until submit would lose exactly the
    cases worth having it for.

    Never mutates quantity_received. The running count lives in the form until
    the user submits it, so a mis-scan is fixed by editing the field like any
    other typo — the log keeps the mis-scan, the receipt keeps the correction.
    """
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        return JsonResponse({'ok': False, 'detail': 'Access denied.'}, status=403)

    proc = get_object_or_404(
        procurements_visible_to(request.user, Procurement.objects.all()), pk=pk
    )
    if not user_can_access_branch(request.user, proc.branch_id):
        return JsonResponse({'ok': False, 'detail': 'Wrong branch.'}, status=403)
    if proc.status not in Procurement.AWAITING_DELIVERY_STATUSES:
        return JsonResponse(
            {'ok': False, 'detail': 'This order is not ready for receiving.'},
            status=400,
        )

    code = (request.POST.get('code') or '').strip()
    if not code:
        return JsonResponse({'ok': False, 'detail': 'No code supplied.'}, status=400)

    # Same resolution order as inventory.barcode_lookup — real barcode first,
    # then our own SKU, which covers products that never got a manufacturer
    # code assigned.
    product = (
        Product.objects.filter(barcode__iexact=code).first()
        or Product.objects.filter(sku_code__iexact=code).first()
    )

    item = None
    if product:
        item = proc.items.filter(product=product).first()

    DeliveryScanLog.objects.create(
        procurement=proc,
        procurement_item=item,
        scanned_code=code,
        matched_product=product,
        was_unexpected=item is None,
        scanned_by=request.user,
    )

    if item:
        return JsonResponse({
            'ok': True, 'status': 'matched',
            'item_id': item.pk,
            'product_name': item.product.product_name,
            'quantity_ordered': item.quantity_ordered,
            'quantity_remaining': item.remaining_quantity,
        })

    if product:
        # Real product, wrong order. Distinguished from an unknown code
        # because the fix differs: this one can be added to the PO in a
        # click, the other needs someone to identify the goods first.
        return JsonResponse({
            'ok': True, 'status': 'not_on_order',
            'product_id': product.pk,
            'product_name': product.product_name,
            'sku_code': product.sku_code,
        })

    return JsonResponse({'ok': True, 'status': 'unknown', 'code': code})


@login_required(login_url='accounts:login')
@require_POST
def delivery_add_unexpected(request, pk):
    """Add a product that arrived but wasn't on the purchase order.

    It becomes a real line so the receipt reflects what physically turned up,
    but with quantity_ordered=0 and is_unexpected=True — the pair says
    "nobody ordered this" without hiding that it arrived. The receiving
    screen's over-delivery guard skips these rows for exactly that reason.
    """
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    proc = get_object_or_404(
        procurements_visible_to(request.user, Procurement.objects.all()), pk=pk
    )
    if not user_can_access_branch(request.user, proc.branch_id):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('delivery_list')
    if proc.status not in Procurement.AWAITING_DELIVERY_STATUSES:
        messages.error(request, f'{proc.po_number} is not ready for receiving.')
        return _receiving_exit(request, proc)

    product = get_object_or_404(Product, pk=request.POST.get('product'))

    if proc.items.filter(product=product).exists():
        messages.info(request, f'{product.product_name} is already on this order.')
        return redirect('delivery_receive', pk=pk)

    # Best-effort cost: what this supplier normally charges, if we know it.
    # Zero otherwise — an unpriced line is honest about being unpriced, and
    # guessing a cost would quietly corrupt the order total.
    link = SupplierProduct.objects.filter(
        supplier=proc.supplier, product=product
    ).first()

    ProcurementItem.objects.create(
        procurement=proc,
        product=product,
        quantity_ordered=0,
        quantity_received=0,
        unit_cost_snapshot=(
            link.current_cost_price
            if link and link.current_cost_price is not None else None
        ),
        is_unexpected=True,
    )
    messages.success(
        request,
        f'{product.product_name} added as an unexpected item. Enter the quantity '
        f'that arrived, then confirm the receipt.'
    )
    return redirect('delivery_receive', pk=pk)


# =============================================================================
# SUPPLIER PORTAL
# The Supplier is one of the five documented roles. Per the Context Diagram they
# "receive procurement requests and provide delivery information" — so they see
# their own orders and can commit to a delivery date. They deliberately CANNOT
# mark an order Received: that is the store's receipt validation (business
# rule 13), and letting a supplier self-confirm would defeat the control.
#
# Three pages now (Dashboard / Purchase orders / Deliveries), each its own URL
# and template, sharing one sidebar+topbar shell (templates/procurement/
# supplier_base.html). The guard below and the two helpers after it are what
# all three views (and nothing else) are built from, so the sidebar badges,
# the Products & pricing modal, and the underlying order queryset can never
# drift out of sync between pages the way hand-copying them three times
# would eventually let happen.
# =============================================================================

def _supplier_only(view_func):
    """Same guard the original single-page supplier_portal view used —
    factored out so all three page views below share it verbatim rather
    than each re-declaring the same three lines."""
    @wraps(view_func)
    @login_required(login_url='accounts:login')
    def _wrapped(request, *args, **kwargs):
        if request.user.role != User.Role.SUPPLIER:
            messages.error(request, 'Access denied.')
            return redirect('accounts:home')
        return view_func(request, *args, **kwargs)
    return _wrapped


def _supplier_next_redirect(request, fallback='supplier_portal'):
    """Where a supplier POST (respond/confirm, save cost, offer a product,
    acknowledge/reject a return, ...) sends them back to, now that those
    actions can be submitted from any of the three portal pages instead of
    always the one dashboard. Every such form carries a hidden `next` set to
    the page it was submitted from (see the `next_url` passed into
    _supplier_po_detail.html and each page's own forms); validated the same
    way any external-redirect-target field must be, falling back to the
    dashboard when absent or unsafe."""
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(fallback)


def _supplier_orders_qs(user):
    """Base queryset every supplier page's order list/table is built from —
    Drafts excluded (an order the Owner hasn't approved yet is not a
    commitment to this supplier and must never appear in their portal)."""
    return Procurement.objects.filter(
        supplier=user
    ).exclude(
        status=Procurement.Status.DRAFT
    ).select_related('branch').prefetch_related('items__product').order_by('-order_date')


def _supplier_greeting(now, user, profile):
    """Dashboard <h1> — same wording and time-of-day cutoffs as the Admin
    (Owner/Manager/Staff) Dashboard's own greeting
    (reports/views.py::_dashboard_greeting, rendered in
    templates/reports/dashboard.html), so a supplier logging in sees the
    same "Good morning/afternoon/evening, {name}!" convention instead of a
    hardcoded phrase found nowhere else in the app. Not imported directly:
    that function's name resolution (first name -> full name -> username)
    has no seam for a supplier's one extra fallback below, and reaching
    into reports/views.py to add one would mean touching the Admin
    dashboard's own file, which this change is explicitly not to do. The
    cutoffs (noon, 6pm) and the exact format string are kept byte-for-byte
    identical on purpose.
    """
    hour = now.hour
    if hour < 12:
        part = 'morning'
    elif hour < 18:
        part = 'afternoon'
    else:
        part = 'evening'
    # first name -> full name -> supplier/company name -> username. The
    # extra rung Admin's own chain doesn't have: a supplier account is a
    # company, not a person, so it usually has no first/last name at all.
    name = (
        user.first_name.strip() or user.get_full_name().strip()
        or (profile.supplier_name if profile and profile.supplier_name else '')
        or user.username
    )
    return f'Good {part}, {name}!'


def _supplier_shared_context(request):
    """Everything the shared shell (supplier_base.html) needs on every
    page: sidebar badges, the avatar/greeting name, and the Products &
    pricing modal's data. Split out of the old single-view supplier_portal
    so the three page views below can't drift out of sync with each other —
    each calls this once, then adds only what its own page needs beyond it.
    """
    user = request.user
    today = timezone.now().date()

    orders = _supplier_orders_qs(user)
    counts = {
        'all': orders.count(),
        'needs_action': orders.filter(
            status__in=Procurement.SUPPLIER_ACTION_STATUSES
        ).count(),
        'incoming': orders.filter(
            status__in=Procurement.AWAITING_DELIVERY_STATUSES
        ).count(),
        'received': orders.filter(status=Procurement.Status.RECEIVED).count(),
        'cancelled': orders.filter(status=Procurement.Status.CANCELLED).count(),
    }
    # A Draft PO hasn't been placed yet — the supplier should never see it.
    orders = orders.exclude(status=Procurement.Status.DRAFT)

    # The stat strip is a global summary of the supplier's whole order book,
    # not a summary of the current tab — so it's computed from `orders`
    # before the status filter below narrows it down for the list. Computing
    # it after the filter (as this used to) meant the Delivered/Cancelled
    # tabs had no Ordered rows left to summarize, and every stat silently
    # collapsed to 0 / ₱0.00 the moment you weren't on All or To fulfil.
    today = timezone.localdate()
    # Committed but not yet finished — includes the negotiation statuses now:
    # a PO mid-negotiation is exactly as "open" as one already Incoming, and
    # used to silently fall out of this count the moment it left Approved.
    # Committed but not yet finished — includes the negotiation statuses:
    # a PO mid-negotiation is exactly as "open" as one already Incoming.
    open_orders = [
        o for o in orders if o.status in Procurement.COMMITTED_OPEN_STATUSES
    ]
    open_value = sum(o.total_cost for o in open_orders)
    overdue = sum(
        1 for o in open_orders if o.delivery_date and o.delivery_date < today
    )

    # A real DB count now (status=Requested), not the client-side row-count
    # the single-page version used — that only worked because the Returns
    # table was guaranteed to be on the same page as the sidebar; now it
    # lives on Deliveries only, so the Dashboard and Purchase orders pages
    # need this number without rendering that table at all.
    returns_awaiting = user.supplier_returns.filter(
        status=SupplierReturn.Status.REQUESTED
    ).count()

    catalog_links = SupplierProduct.objects.filter(
        supplier=user
    ).select_related('product').order_by('product__product_name')

    # The "Offer a product" picker — Product Master List minus what this
    # supplier already has a row for (any status), so neither picker can
    # bounce off the unique constraint. When the supplier's category is linked
    # to product categories the list starts narrowed to those; ?all_products=1
    # widens it. A convenience only: any master-list product may be offered.
    profile = getattr(user, 'supplier_profile', None)
    available_products = Product.objects.exclude(
        pk__in=catalog_links.values_list('product_id', flat=True)
    ).order_by('product_name')
    suggested_category_ids = (
        list(profile.category.product_categories.values_list('pk', flat=True))
        if profile and profile.category_id else []
    )
    narrowed = bool(suggested_category_ids) and not request.GET.get('all_products')
    if narrowed:
        available_products = available_products.filter(
            category_id__in=suggested_category_ids
        )

    return {
        'greeting': _supplier_greeting(timezone.localtime(), user, profile),
        'counts': counts,
        'open_count': len(open_orders),
        'open_value': open_value,
        'overdue': overdue,
        'returns_awaiting': returns_awaiting,
        # One number for the Deliveries sidebar badge — overdue deliveries
        # plus returns waiting on this supplier, exactly what item 2 of the
        # nav-badge fix asked the badge to show, just server-computed now.
        'deliveries_badge': overdue + returns_awaiting,
        'today': today,
        'catalog_links': catalog_links,
        'catalog_count': catalog_links.count(),
        'available_products': available_products,
        'products_narrowed': narrowed,
        'product_requests': user.product_requests.exclude(
            status=SupplierProductRequest.Status.WITHDRAWN
        )[:20],
        'unit_choices': Product.UnitOfMeasure.choices,
        # Passed through so each page's own per-row checks (which order
        # gets a red "(overdue)" label, which gets a primary "Respond"
        # button vs. a plain "View" one) read the same sets this context's
        # own counts/filters above are built from, rather than hand-copying
        # the status list a third time.
        'awaiting_delivery_statuses': Procurement.AWAITING_DELIVERY_STATUSES,
        'supplier_action_statuses': Procurement.SUPPLIER_ACTION_STATUSES,
        'profile': profile,
        'notif_unread_count': user.notifications.filter(is_read=False).count(),
    }


@_supplier_only
def supplier_portal(request):
    """Dashboard — a summary only. The full filterable order table lives on
    supplier_purchase_orders now, and the Returns table on
    supplier_deliveries; this page just previews the top few of each."""
    ctx = _supplier_shared_context(request)
    orders = _supplier_orders_qs(request.user)
    ctx.update({
        'recent_orders': orders[:5],
        'needs_response_orders': orders.filter(
            status__in=Procurement.SUPPLIER_ACTION_STATUSES
        )[:3],
        'upcoming_orders': orders.filter(
            status__in=Procurement.AWAITING_DELIVERY_STATUSES,
            delivery_date__isnull=False,
        ).order_by('delivery_date')[:3],
    })
    return render(request, 'procurement/supplier_portal.html', ctx)


@_supplier_only
def supplier_purchase_orders(request):
    """All of a supplier's purchase orders — the full table the Dashboard
    used to carry, now with server-side filtering/search/pagination so a
    Dashboard link (e.g. "Review now" -> ?status=NeedsAction) lands on the
    right slice instead of a client-side filter the URL knows nothing
    about."""
    ctx = _supplier_shared_context(request)
    orders = _supplier_orders_qs(request.user)

    status_filter = request.GET.get('status', '')
    if status_filter == 'NeedsAction':
        orders = orders.filter(status__in=Procurement.SUPPLIER_ACTION_STATUSES)
    elif status_filter == 'Incoming':
        orders = orders.filter(status__in=Procurement.AWAITING_DELIVERY_STATUSES)
    elif status_filter == 'Overdue':
        orders = orders.filter(
            status__in=Procurement.AWAITING_DELIVERY_STATUSES,
            delivery_date__lt=ctx['today'],
        )
    elif status_filter:
        orders = orders.filter(status=status_filter)

    q = request.GET.get('q', '').strip()
    if q:
        orders = orders.filter(po_number__icontains=q)

    paginator = Paginator(orders, 10)
    page_obj = paginator.get_page(request.GET.get('page'))

    ctx.update({
        'orders': page_obj,
        'page_obj': page_obj,
        'status_filter': status_filter,
        'q': q,
    })
    return render(request, 'procurement/supplier_purchase_orders.html', ctx)


@_supplier_only
def supplier_deliveries(request):
    """Everything about goods moving toward or back to GERLYN: scheduled
    and overdue deliveries, returns awaiting this supplier's response
    (moved here from the Dashboard), and recent delivered history."""
    ctx = _supplier_shared_context(request)
    orders = _supplier_orders_qs(request.user)

    scheduled_orders = orders.filter(
        status__in=Procurement.AWAITING_DELIVERY_STATUSES,
        delivery_date__isnull=False,
    ).order_by('delivery_date')
    week_end = ctx['today'] + timedelta(days=7)
    scheduled_this_week = scheduled_orders.filter(
        delivery_date__gte=ctx['today'], delivery_date__lte=week_end,
    ).count()

    ctx.update({
        'scheduled_orders': scheduled_orders,
        # Prefetch with to_attr rather than plain 'receipts__received_by':
        # a PO can have more than one DeliveryReceipt (partial shipments),
        # and the template needs the most recent one — QuerySets (even a
        # prefetched relation's own .all()) reject negative indexing
        # (`|last` failed with "Negative indexing is not supported"), but
        # to_attr caches a plain Python list, which index/slice freely.
        # Ordered newest-first so the template just takes .0.
        'delivered_orders': orders.filter(
            status=Procurement.Status.RECEIVED
        ).prefetch_related(
            Prefetch(
                'receipts',
                queryset=DeliveryReceipt.objects.select_related('received_by').order_by('-received_at'),
                to_attr='latest_receipts',
            ),
        )[:20],
        'scheduled_this_week': scheduled_this_week,
        'supplier_returns': request.user.supplier_returns.select_related(
            'procurement', 'branch', 'requested_by',
        ).prefetch_related('items__product')[:20],
    })
    return render(request, 'procurement/supplier_deliveries.html', ctx)


# =============================================================================
# SUPPLIER ACCOUNT — the portal's own self-service page (procurement/
# supplier_account.html). Everything else in the portal is transactional
# (orders, deliveries, catalog); this is the one screen about the account
# itself, the Supplier equivalent of accounts/profile.html (Customer) and
# internal_settings.html (Owner/Manager/Staff) — same idea, portal-shelled
# instead of storefront- or internal-shelled, since neither of those chromes
# is this role's.
#
# Field split, by who a change actually affects:
#   self-editable   — username, email, contact_person, contact_number,
#                      address: this supplier's own day-to-day contact
#                      details, no different in kind from a Customer editing
#                      their own name/phone/address.
#   Owner/staff-only — supplier_name, supplier_category, branch: shown
#                      read-only here. These aren't private to the supplier
#                      the way contact details are — supplier_name is how
#                      staff look this account up across POs/products/
#                      dropdowns, and branch is store-side scoping (the same
#                      reason a Branch Manager/Staff account can't reassign
#                      its own `branch` either) — letting a supplier rename
#                      either out from under the store's own records is a
#                      store decision, not a personal-details one. Both stay
#                      changeable through the existing Owner-only
#                      supplier_form/supplier_list.
#
# Password/2FA reuse accounts.views' existing endpoints verbatim
# (change_password, toggle_two_factor, request_password_change_email) —
# none of the three ever render a template on error, only ever redirect via
# accounts.views._profile_redirect, which already has a SUPPLIER branch
# pointing back at supplier_account. Nothing role-specific needed on that
# side beyond that one redirect branch.
# =============================================================================

def _supplier_account_context(request):
    """Context for supplier_account.html — shared by the GET view and
    supplier_account_update's error path, the same split
    _internal_settings_context/update_profile use for their own pages."""
    from accounts.services import get_active_sessions

    user = request.user
    profile = user.get_or_create_role_profile()
    return {
        'username': user.username,
        'email': user.email,
        'pending_email': user.pending_email,
        'is_email_verified': user.is_email_verified,
        'initials': user.get_initials(),
        'avatar_url': user.avatar.url if user.avatar else None,
        'profile': profile,
        'two_factor_enabled': user.two_factor_enabled,
        'notif_unread_count': user.notifications.filter(is_read=False).count(),
        'active_page': 'account',
        # Hero stat tile — this supplier's own PO count, the Supplier-side
        # equivalent of Customer's "Total Orders" tile. Drafts excluded for
        # the same reason supplier_portal's own list excludes them: a PO the
        # Owner hasn't approved yet isn't a commitment to this supplier, so
        # it was never really "theirs" to count.
        'purchase_order_count': Procurement.objects.filter(
            supplier=user
        ).exclude(status=Procurement.Status.DRAFT).count(),
        # Sessions card — get_active_sessions is the same role-agnostic
        # service _internal_settings_context uses; session_revoke/
        # session_logout_all_others already exist and now redirect back
        # here via _sessions_redirect's own SUPPLIER branch.
        'sessions': get_active_sessions(user),
        'current_session_key': request.session.session_key,
    }


@login_required(login_url='accounts:login')
def supplier_account(request):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')
    return render(request, 'procurement/supplier_account.html', _supplier_account_context(request))


@login_required(login_url='accounts:login')
@require_POST
def supplier_account_update(request):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    from accounts.services import set_contact_number, send_email_change_verification

    user = request.user
    profile = user.get_or_create_role_profile()
    errors = {}

    new_username = request.POST.get('username', '').strip()
    new_email = request.POST.get('email', '').strip().lower()
    new_contact_person = request.POST.get('contact_person', '').strip()
    new_contact_number = request.POST.get('contact_number', '').strip()
    new_address = request.POST.get('address', '').strip()

    if new_username and new_username != user.username:
        if User.objects.filter(username__iexact=new_username).exclude(pk=user.pk).exists():
            errors['username'] = 'That username is already taken.'
        else:
            user.username = new_username

    if new_email and new_email != user.email:
        email_taken = (
            User.objects.filter(email=new_email).exclude(pk=user.pk).exists()
            or User.objects.filter(pending_email=new_email).exclude(pk=user.pk).exists()
        )
        if email_taken:
            errors['email'] = 'That email is already associated with another account.'
        else:
            try:
                send_email_change_verification(user, new_email, request)
                messages.info(
                    request,
                    f"A verification link has been sent to {new_email}. "
                    f"Your email won't change until you click it."
                )
            except Exception:
                errors['email'] = 'Failed to send verification email. Please try again.'

    if new_contact_number:
        try:
            set_contact_number(profile, new_contact_number, user)
        except ValidationError as e:
            errors['contact_number'] = e.messages[0]

    if errors:
        context = {**_supplier_account_context(request), 'errors': errors}
        return render(request, 'procurement/supplier_account.html', context)

    user.save()
    profile.contact_person = new_contact_person
    profile.address = new_address
    profile.save(update_fields=['contact_person', 'address'])
    messages.success(request, 'Account updated.')
    return redirect('supplier_account')


@login_required(login_url='accounts:login')
@require_POST
def supplier_set_product_cost(request, link_pk):
    """Allow only the owning supplier to maintain its current unit cost."""
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    link = get_object_or_404(
        SupplierProduct.objects.select_related('product'),
        pk=link_pk,
        supplier=request.user,
    )
    raw_cost = request.POST.get('current_cost_price', '').strip()
    form_field = SupplierProduct._meta.get_field(
        'current_cost_price'
    ).formfield(required=True)
    try:
        link.current_cost_price = form_field.clean(raw_cost)
        if link.current_cost_price <= 0:
            raise ValidationError('Enter a cost greater than zero.')
        link.full_clean(exclude=['supplier', 'product'])
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return _supplier_next_redirect(request)

    link.save(update_fields=['current_cost_price'])
    if link.status == SupplierProduct.Status.APPROVED:
        pricing.on_cost_change(link.product, request.user)
    messages.success(
        request,
        f'Current cost for {link.product.product_name} updated.'
    )
    return _supplier_next_redirect(request)


# =============================================================================
# SUPPLIER NOTIFICATIONS
# The supplier's own bell/tray, off supplier_portal.html's header — the same
# static/js/notification-tray.js the internal Owner Notification Center uses
# (core/views.py, templates/internal_base.html), pointed at these endpoints
# instead. Deliberately NOT core.views.notification_center_view's own
# endpoints: those are @role_required(*INTERNAL_ROLES) — Owner/Manager/Staff
# only — and render the internal-chrome full page, neither of which a
# Supplier should ever land on. Every query below is scoped to
# request.user.notifications, so one supplier can never see another
# supplier's (or the store's own internal) notifications regardless of what
# gets requested — role separation here is "this endpoint only exists for
# SUPPLIER", not "the query trusts the caller".
# =============================================================================

def _is_xhr(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _supplier_notification_counts(notifications_qs):
    # notification-tray.js always badges off `needs_action_count` (see its
    # own comments) — that matches the Owner tray's "unresolved Critical/
    # High" semantics, but notify_supplier() never sets requires_action, so
    # that count would always read 0 here. Reusing the key for unread_count
    # instead gives the supplier's bell the plain "you have unread
    # notifications" badge that actually fits a read-only inbox.
    unread_count = notifications_qs.filter(is_read=False).count()
    return {
        'total_count': notifications_qs.count(),
        'unread_count': unread_count,
        'needs_action_count': unread_count,
    }


@login_required(login_url='accounts:login')
def supplier_notifications(request):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    notifications = request.user.notifications.filter(
        category=Notification.Category.PROCUREMENT
    ).order_by('-created_at')[:200]
    counts = _supplier_notification_counts(request.user.notifications.filter(
        category=Notification.Category.PROCUREMENT
    ))

    if _is_xhr(request):
        list_html = render_to_string(
            'procurement/_supplier_notification_tray_list.html',
            {'notifications': notifications}, request=request,
        )
        return JsonResponse({'list_html': list_html, **counts})

    return render(request, 'procurement/supplier_notifications.html', {
        'notifications': notifications,
        **counts,
    })


@require_POST
@login_required(login_url='accounts:login')
def supplier_notification_mark_read(request, pk):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    notification = get_object_or_404(request.user.notifications, pk=pk)
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=['is_read', 'read_at'])
    if _is_xhr(request):
        return JsonResponse(_supplier_notification_counts(
            request.user.notifications.filter(category=Notification.Category.PROCUREMENT)
        ))
    return redirect('supplier_notifications')


@require_POST
@login_required(login_url='accounts:login')
def supplier_notification_mark_all_read(request):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    updated = request.user.notifications.filter(
        category=Notification.Category.PROCUREMENT, is_read=False
    ).update(is_read=True, read_at=timezone.now())
    if _is_xhr(request):
        return JsonResponse(_supplier_notification_counts(
            request.user.notifications.filter(category=Notification.Category.PROCUREMENT)
        ))
    if updated:
        messages.success(
            request,
            f'Marked {updated} notification{"" if updated == 1 else "s"} as read.',
        )
    else:
        messages.info(request, 'You have no unread notifications.')
    return redirect('supplier_notifications')


# =============================================================================
# SUPPLIERS (managed by the store)
# =============================================================================

@login_required(login_url='accounts:login')
def _supplier_list_context(request):
    from django.db.models import Count

    suppliers = User.objects.filter(role=User.Role.SUPPLIER).select_related(
        'supplier_profile', 'supplier_profile__category'
    ).annotate(
        # Drives the Products column, which links into the per-supplier
        # product sub-view. Annotated rather than counted per row in the
        # template, which would be one query per supplier.
        product_count=Count(
            'supplied_products', distinct=True,
            filter=Q(supplied_products__status=SupplierProduct.Status.APPROVED),
        ),
        # Offers and new-product requests waiting on the Owner, flagged on the row.
        pending_offer_count=Count(
            'supplied_products', distinct=True,
            filter=Q(supplied_products__status=SupplierProduct.Status.PROPOSED),
        ),
        pending_request_count=Count(
            'product_requests', distinct=True,
            filter=Q(product_requests__status=SupplierProductRequest.Status.PENDING),
        ),
    ).order_by('supplier_profile__supplier_name')

    search_query = request.GET.get('q', '')
    if search_query:
        suppliers = suppliers.filter(
            supplier_profile__supplier_name__icontains=search_query
        )

    from branches.models import Branch
    return {
        'suppliers': suppliers,
        'search_query': search_query,
        'can_add': (
            request.user.role == User.Role.OWNER
            and user_can_perform_module_action(request.user, PROCUREMENT, ADD)
        ),
        'can_edit': (
            request.user.role == User.Role.OWNER
            and user_can_perform_module_action(request.user, PROCUREMENT, EDIT)
        ),
        'can_delete': (
            request.user.role == User.Role.OWNER
            and user_can_perform_module_action(request.user, PROCUREMENT, DELETE)
        ),
        # The add/edit modal assigns a supplier to a branch (or to none, meaning
        # every branch). Without this the modal's select renders empty and a
        # saved edit would blank the assignment the list column shows.
        'branches': Branch.objects.all(),
        # Inactive categories are hidden from the picker; the modal adds the
        # supplier's current one back itself so editing never blanks it.
        'supplier_categories': SupplierCategory.objects.filter(is_active=True),
    }


@login_required(login_url='accounts:login')
def supplier_list(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    context = _supplier_list_context(request)

    edit_pk = request.GET.get('edit')
    if edit_pk and context['can_edit']:
        supplier = User.objects.filter(pk=edit_pk, role=User.Role.SUPPLIER).first()
        if supplier:
            context.update({
                'show_edit_modal': True,
                'edit_supplier': supplier,
                'edit_profile': getattr(supplier, 'supplier_profile', None),
            })

    return render(request, 'procurement/supplier_list.html', context)


@login_required(login_url='accounts:login')
def supplier_form(request, pk=None):
    """Add or edit a supplier (Owner only). Both live in a modal on the
    supplier list, so this is the POST target and renders no page of its own.

    What changed when the form moved into the modal: the GET branch, and how a
    validation failure is reported. Every write below is untouched — same
    fields, same order, same transaction."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage suppliers.')
        return redirect('supplier_list')

    supplier = get_object_or_404(User, pk=pk, role=User.Role.SUPPLIER) if pk else None
    profile = getattr(supplier, 'supplier_profile', None) if supplier else None

    if request.method != 'POST':
        flag = f'edit={pk}' if supplier else 'add=1'
        return redirect(f"{reverse('supplier_list')}?{flag}")

    name = request.POST.get('supplier_name', '').strip()
    username = request.POST.get('username', '').strip()
    email = request.POST.get('email', '').strip().lower()
    business_registration_id = (
        request.POST.get('business_registration_id', '').strip().upper() or None
    )

    # Both checks were already here — the first returned to request.path, the
    # second sat inside the atomic block and returned before writing anything.
    # Collected instead of redirected so the typed form survives.
    errors = []
    if not name or not username:
        errors.append('Supplier name and username are required.')
    elif User.objects.filter(username__iexact=username).exclude(
        pk=supplier.pk if supplier else None
    ).exists():
        errors.append(f'Username "{username}" is already taken.')
    if email and User.objects.filter(
        role=User.Role.SUPPLIER, email__iexact=email,
    ).exclude(pk=supplier.pk if supplier else None).exists():
        errors.append(f'Email "{email}" is already used by another supplier account.')
    if business_registration_id and User.objects.filter(
        role=User.Role.SUPPLIER,
        supplier_profile__business_registration_id__iexact=business_registration_id,
    ).exclude(pk=supplier.pk if supplier else None).exists():
        errors.append('That business/tax registration identifier already belongs to another supplier.')

    # A category is picked from the managed list, never typed. A blank choice
    # clears it; a retired (inactive) one is only accepted if the supplier
    # already had it, so editing an old supplier doesn't force a change.
    category = None
    category_id = request.POST.get('category', '').strip()
    if category_id:
        category = SupplierCategory.objects.filter(pk=category_id).first()             if category_id.isdigit() else None
        current_id = profile.category_id if profile else None
        if category is None or (not category.is_active and category.pk != current_id):
            errors.append('Choose a category from the list.')

    branch = None
    branch_id = request.POST.get('branch', '').strip()
    if branch_id:
        from branches.models import Branch
        branch = Branch.objects.filter(pk=branch_id).first() if branch_id.isdigit() else None
        if branch is None:
            errors.append('Choose a valid branch from the list.')

    if not errors:
        with transaction.atomic():
            if supplier is None:
                # Suppliers get a random password; the Owner shares a reset link
                # if the supplier ever needs to log in themselves.
                supplier = User.objects.create_user(
                    username=username,
                    email=email,
                    password=get_random_string(20),
                    role=User.Role.SUPPLIER,
                )
                # accounts.signals creates a blank profile on user creation.
                profile = supplier.supplier_profile
            else:
                supplier.username = username
                supplier.email = email
                supplier.save()

            profile.supplier_name = name
            profile.contact_person = request.POST.get('contact_person', '')
            profile.contact_number = request.POST.get('contact_number', '')
            profile.category = category
            profile.address = request.POST.get('address', '')
            profile.branch = branch
            profile.business_registration_id = business_registration_id
            profile.save()

        similar = User.objects.filter(role=User.Role.SUPPLIER).exclude(pk=supplier.pk)
        contact = request.POST.get('contact_number', '').strip()
        if (
            similar.filter(supplier_profile__supplier_name__iexact=name).exists()
            or (contact and similar.filter(supplier_profile__contact_number=contact).exists())
        ):
            messages.warning(
                request,
                'Supplier saved, but another supplier has a similar name or contact number. '
                'Review both records to confirm they are separate businesses.',
            )
        messages.success(request, f'Supplier "{name}" saved.')
        return redirect('supplier_list')

    context = _supplier_list_context(request)
    if pk:
        context.update({
            'show_edit_modal': True,
            'edit_supplier': supplier,
            'edit_profile': profile,
            'edit_errors': errors,
            'edit_form_data': request.POST,
        })
    else:
        context.update({
            'show_add_modal': True,
            'add_errors': errors,
            'add_form_data': request.POST,
        })
    return render(request, 'procurement/supplier_list.html', context)


# -----------------------------------------------------------------------------
# SUPPLIER CATEGORIES — the managed list the supplier form's dropdown reads.
#
# Owner-only, like every other supplier write. Kept on one small page (add
# form + a row per category) rather than the modal pattern: a category is just
# a name, a flag and a set of product categories.
# -----------------------------------------------------------------------------

def _supplier_category_context():
    from inventory.models import ProductCategory
    return {
        'categories': SupplierCategory.objects.prefetch_related(
            'product_categories'
        ).annotate(supplier_count=Count('suppliers')),
        'product_categories': ProductCategory.objects.all(),
    }


@login_required(login_url='accounts:login')
def supplier_category_list(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')
    context = _supplier_category_context()
    context.update({
        'can_add': (
            request.user.role == User.Role.OWNER
            and user_can_perform_module_action(request.user, PROCUREMENT, ADD)
        ),
        'can_edit': (
            request.user.role == User.Role.OWNER
            and user_can_perform_module_action(request.user, PROCUREMENT, EDIT)
        ),
        'can_delete': (
            request.user.role == User.Role.OWNER
            and user_can_perform_module_action(request.user, PROCUREMENT, DELETE)
        ),
    })
    return render(request, 'procurement/supplier_category_list.html', context)


@login_required(login_url='accounts:login')
def supplier_category_save(request, pk=None):
    """Create or update one supplier category (Owner only, POST only)."""
    from inventory.models import ProductCategory

    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage supplier categories.')
        return redirect('supplier_category_list')
    if request.method != 'POST':
        return redirect('supplier_category_list')

    category = get_object_or_404(SupplierCategory, pk=pk) if pk else None
    name = request.POST.get('name', '').strip()
    if not name:
        messages.error(request, 'Category name is required.')
        return redirect('supplier_category_list')
    if len(name) > 100:
        messages.error(request, 'Category name is too long (100 characters max).')
        return redirect('supplier_category_list')
    clash = SupplierCategory.objects.filter(name__iexact=name)
    if category:
        clash = clash.exclude(pk=category.pk)
    if clash.exists():
        messages.error(request, f'A supplier category called "{name}" already exists.')
        return redirect('supplier_category_list')

    product_ids = [
        int(v) for v in request.POST.getlist('product_categories') if v.isdigit()
    ]
    with transaction.atomic():
        target = category or SupplierCategory()
        target.name = name
        if category:
            # The row form only posts the checkbox when ticked; a create form
            # has no such field, so a new category starts active.
            target.is_active = bool(request.POST.get('is_active'))
        target.save()
        target.product_categories.set(ProductCategory.objects.filter(pk__in=product_ids))
    messages.success(
        request, f'Supplier category "{name}" {"updated" if category else "added"}.'
    )
    return redirect('supplier_category_list')


@login_required(login_url='accounts:login')
def supplier_category_delete(request, pk):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage supplier categories.')
        return redirect('supplier_category_list')
    if request.method != 'POST':
        return redirect('supplier_category_list')

    category = get_object_or_404(SupplierCategory, pk=pk)
    in_use = category.suppliers.count()
    if in_use:
        # Deleting would silently un-categorise these suppliers (SET_NULL);
        # make the Owner move them first, or retire the category instead.
        messages.error(
            request,
            f'Cannot delete "{category.name}" — {in_use} supplier'
            f'{"s use" if in_use != 1 else " uses"} it. Reassign them, or mark '
            f'the category inactive instead.',
        )
        return redirect('supplier_category_list')
    name = category.name
    category.delete()
    messages.success(request, f'Supplier category "{name}" deleted.')
    return redirect('supplier_category_list')


# -----------------------------------------------------------------------------
# SUPPLIER DETAIL — the per-supplier product sub-view.
#
# Which products a supplier carries used to be answerable only by reading back
# through past purchase orders. SupplierProduct makes it a real, editable
# mapping; these views are its UI.
# -----------------------------------------------------------------------------

@login_required(login_url='accounts:login')
def supplier_detail(request, pk):
    # supplier_list (the only place this page is linked from) already lets
    # Staff view the supplier table and its product/pending-offer counts —
    # both are gated by the same PROCUREMENT view grant in the module
    # registry. Staff used to 404 into "Access denied" the moment they
    # followed one of those links; this view's own role check had simply
    # never been widened to match. can_edit/can_add/can_delete below stay
    # Owner-only, so nothing writable opens up — Staff lands on the same
    # read-only view a Branch Manager already gets.
    if request.user.role not in [
        User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF,
    ]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    supplier = get_object_or_404(
        User.objects.select_related('supplier_profile'),
        pk=pk, role=User.Role.SUPPLIER,
    )

    all_links = SupplierProduct.objects.filter(supplier=supplier).select_related(
        'product', 'product__category'
    )
    # The table below is what the store actually buys: approved offers only.
    # Proposed ones wait in their own card until the Owner decides.
    links = all_links.approved()
    pending_offers = all_links.filter(status=SupplierProduct.Status.PROPOSED)

    # Products this supplier doesn't already have a row for — the "Add Product"
    # picker. Excluding every status up front is what stops a user picking a
    # product that would only bounce off the unique constraint.
    available = Product.objects.exclude(
        pk__in=all_links.values_list('product_id', flat=True)
    ).order_by('product_name')

    # Everyone else, for the transfer destination dropdown.
    other_suppliers = User.objects.filter(
        role=User.Role.SUPPLIER
    ).exclude(pk=supplier.pk).select_related('supplier_profile').order_by(
        'supplier_profile__supplier_name'
    )

    return render(request, 'procurement/supplier_detail.html', {
        'supplier': supplier,
        'profile': getattr(supplier, 'supplier_profile', None),
        'links': links,
        'pending_offers': pending_offers,
        'pending_requests': _with_price_hints(supplier.product_requests.filter(
            status=SupplierProductRequest.Status.PENDING
        )),
        'product_categories': ProductCategory.objects.all(),
        'available_products': available,
        'other_suppliers': other_suppliers,
        'can_add': (
            request.user.role == User.Role.OWNER
            and user_can_perform_module_action(request.user, PROCUREMENT, ADD)
        ),
        'can_edit': (
            request.user.role == User.Role.OWNER
            and user_can_perform_module_action(request.user, PROCUREMENT, EDIT)
        ),
        'can_delete': (
            request.user.role == User.Role.OWNER
            and user_can_perform_module_action(request.user, PROCUREMENT, DELETE)
        ),
        'recent_orders': scope_queryset(
            Procurement.objects.filter(supplier=supplier).select_related('branch'),
            request.user,
        ).order_by('-order_date')[:5],
    })


@login_required(login_url='accounts:login')
def supplier_product_add(request, pk):
    """Link a product to this supplier's catalog."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage supplier products.')
        return redirect('supplier_detail', pk=pk)

    supplier = get_object_or_404(User, pk=pk, role=User.Role.SUPPLIER)
    if request.method != 'POST':
        return redirect('supplier_detail', pk=pk)

    product_id = request.POST.get('product')
    if not product_id:
        messages.error(request, 'Choose a product to add.')
        return redirect('supplier_detail', pk=pk)

    product = get_object_or_404(Product, pk=product_id)

    lead = request.POST.get('lead_time_days') or None

    # get_or_create rather than create: the picker already excludes what the
    # supplier carries, but a stale form or a double-submit can still arrive
    # with a duplicate, and that should be a no-op rather than a 500 off the
    # unique constraint.
    link, created = SupplierProduct.objects.get_or_create(
        supplier=supplier, product=product,
        defaults={
            'supplier_sku': request.POST.get('supplier_sku', '').strip(),
            'lead_time_days': lead,
            'is_preferred': bool(request.POST.get('is_preferred')),
        },
    )

    if created:
        messages.success(request, f'{product.product_name} added to this supplier.')
    elif link.status != SupplierProduct.Status.APPROVED:
        # The Owner adding a product the supplier had only offered (or had
        # rejected) is a decision — approve it rather than saying "already".
        link.status = SupplierProduct.Status.APPROVED
        link.reviewed_by = request.user
        link.reviewed_at = timezone.now()
        link.review_note = ''
        link.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_note'])
        messages.success(request, f'{product.product_name} approved for this supplier.')
    else:
        messages.info(request, f'{product.product_name} is already on this supplier.')
    return redirect('supplier_detail', pk=pk)


def _clean_offer_cost(raw):
    """A supplier's quoted unit cost: a positive decimal, same rules as the
    model field. Raises ValidationError with a user-facing message."""
    field = SupplierProduct._meta.get_field('current_cost_price').formfield(required=True)
    cost = field.clean((raw or '').strip())
    if cost <= 0:
        raise ValidationError('Enter a cost greater than zero.')
    return cost


@login_required(login_url='accounts:login')
@require_POST
def supplier_product_self_add(request):
    """A supplier offers a product from the Product Master List.

    The offer starts Proposed with the supplier's cost and lead time; it can't
    be put on a purchase order until the Owner approves it (see
    supplier_offer_review). Re-offering a Rejected product re-opens it.
    """
    from .notifications import notify_offer_for_review

    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    product_id = request.POST.get('product')
    if not product_id:
        messages.error(request, 'Choose a product to add.')
        return _supplier_next_redirect(request)

    product = get_object_or_404(Product, pk=product_id)
    try:
        cost = _clean_offer_cost(request.POST.get('current_cost_price'))
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return _supplier_next_redirect(request)
    lead = _safe_nonneg_int(request.POST.get('lead_time_days'))

    with transaction.atomic():
        link, created = SupplierProduct.objects.get_or_create(
            supplier=request.user, product=product,
            defaults={
                'status': SupplierProduct.Status.PROPOSED,
                'current_cost_price': cost,
                'lead_time_days': lead,
            },
        )
        reopened = False
        if not created and link.status == SupplierProduct.Status.REJECTED:
            link.status = SupplierProduct.Status.PROPOSED
            link.current_cost_price = cost
            link.lead_time_days = lead
            link.reviewed_by = None
            link.reviewed_at = None
            link.review_note = ''
            link.save()
            reopened = True

    if created or reopened:
        notify_offer_for_review(request.user, product.product_name, new_product=False)
        messages.success(
            request,
            f'{product.product_name} offered — it can be ordered once Gerlyn approves it.',
        )
    elif link.status == SupplierProduct.Status.PROPOSED:
        messages.info(request, f'{product.product_name} is already awaiting approval.')
    else:
        messages.info(request, f'{product.product_name} is already on your catalog.')
    return _supplier_next_redirect(request)


@login_required(login_url='accounts:login')
@require_POST
def supplier_product_self_remove(request, link_pk):
    """A supplier takes a product off its own catalog (or withdraws a
    pending/rejected offer). Blocked while one of its open orders needs it."""
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    # Scoped to request.user: another supplier's link is a 404, not a refusal.
    link = get_object_or_404(
        SupplierProduct.objects.select_related('product'),
        pk=link_pk, supplier=request.user,
    )
    name = link.product.product_name
    try:
        remove_supplier_product(link)
    except SupplierRemovalBlocked as exc:
        messages.error(request, f'Cannot remove {name}. {exc}')
        return _supplier_next_redirect(request)
    except SupplierProduct.DoesNotExist:
        pass  # double-submit: already gone, but the supplier's intent is satisfied
    messages.success(request, f'{name} removed from your catalog.')
    return _supplier_next_redirect(request)


@login_required(login_url='accounts:login')
@require_POST
def supplier_product_request_add(request):
    """A supplier asks the store to carry a product that isn't in the master
    list. Only the Owner can turn it into a real product."""
    from .notifications import notify_offer_for_review

    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    name = request.POST.get('product_name', '').strip()
    unit = request.POST.get('unit_of_measure', '')
    if not name or len(name) > 150:
        messages.error(request, 'Enter the product name (150 characters max).')
        return _supplier_next_redirect(request)
    if unit not in Product.UnitOfMeasure.values:
        messages.error(request, 'Choose a unit for the product.')
        return _supplier_next_redirect(request)
    try:
        cost = _clean_offer_cost(request.POST.get('proposed_cost'))
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return _supplier_next_redirect(request)
    if Product.objects.filter(product_name__iexact=name).exists():
        messages.error(
            request,
            f'"{name}" is already in the product list — offer it from '
            f'"Offer a product" instead.',
        )
        return _supplier_next_redirect(request)
    if SupplierProductRequest.objects.filter(
        supplier=request.user, product_name__iexact=name,
        status=SupplierProductRequest.Status.PENDING,
    ).exists():
        messages.info(request, f'You already have a pending request for "{name}".')
        return _supplier_next_redirect(request)

    SupplierProductRequest.objects.create(
        supplier=request.user, product_name=name, unit_of_measure=unit,
        proposed_cost=cost,
        lead_time_days=_safe_nonneg_int(request.POST.get('lead_time_days')),
        note=request.POST.get('note', '').strip()[:300],
    )
    notify_offer_for_review(request.user, name, new_product=True)
    messages.success(request, f'Request for "{name}" sent to Gerlyn for review.')
    return _supplier_next_redirect(request)


@login_required(login_url='accounts:login')
@require_POST
def supplier_product_request_withdraw(request, req_pk):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')
    with transaction.atomic():
        req = get_object_or_404(
            SupplierProductRequest.objects.select_for_update(),
            pk=req_pk, supplier=request.user,
        )
        if req.status != SupplierProductRequest.Status.PENDING:
            messages.error(request, 'That request was already decided.')
            return _supplier_next_redirect(request)
        req.status = SupplierProductRequest.Status.WITHDRAWN
        req.save(update_fields=['status'])
    messages.success(request, f'Request for "{req.product_name}" withdrawn.')
    return _supplier_next_redirect(request)


@login_required(login_url='accounts:login')
@require_POST
def supplier_offer_review(request, pk, link_pk):
    """Owner approves or rejects a supplier's Proposed offer."""
    from .notifications import notify_offer_decision

    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can review supplier offers.')
        return redirect('supplier_detail', pk=pk)

    link = get_object_or_404(
        SupplierProduct.objects.select_related('product', 'supplier'),
        pk=link_pk, supplier_id=pk,
    )
    approve = request.POST.get('decision') == 'approve'
    note = request.POST.get('note', '').strip()
    try:
        link = review_offer(link, request.user, approve=approve, note=note)
    except OfferReviewError as exc:
        messages.error(request, str(exc))
        return redirect('supplier_detail', pk=pk)

    notify_offer_decision(
        link.supplier, link.product.product_name, request.user,
        approved=approve, note=note,
    )
    if approve:
        pricing.on_cost_change(link.product, request.user)
    messages.success(
        request,
        f'{link.product.product_name} {"approved" if approve else "rejected"}.',
    )
    return redirect('supplier_detail', pk=pk)


def _with_price_hints(requests):
    """Attach the computed retail price to each pending new-product request.

    `price_hint` is what the approval form pre-fills (store-wide markup, no
    category yet) and `price_hint_by_category` re-prices when the Owner picks
    one. Absent markup means no hint: the Owner types the price as before.
    """
    categories = list(ProductCategory.objects.all())
    store_markup, _ = pricing.resolve_markup(None)
    rows = list(requests)
    for req in rows:
        req.price_hint = (
            pricing.price_from_cost(req.proposed_cost, store_markup)
            if store_markup is not None else None
        )
        by_category = {}
        for category in categories:
            markup, _source = pricing.resolve_markup(None, category)
            if markup is not None:
                by_category[category.pk] = pricing.price_from_cost(
                    req.proposed_cost, markup,
                )
        req.price_hint_by_category = by_category
    return rows


@login_required(login_url='accounts:login')
@require_POST
def supplier_request_review(request, pk, req_pk):
    """Owner approves (creating the product) or rejects a new-product request."""
    from .notifications import notify_offer_decision

    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can review supplier requests.')
        return redirect('supplier_detail', pk=pk)

    req = get_object_or_404(
        SupplierProductRequest.objects.select_related('supplier'),
        pk=req_pk, supplier_id=pk,
    )
    approve = request.POST.get('decision') == 'approve'
    note = request.POST.get('note', '').strip()
    unit_price = category = None
    if approve:
        try:
            unit_price = Decimal(request.POST.get('unit_price', '').strip())
        except (InvalidOperation, ValueError):
            messages.error(request, 'Enter a retail price greater than zero.')
            return redirect('supplier_detail', pk=pk)
        category_id = request.POST.get('category', '')
        if category_id:
            category = ProductCategory.objects.filter(
                pk=category_id if category_id.isdigit() else 0
            ).first()
            if category is None:
                messages.error(request, 'Choose a valid product category.')
                return redirect('supplier_detail', pk=pk)
    try:
        req = review_product_request(
            req, request.user, approve=approve, note=note,
            unit_price=unit_price, category=category,
        )
    except OfferReviewError as exc:
        messages.error(request, str(exc))
        return redirect('supplier_detail', pk=pk)

    notify_offer_decision(
        req.supplier, req.product_name, request.user, approved=approve, note=note,
    )
    verdict = 'approved and added to the product list' if approve else 'rejected'
    messages.success(request, f'"{req.product_name}" {verdict}.')
    return redirect('supplier_detail', pk=pk)


@login_required(login_url='accounts:login')
def supplier_product_remove(request, pk, link_pk):
    """Unlink a product without transferring it — it simply ends up with no
    current supplier."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage supplier products.')
        return redirect('supplier_detail', pk=pk)

    if request.method != 'POST':
        return redirect('supplier_detail', pk=pk)

    link = get_object_or_404(
        SupplierProduct.objects.select_related('product'),
        pk=link_pk, supplier_id=pk,
    )
    name = link.product.product_name
    try:
        remove_supplier_product(link)
    except SupplierRemovalBlocked as exc:
        messages.error(request, f'Cannot remove {name}. {exc}')
        return redirect('supplier_detail', pk=pk)
    except SupplierProduct.DoesNotExist:
        pass  # double-submit: already gone
    messages.success(request, f'{name} removed from this supplier.')
    return redirect('supplier_detail', pk=pk)


@login_required(login_url='accounts:login')
def supplier_product_transfer(request, pk):
    """Move selected products from this supplier to another one.

    Handles both the per-row action and the multi-select bulk action — the
    row action just posts a single product_ids value, so there is one code
    path and one set of rules.
    """
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can transfer supplier products.')
        return redirect('supplier_detail', pk=pk)

    source = get_object_or_404(User, pk=pk, role=User.Role.SUPPLIER)
    if request.method != 'POST':
        return redirect('supplier_detail', pk=pk)

    product_ids = request.POST.getlist('product_ids')
    dest_id = request.POST.get('to_supplier')

    if not product_ids:
        messages.error(request, 'Select at least one product to transfer.')
        return redirect('supplier_detail', pk=pk)
    if not dest_id:
        messages.error(request, 'Choose the supplier to transfer to.')
        return redirect('supplier_detail', pk=pk)

    dest = get_object_or_404(User, pk=dest_id, role=User.Role.SUPPLIER)

    try:
        moved, merged = transfer_supplier_products(product_ids, source, dest)
    except SupplierTransferError as e:
        messages.error(request, str(e))
        return redirect('supplier_detail', pk=pk)

    dest_name = getattr(dest.supplier_profile, 'supplier_name', dest.username)
    note = f'{moved} product{"" if moved == 1 else "s"} transferred to {dest_name}.'
    if merged:
        note += (
            f' {merged} {"was" if merged == 1 else "were"} already on that '
            f'supplier and {"has" if merged == 1 else "have"} been consolidated.'
        )
    messages.success(request, note)
    return redirect('supplier_detail', pk=pk)


@login_required(login_url='accounts:login')
def supplier_delete(request, pk):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can deactivate suppliers.')
        return redirect('supplier_list')

    supplier = get_object_or_404(User, pk=pk, role=User.Role.SUPPLIER)
    open_orders = Procurement.objects.filter(supplier=supplier).exclude(
        status__in=[Procurement.Status.RECEIVED, Procurement.Status.CANCELLED]
    ).count()

    if request.method == 'POST':
        if supplier.is_active and open_orders:
            messages.error(
                request,
                f'Cannot delete — this supplier still has {open_orders} open order(s).'
            )
            return redirect('supplier_list')
        name = getattr(supplier.supplier_profile, 'supplier_name', supplier.username)
        supplier.is_active = not supplier.is_active
        supplier.save(update_fields=['is_active'])
        state = 'reactivated' if supplier.is_active else 'deactivated'
        messages.success(request, f'Supplier "{name}" {state}. Historical records were preserved.')
        return redirect('supplier_list')

    # The confirm step is a modal on the supplier list now (confirm-dialog.js
    # off the row's Delete button). A direct GET here just goes to the list.
    return redirect('supplier_list')


# =============================================================================
# SECTION 2 — DRF API Views (what React calls)
# Base URL: /api/procurement/
# =============================================================================

class ProcurementListCreateAPIView(APIView):
    """
    GET  /api/procurement/        -> list all POs (filtered by role)
    POST /api/procurement/        -> create a new PO
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get_queryset(self, user):
        qs = Procurement.objects.select_related(
            'branch', 'supplier', 'supplier__supplier_profile',
            'owner', 'owner__owner_profile',
            'created_by', 'created_by__staff_profile',
            'created_by__manager_profile',
        ).prefetch_related('items__product', 'receipts__received_by').order_by('-id')

        # Role-based filtering — mirrors the original template view logic
        return procurements_visible_to(user, qs)

    def get(self, request):
        procurements = self.get_queryset(request.user)
        serializer   = ProcurementReadSerializer(procurements, many=True)
        return Response(serializer.data)

    def post(self, request):
        # Only Owner and Branch Manager can create POs
        if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
            return Response(
                {'detail': 'You do not have permission to create procurement orders.'},
                status=status.HTTP_403_FORBIDDEN
            )

        payload = request.data.copy()
        header_key = request.headers.get('Idempotency-Key', '').strip()
        if header_key:
            payload['submissionKey'] = header_key
        submission_key = (payload.get('submissionKey') or '').strip()
        if submission_key:
            existing = Procurement.objects.filter(submission_key=submission_key).first()
            if existing:
                if existing.created_by_id != request.user.id:
                    return Response(
                        {'detail': 'That submission key belongs to another user.'},
                        status=status.HTTP_409_CONFLICT,
                    )
                return Response(ProcurementReadSerializer(existing).data, status=status.HTTP_200_OK)

        serializer = ProcurementWriteSerializer(
            data=payload,
            context={'request': request}
        )
        if serializer.is_valid():
            try:
                proc = serializer.save()
            except IntegrityError:
                submission_key = (
                    serializer.validated_data.get('submissionKey') or ''
                ).strip()
                existing = Procurement.objects.filter(
                    submission_key=submission_key,
                    created_by=request.user,
                ).first()
                if existing:
                    return Response(
                        ProcurementReadSerializer(existing).data,
                        status=status.HTTP_200_OK,
                    )
                raise
            read_serializer = ProcurementReadSerializer(proc)
            return Response(read_serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ProcurementDetailAPIView(APIView):
    """
    GET   /api/procurement/<pk>/   -> retrieve single PO
    DELETE /api/procurement/<pk>/  -> cancel a Draft PO only
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get_object(self, pk):
        return get_object_or_404(
            Procurement.objects.select_related(
                'branch', 'supplier', 'supplier__supplier_profile',
                'owner', 'owner__owner_profile',
                'created_by', 'created_by__staff_profile',
            ).prefetch_related('items__product', 'receipts__received_by'),
            pk=pk
        )

    def _can_access(self, user, proc):
        return user_can_read_procurement(user, proc)

    def get(self, request, pk):
        proc = self.get_object(pk)
        if not self._can_access(request.user, proc):
            return Response(
                {'detail': 'That purchase order is not available to you.'},
                status=status.HTTP_404_NOT_FOUND
            )
        serializer = ProcurementReadSerializer(proc)
        return Response(serializer.data)

    def delete(self, request, pk):
        proc = self.get_object(pk)

        if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
            return Response(
                {'detail': 'Permission denied.'},
                status=status.HTTP_403_FORBIDDEN
            )
        if not user_can_access_branch(request.user, proc.branch_id):
            return Response(
                {'detail': 'That purchase order belongs to another branch.'},
                status=status.HTTP_403_FORBIDDEN
            )
        if proc.status != Procurement.Status.DRAFT:
            return Response(
                {'detail': 'Only Draft orders can be deleted.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        proc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProcurementStatusUpdateAPIView(APIView):
    """
    PATCH /api/procurement/<pk>/update_status/

    Allowed transitions (Owner or Manager only — see business rule 13 in
    delivery_receive: a supplier must never be able to self-confirm receipt):
        DRAFT      -> ORDERED (Owner or Manager submits the PO to supplier)
        ORDERED    -> RECEIVED (Owner or Manager confirms delivery)
        ORDERED    -> CANCELLED (Owner or Manager cancels)
        RECEIVED   -> no further transitions

    On RECEIVED:
        - Sets quantity_received on each item (per-item, if the client sends
          `items`; otherwise assumes full receipt, same as before)
        - Credits WarehouseStock.quantity_on_hand for the branch — never
          Inventory (floor stock) directly, matching delivery_receive
        - Auto-creates a Batch record per item for FEFO expiry tracking
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    # Allowed transitions live on the model now (Procurement.ALLOWED_TRANSITIONS)
    # so this endpoint and the template view procurement_update_status enforce
    # one definition instead of two (finding C-3).

    def patch(self, request, pk):
        proc = get_object_or_404(Procurement, pk=pk)

        # Suppliers deliberately have no path here — see business rule 13.
        # A supplier confirming their own delivery would defeat the store's
        # receipt-validation control (that's what delivery_receive is for).
        if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
            return Response(
                {'detail': 'Permission denied.'},
                status=status.HTTP_403_FORBIDDEN
            )
        if not user_can_access_branch(request.user, proc.branch_id):
            return Response(
                {'detail': 'That purchase order belongs to another branch.'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = ProcurementStatusSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        new_status = serializer.validated_data['status']

        if new_status in (
            Procurement.Status.PENDING_OWNER_REVIEW,
            Procurement.Status.PENDING_SUPPLIER_REVIEW,
            Procurement.Status.TERMS_AGREED,
            Procurement.Status.COMMUNICATED,
            Procurement.Status.INCOMING,
            Procurement.Status.PARTIALLY_RECEIVED,
            Procurement.Status.RECEIVED,
        ):
            return Response(
                {
                    'detail': (
                        'Use the dedicated negotiation, communication, or receiving endpoint '
                        'for this workflow transition.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            # Re-fetch and lock so a double-submit (double-click, retry)
            # can't both pass this same-transition check and both apply the
            # RECEIVED branch's stock credit for one delivery.
            locked_proc = get_object_or_404(
                Procurement.objects.select_for_update(), pk=pk
            )
            if not locked_proc.can_transition_to(new_status):
                return Response(
                    {'detail': f"Cannot transition from '{locked_proc.status}' to '{new_status}'."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if (
                locked_proc.status == Procurement.Status.DRAFT
                and new_status == Procurement.Status.APPROVED
                and request.user.role != User.Role.OWNER
            ):
                return Response(
                    {'detail': 'Only the Owner can approve a draft purchase order.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            updated = serializer.update(locked_proc, serializer.validated_data)

            if new_status == Procurement.Status.APPROVED:
                updated.owner = request.user
                updated.authorized_at = timezone.now()
                updated.save(update_fields=['owner', 'authorized_at'])

        if new_status == Procurement.Status.APPROVED:
            from .notifications import notify_po_approved

            notify_po_approved(updated, request.user)

        read_serializer = ProcurementReadSerializer(updated)
        return Response(read_serializer.data)


class ProcurementReceiveAPIView(APIView):
    """Record an accepted or rejected delivery through the shared service."""

    permission_classes = [IsAuthenticated, HasModuleAccess]

    def post(self, request, pk):
        if request.user.role not in [
            User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF,
        ]:
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        proc = get_object_or_404(
            procurements_visible_to(request.user, Procurement.objects.all()), pk=pk,
        )
        if not user_can_access_branch(request.user, proc.branch_id):
            return Response(
                {'detail': 'That purchase order belongs to another branch.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        payload = request.data.copy()
        header_key = request.headers.get('Idempotency-Key', '').strip()
        if header_key:
            payload['submissionKey'] = header_key
        serializer = DeliveryEventSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        rejected = data['outcome'] == DeliveryReceipt.Outcome.REJECTED
        lines = {} if rejected else {
            row['itemId']: (row['quantityReceived'], row.get('expiryDate'))
            for row in data['items']
        }
        try:
            receipt, updated, duplicate = record_delivery(
                proc, request.user, lines=lines,
                receipt_date=data.get('actualReceiptDate'),
                delivered_by=data.get('deliveredBy', ''),
                idempotency_key=data['submissionKey'],
                rejection_reason=data.get('rejectionReason', '') if rejected else '',
            )
        except DeliveryError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        response = ProcurementReadSerializer(updated).data
        response['deliveryEvent'] = {
            'id': receipt.pk,
            'outcome': receipt.outcome,
            'duplicate': duplicate,
            'rejectionReason': receipt.rejection_reason,
        }
        return Response(response, status=status.HTTP_200_OK)


class ProcurementCommunicateAPIView(APIView):
    """Record external supplier contact without requiring a supplier login."""
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def post(self, request, pk):
        if request.user.role not in [
            User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF,
        ]:
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        proc = get_object_or_404(
            procurements_visible_to(request.user, Procurement.objects.all()), pk=pk,
        )
        if not user_can_access_branch(request.user, proc.branch_id):
            return Response(
                {'detail': 'That purchase order belongs to another branch.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = ProcurementExternalOrderSerializer(
            data=request.data, context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            locked = Procurement.objects.select_for_update().get(pk=pk)
            updated = serializer.update(locked, serializer.validated_data)
        return Response(ProcurementReadSerializer(updated).data)


class SupplierListAPIView(APIView):
    """
    GET /api/procurement/suppliers/
    Minimal supplier picker list for the PO creation form — mirrors the
    `suppliers = User.objects.filter(role=User.Role.SUPPLIER)` queryset
    the template view (procurement_add) already uses.
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request):
        if request.user.role not in [
            User.Role.OWNER,
            User.Role.BRANCH_MANAGER,
        ]:
            return Response(
                {'detail': 'Permission denied.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        suppliers = _po_suppliers_for_user(request.user).select_related(
            'supplier_profile'
        )
        data = [
            {
                'id': s.id,
                'name': (
                    getattr(getattr(s, 'supplier_profile', None), 'supplier_name', None)
                    or s.get_full_name()
                    or s.username
                ),
            }
            for s in suppliers
        ]
        return Response(data)


# =============================================================================
# SUPPLIER RETURNS (P17)
# Goods sent back to the supplier that delivered them. The store side is
# Owner / Branch Manager (branch-scoped) to act, and anyone with Procurement
# access to read; the supplier sees and answers its own returns from the
# portal. The rules live in procurement.services — these views only gate who
# may ask and turn a refusal into a message.
# =============================================================================

_RETURN_MANAGERS = (User.Role.OWNER, User.Role.BRANCH_MANAGER)


def _store_returns(user):
    """The returns a back-office user may see (Owner: all; others: their branch)."""
    return scope_queryset(
        SupplierReturn.objects.select_related(
            'procurement', 'supplier', 'supplier__supplier_profile', 'branch',
            'requested_by',
        ),
        user,
    )


@login_required(login_url='accounts:login')
def supplier_return_list(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    returns = _store_returns(request.user).prefetch_related('items__product')

    search_query = request.GET.get('q', '')
    if search_query:
        returns = returns.filter(
            Q(return_number__icontains=search_query) |
            Q(procurement__po_number__icontains=search_query) |
            Q(supplier__supplier_profile__supplier_name__icontains=search_query) |
            Q(supplier__username__icontains=search_query)
        )

    # The shared back-office range control (_daterange.html), same as
    # Purchase Orders/Billing. created_at is a DateTimeField, so local
    # calendar days are turned into aware bounds (see core.timebounds).
    range_start, range_end, active_range = range_from_request(request)
    range_dt_start, range_dt_end = local_range_bounds(range_start or range_end, range_end)
    if range_start:
        returns = returns.filter(created_at__gte=range_dt_start)
    returns = returns.filter(created_at__lt=range_dt_end)

    counts = {status: 0 for status in SupplierReturn.Status.values}
    for status in returns.values_list('status', flat=True):
        counts[status] = counts.get(status, 0) + 1

    status_filter = request.GET.get('status', '')
    if status_filter in SupplierReturn.Status.values:
        returns = returns.filter(status=status_filter)
    else:
        status_filter = ''

    return render(request, 'procurement/supplier_return_list.html', {
        'returns': returns,
        'counts': counts,
        'total_count': sum(counts.values()),
        'status_filter': status_filter,
        'search_query': search_query,
        # _daterange.html: a preset click must not drop the search/filters.
        'active_range': active_range,
        'range_start': range_start or range_end,
        'range_end': range_end,
        'daterange_carry': [('q', search_query), ('status', status_filter)],
        # O-3 — one urlencoded filter string for the status tabs, same fix as
        # Purchase Orders/Deliveries/Billing: a tab's href used to be
        # hand-built and silently dropped other filters.
        'filter_qs': list_filter_querystring(q=search_query),
        'status_tabs': [
            (value, label, counts.get(value, 0))
            for value, label in SupplierReturn.Status.choices
        ],
        'can_manage': (
            request.user.role in _RETURN_MANAGERS
            and user_can_perform_module_action(request.user, PROCUREMENT, EDIT)
        ),
    })


def _return_form_context(proc, posted=None):
    lines = returnable_lines(proc)
    for row in lines:
        # Re-fill what was typed after a rejected submit.
        row['posted_qty'] = (posted or {}).get(f"qty_{row['item'].pk}", '')
    return {
        'procurement': proc,
        'lines': lines,
        'reasons': SupplierReturn.Reason.choices,
        'posted': posted or {},
    }


@login_required(login_url='accounts:login')
def supplier_return_add(request, pk):
    """Raise a return against a received purchase order."""
    if request.user.role not in _RETURN_MANAGERS:
        messages.error(request, 'You do not have permission to return goods to a supplier.')
        return redirect('procurement_list')

    proc = get_object_or_404(
        Procurement.objects.select_related('branch', 'supplier'), pk=pk,
    )
    if not user_can_access_branch(request.user, proc.branch_id):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('procurement_list')
    if proc.status not in (
        Procurement.Status.RECEIVED, Procurement.Status.PARTIALLY_RECEIVED,
    ):
        messages.error(request, 'Only an order that has been received can be returned.')
        return redirect('procurement_detail', pk=pk)

    if request.method != 'POST':
        return render(
            request, 'procurement/supplier_return_form.html', _return_form_context(proc)
        )

    quantities = {}
    for key, value in request.POST.items():
        if key.startswith('qty_') and key[4:].isdigit():
            qty = _safe_nonneg_int(value)
            if qty:
                quantities[int(key[4:])] = qty
    try:
        ret = create_supplier_return(
            proc, request.user,
            reason=request.POST.get('reason', ''),
            notes=request.POST.get('notes', ''),
            quantities=quantities,
        )
    except ReturnError as exc:
        messages.error(request, str(exc))
        return render(
            request, 'procurement/supplier_return_form.html',
            _return_form_context(proc, posted=request.POST),
        )

    from .notifications import notify_return_for_supplier
    notify_return_for_supplier(ret, 'return requested', request.user)
    messages.success(request, f'{ret.return_number} sent to the supplier.')
    return redirect('supplier_return_detail', pk=ret.pk)


# Same display-only-stepper idea as _procurement_progress, sized to
# SupplierReturn's own 4-step happy path. Rejected and Cancelled are both
# "stopped here" outcomes rather than points on the path, so they get the
# banner treatment instead of a dot.
RETURN_PROGRESS_STEPS = ['Requested', 'Acknowledged', 'Shipped', 'Settled']

_RETURN_STEP_BY_STATUS = {
    SupplierReturn.Status.REQUESTED: 0,
    SupplierReturn.Status.ACKNOWLEDGED: 1,
    SupplierReturn.Status.SHIPPED: 2,
    SupplierReturn.Status.SETTLED: 3,
}


def _return_progress(ret):
    stopped = ret.status in (SupplierReturn.Status.REJECTED, SupplierReturn.Status.CANCELLED)
    current = 0 if stopped else _RETURN_STEP_BY_STATUS.get(ret.status, 0)
    return {
        'stopped': stopped,
        'steps': [
            {
                'name': name,
                'done': (not stopped) and i < current,
                'current': (not stopped) and i == current,
            }
            for i, name in enumerate(RETURN_PROGRESS_STEPS)
        ],
    }


@login_required(login_url='accounts:login')
def supplier_return_detail(request, pk):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    ret = get_object_or_404(_store_returns(request.user), pk=pk)
    can_manage = (
        request.user.role in _RETURN_MANAGERS
        and user_can_perform_module_action(request.user, PROCUREMENT, EDIT)
    )
    S = SupplierReturn.Status
    return render(request, 'procurement/supplier_return_detail.html', {
        'ret': ret,
        'return_progress': _return_progress(ret),
        'items': ret.items.select_related(
            'product', 'procurement_item', 'source_receipt_item__receipt',
        ),
        'receipts': ret.replacement_receipts.select_related('received_by'),
        'can_manage': can_manage,
        'can_cancel': (
            request.user.role in _RETURN_MANAGERS
            and user_can_perform_module_action(request.user, PROCUREMENT, DELETE)
            and ret.status in SupplierReturn.CANCELLABLE_STATUSES
        ),
        'can_ship': can_manage and ret.status == S.ACKNOWLEDGED,
        'can_settle': can_manage and ret.status == S.SHIPPED,
        'today': timezone.localdate(),
    })


def _managed_return_or_redirect(request, pk):
    """Shared gate for the store-side POST actions: Owner/Branch Manager, on a
    return inside their branch scope. Returns (ret, None) or (None, redirect)."""
    if request.user.role not in _RETURN_MANAGERS:
        messages.error(request, 'You do not have permission to change supplier returns.')
        return None, redirect('supplier_return_list')
    ret = get_object_or_404(_store_returns(request.user), pk=pk)
    return ret, None


def _run_return_action(request, ret, action, success, *, notify=None):
    """Run a service call, turning ReturnError into a message. Returns True on
    success. `notify` is called with the updated return after it commits."""
    try:
        updated = action()
    except ReturnError as exc:
        messages.error(request, str(exc))
        return False
    if notify:
        notify(updated)
    messages.success(request, success)
    return True


@login_required(login_url='accounts:login')
@require_POST
def supplier_return_cancel(request, pk):
    from .notifications import notify_return_for_supplier
    ret, bounce = _managed_return_or_redirect(request, pk)
    if bounce:
        return bounce
    _run_return_action(
        request, ret, lambda: cancel_return(ret), f'{ret.return_number} cancelled.',
        notify=lambda r: notify_return_for_supplier(r, 'return cancelled', request.user),
    )
    return redirect('supplier_return_detail', pk=pk)


@login_required(login_url='accounts:login')
@require_POST
def supplier_return_ship(request, pk):
    from .notifications import notify_return_for_supplier
    ret, bounce = _managed_return_or_redirect(request, pk)
    if bounce:
        return bounce
    _run_return_action(
        request, ret,
        lambda: ship_return(ret, request.user, note=request.POST.get('shipping_note', '')),
        f'{ret.return_number} shipped back. Warehouse stock updated.',
        notify=lambda r: notify_return_for_supplier(r, 'goods shipped back', request.user),
    )
    return redirect('supplier_return_detail', pk=pk)


@login_required(login_url='accounts:login')
@require_POST
def supplier_return_credit(request, pk):
    from .notifications import notify_return_for_supplier
    ret, bounce = _managed_return_or_redirect(request, pk)
    if bounce:
        return bounce
    _run_return_action(
        request, ret,
        lambda: settle_with_credit(
            ret, request.user,
            reference=request.POST.get('credit_reference', ''),
            amount=request.POST.get('credit_amount', ''),
        ),
        f'{ret.return_number} settled with a credit note.',
        notify=lambda r: notify_return_for_supplier(r, 'settled with a credit note', request.user),
    )
    return redirect('supplier_return_detail', pk=pk)


@login_required(login_url='accounts:login')
@require_POST
def supplier_return_replacement(request, pk):
    """Receive replacement goods for a shipped return (adds warehouse stock)."""
    from .notifications import notify_return_for_supplier
    ret, bounce = _managed_return_or_redirect(request, pk)
    if bounce:
        return bounce

    lines = {}
    for key, value in request.POST.items():
        if key.startswith('qty_') and key[4:].isdigit():
            qty = _safe_nonneg_int(value)
            if qty:
                expiry_raw = request.POST.get(f'expiry_{key[4:]}', '').strip()
                expiry = parse_date(expiry_raw) if expiry_raw else None
                if expiry_raw and expiry is None:
                    messages.error(request, 'Enter a valid expiry date.')
                    return redirect('supplier_return_detail', pk=pk)
                lines[int(key[4:])] = (qty, expiry)

    def _notify(r):
        headline = (
            'replacement received — settled' if r.status == SupplierReturn.Status.SETTLED
            else 'replacement partly received'
        )
        notify_return_for_supplier(r, headline, request.user)

    _run_return_action(
        request, ret,
        lambda: receive_replacement(
            ret, request.user, lines=lines,
            delivered_by=request.POST.get('delivered_by', ''),
        ),
        f'Replacement recorded for {ret.return_number}. Warehouse stock updated.',
        notify=_notify,
    )
    return redirect('supplier_return_detail', pk=pk)


@login_required(login_url='accounts:login')
@require_POST
def supplier_return_respond(request, pk):
    """The supplier acknowledges or rejects a return raised against them."""
    from .notifications import notify_return_response

    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    # Scoped to request.user: another supplier's return is a 404.
    ret = get_object_or_404(SupplierReturn, pk=pk, supplier=request.user)
    acknowledge = request.POST.get('decision') == 'acknowledge'
    try:
        updated = respond_to_return(
            ret, acknowledge=acknowledge, note=request.POST.get('note', ''),
        )
    except ReturnError as exc:
        messages.error(request, str(exc))
        return _supplier_next_redirect(request)

    notify_return_response(updated, acknowledged=acknowledge)
    messages.success(
        request,
        f'{updated.return_number} {"acknowledged" if acknowledge else "rejected"}.',
    )
    return _supplier_next_redirect(request)


class SupplierProductListAPIView(APIView):
    """Active products assigned to one supplier for PO item pickers."""
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request, pk):
        if request.user.role not in [
            User.Role.OWNER,
            User.Role.BRANCH_MANAGER,
        ]:
            return Response(
                {'detail': 'Permission denied.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        supplier = _po_suppliers_for_user(request.user).filter(pk=pk).first()
        if supplier is None:
            return Response(
                {'detail': 'Active supplier not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        links = (
            active_supplier_product_links(supplier)
            .select_related('product')
            .prefetch_related('product__price_tiers')
            .order_by('product__product_name')
        )
        return Response([
            {
                'id': link.product_id,
                'name': link.product.product_name,
                'sku': link.product.sku_code,
                'defaultCost': (
                    link.default_cost_price
                    if link.default_cost_price is not None
                    else link.product.bulk_floor_price
                ),
            }
            for link in links
        ])
