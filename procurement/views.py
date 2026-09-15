# procurement/views.py
# Structure:
#   SECTION 1 — original Django template views (untouched, kept for reference)
#   SECTION 2 — new DRF API views (what React calls)

import io

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse, HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_POST

from django.db.models import Q

from xhtml2pdf import pisa

from core.scoping import scope_queryset, user_can_access_branch, branch_for_user
from core.permissions import OWNER, BRANCH_MANAGER, STAFF, SUPPLIER, role_required

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import (
    Procurement, ProcurementItem, SupplierProduct, DeliveryScanLog,
)
from .services import transfer_supplier_products, SupplierTransferError
from .serializers import (
    ProcurementReadSerializer,
    ProcurementWriteSerializer,
    ProcurementStatusSerializer,
)
from inventory.models import Product, StockMovement, Warehouse, WarehouseStock
from accounts.models import User


# =============================================================================
# SECTION 1 — Original Django template views (unchanged)
# These still work for any admin/template-based pages.
# =============================================================================

@role_required(OWNER, BRANCH_MANAGER, STAFF, SUPPLIER)
def procurement_list(request):
    procurements = Procurement.objects.select_related('branch', 'supplier', 'owner').all()

    if request.user.role == User.Role.SUPPLIER:
        # Suppliers only ever see the orders addressed to them.
        procurements = procurements.filter(supplier=request.user)
    else:
        # Owner = all branches; manager/staff = their own branch only.
        procurements = scope_queryset(procurements, request.user)

    procurements = procurements.order_by('-order_date')

    status_filter = request.GET.get('status', '')
    counts = {
        'all': procurements.count(),
        'draft': procurements.filter(status=Procurement.Status.DRAFT).count(),
        'ordered': procurements.filter(status=Procurement.Status.ORDERED).count(),
        'received': procurements.filter(status=Procurement.Status.RECEIVED).count(),
        'cancelled': procurements.filter(status=Procurement.Status.CANCELLED).count(),
    }
    if status_filter:
        procurements = procurements.filter(status=status_filter)

    return render(request, 'procurement/procurement_list.html', {
        'procurements': procurements,
        'counts': counts,
        'status_filter': status_filter,
        'today': timezone.now().date(),
        # Staff can now view this list (see core.module_permissions), but
        # creating a PO stays Owner/Manager-only — procurement_add's own role
        # check has always enforced that; this just stops offering a button
        # Staff would only get rejected from now that they can reach the page.
        'can_create': request.user.role in [
            User.Role.OWNER, User.Role.BRANCH_MANAGER
        ],
    })


def _person_name(user):
    """A person's display name, or '' when there is nobody.

    Resolved here rather than in the template because a chained
    `{{ x.get_full_name|default:x.username }}` raises VariableDoesNotExist
    when `x` is null — a filter ARGUMENT is resolved strictly, unlike the
    main variable position, which quietly yields ''. Older purchase orders
    predate created_by and have it unset, so this is the normal case, not
    an edge one.
    """
    if user is None:
        return ''
    return user.get_full_name() or user.username


def _can_read_procurement(user, proc):
    """Scoping the list is not enough — every per-order page must check too,
    or anyone can read another branch's (or a rival supplier's) order by
    editing the URL."""
    if user.role == User.Role.SUPPLIER:
        return proc.supplier_id == user.id
    return user_can_access_branch(user, proc.branch_id)


@login_required(login_url='accounts:login')
def procurement_detail(request, pk):
    proc = get_object_or_404(
        Procurement.objects.select_related('branch', 'supplier'), pk=pk
    )

    if not _can_read_procurement(request.user, proc):
        messages.error(request, 'That purchase order is not available to you.')
        return redirect('procurement_list')

    items = proc.items.select_related('product').all()
    return render(request, 'procurement/procurement_detail.html', {
        'procurement': proc, 'items': items
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

    supplier_profile = getattr(proc.supplier, 'supplier_profile', None)

    return {
        'procurement': proc,
        'items': items,
        'line_sum': line_sum,
        'totals_disagree': line_sum != proc.total_cost,
        'supplier_profile': supplier_profile,
        # Signature-block names, pre-resolved so the template never chains a
        # lookup through a FK that may be null. Any of the three can legally
        # be absent: created_by postdates the older orders, owner is only set
        # when an Owner raised the PO, and a supplier may have no named
        # contact person.
        'prepared_by_name': _person_name(proc.created_by),
        'authorized_by_name': _person_name(proc.owner),
        'supplier_signatory': (
            (supplier_profile.contact_person or supplier_profile.supplier_name)
            if supplier_profile else ''
        ),
        'printed_on': timezone.localdate(),
        'printed_by': _person_name(request.user),
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


def _procurement_form_context(user):
    """Everything procurement_form.html needs, in one place so the error
    re-render below cannot drift from the initial GET."""
    from branches.models import Branch

    user_branch = branch_for_user(user)  # None for Owner; the manager's branch otherwise

    if user_branch:
        # Pinned to one branch: only suppliers assigned to it (or unassigned,
        # i.e. available to every branch) belong in this dropdown — a
        # Branch Manager creating a PO for their branch should never see,
        # or be able to pick, a supplier scoped to a different one.
        branches = Branch.objects.filter(pk=user_branch.pk)
        suppliers = User.objects.filter(role=User.Role.SUPPLIER).filter(
            Q(supplier_profile__branch=user_branch) | Q(supplier_profile__branch__isnull=True)
        )
    else:
        branches = Branch.objects.all()
        suppliers = User.objects.filter(role=User.Role.SUPPLIER)

    return {
        'products': Product.objects.all(),
        'suppliers': suppliers,
        'branches': branches,
        'user_branch': user_branch,
        # localdate(), not now(): the order-date field defaults to this, and
        # now() is UTC under USE_TZ, so late evening in Asia/Manila would
        # prefill tomorrow's date.
        'today': timezone.localdate(),
    }


@login_required(login_url='accounts:login')
def procurement_add(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to create procurement orders.')
        return redirect('procurement_list')

    if request.method == 'POST':
        if request.user.role == User.Role.BRANCH_MANAGER:
            try:
                branch_id = request.user.manager_profile.branch.id
            except Exception:
                messages.error(request, 'No branch assigned to your account.')
                return redirect('procurement_list')
        else:
            branch_id = request.POST.get('branch')

        supplier_id  = request.POST.get('supplier')
        order_date   = request.POST.get('order_date')
        delivery_date = request.POST.get('delivery_date') or None
        product_ids  = request.POST.getlist('product_id')
        quantities   = request.POST.getlist('quantity_ordered')
        cost_prices  = request.POST.getlist('cost_price')

        # Collect the usable lines BEFORE writing anything. This loop used to
        # run inside the transaction with the Procurement already created, so a
        # submission where every row was missing a cost price saved an order
        # with no items and a ₱0.00 total, then reported "created successfully".
        # That is not hypothetical — PO-2026-001 in this database is one, left
        # behind when the cost field silently blanked itself (see the
        # |unlocalize note in procurement_form.html).
        items = []
        for product_id, qty, cost in zip(product_ids, quantities, cost_prices):
            if not product_id or not qty or not cost:
                continue
            items.append((product_id, int(qty), float(cost)))

        if not items:
            messages.error(
                request,
                'The order was not created — every item needs a product, a '
                'quantity and a cost price.'
            )
            return render(request, 'procurement/procurement_form.html',
                          _procurement_form_context(request.user))

        with transaction.atomic():
            owner_user = request.user if request.user.role == User.Role.OWNER else None

            proc = Procurement.objects.create(
                branch_id=branch_id,
                supplier_id=supplier_id,
                owner=owner_user,
                order_date=order_date,
                delivery_date=delivery_date,
                total_cost=0,
                status=Procurement.Status.ORDERED,
            )

            total = 0
            for product_id, qty, cost in items:
                ProcurementItem.objects.create(
                    procurement=proc,
                    product_id=product_id,
                    quantity_ordered=qty,
                    cost_price=cost,
                )
                total += qty * cost

            proc.total_cost = total
            proc.save()

        messages.success(request, 'Procurement order created successfully.')
        return redirect('procurement_list')

    return render(request, 'procurement/procurement_form.html',
                  _procurement_form_context(request.user))


@login_required(login_url='accounts:login')
def procurement_update_status(request, pk):
    proc = get_object_or_404(Procurement, pk=pk)

    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.SUPPLIER]:
        messages.error(request, 'You do not have permission to update this order.')
        return redirect('procurement_list')

    if request.method == 'POST':
        new_status = request.POST.get('status')

        # Receiving moves stock, so it goes through the Deliveries screen where
        # actual quantities are recorded. This guard also stops a repeat POST
        # from adding the same delivery to stock twice.
        if new_status == Procurement.Status.RECEIVED:
            if proc.status == Procurement.Status.RECEIVED:
                messages.error(request, f'{proc.po_number} has already been received.')
                return redirect('procurement_detail', pk=pk)
            return redirect('delivery_receive', pk=pk)

        proc.status = new_status
        proc.save()
        messages.success(request, f'{proc.po_number} is now {new_status}.')

        if new_status == Procurement.Status.CANCELLED:
            from core.module_permissions import PROCUREMENT
            from core.notifications import CATEGORY_PROCUREMENT, PRIORITY_HIGH, notify

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

    return redirect('procurement_detail', pk=pk)


# =============================================================================
# DELIVERIES — receive a purchase order into stock.
# Business rule 13: a procurement transaction updates branch inventory only
# after a formal receipt validation, so digital stock matches physical goods.
# =============================================================================

class _DeliveryStatusError(Exception):
    """Raised inside delivery_receive's atomic block (already processed, or
    a bad/out-of-range received quantity) so the whole block rolls back
    instead of partially crediting warehouse stock for a rejected receipt."""


def _safe_nonneg_int(value):
    """int(value) if it's a clean non-negative integer, else None."""
    if value is None:
        return None
    value = str(value).strip()
    return int(value) if value.isdigit() else None

@login_required(login_url='accounts:login')
def delivery_list(request):
    """Pick a purchase order to receive."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    pending = scope_queryset(
        Procurement.objects.select_related('branch', 'supplier')
        .filter(status=Procurement.Status.ORDERED),
        request.user,
    ).order_by('delivery_date')

    return render(request, 'procurement/delivery_list.html', {
        'pending': pending,
        'today': timezone.now().date(),
    })


@login_required(login_url='accounts:login')
def delivery_receive(request, pk):
    """Record the quantities actually delivered against a PO."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    proc = get_object_or_404(
        Procurement.objects.select_related('branch', 'supplier'), pk=pk
    )

    if not user_can_access_branch(request.user, proc.branch_id):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('delivery_list')

    if proc.status == Procurement.Status.RECEIVED:
        messages.info(request, f'{proc.po_number} was already received.')
        return redirect('procurement_detail', pk=pk)
    if proc.status == Procurement.Status.CANCELLED:
        messages.error(request, f'{proc.po_number} was cancelled.')
        return redirect('procurement_detail', pk=pk)

    items = proc.items.select_related('product').all()

    if request.method == 'POST':
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

        try:
            with transaction.atomic():
                # Re-fetch and lock so a double-submit (double-click, retry)
                # can't both pass the RECEIVED/CANCELLED guard above (read
                # before this transaction opened) and both credit warehouse
                # stock for the same delivery.
                locked_proc = get_object_or_404(
                    Procurement.objects.select_for_update(), pk=pk
                )
                if locked_proc.status == Procurement.Status.RECEIVED:
                    raise _DeliveryStatusError(f'{locked_proc.po_number} was already received.')
                if locked_proc.status == Procurement.Status.CANCELLED:
                    raise _DeliveryStatusError(f'{locked_proc.po_number} was cancelled.')

                any_short = False
                for item_id, qty_str in zip(item_ids, received_map):
                    item = locked_proc.items.select_related('product').get(pk=item_id)
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
                    if not item.is_unexpected and qty_received > item.quantity_ordered:
                        raise _DeliveryStatusError(
                            f'Received quantity for {item.product.product_name} '
                            f'({qty_received}) cannot exceed what was ordered '
                            f'({item.quantity_ordered}). If this product arrived '
                            f'without being ordered, add it as an unexpected item.'
                        )

                    if item.is_unexpected or qty_received != item.quantity_ordered:
                        any_short = True

                    item.quantity_received = qty_received
                    item.save()

                    if qty_received <= 0:
                        continue

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

                locked_proc.status = Procurement.Status.RECEIVED
                locked_proc.receipt_flag = (
                    Procurement.ReceiptFlag.DISCREPANCY if any_short
                    else Procurement.ReceiptFlag.NONE
                )
                locked_proc.save()
        except _DeliveryStatusError as e:
            messages.error(request, str(e))
            return redirect('procurement_detail', pk=pk)

        if any_short:
            messages.warning(
                request,
                f'{proc.po_number} received with a discrepancy — '
                f'delivered quantities differ from what was ordered.'
            )
            from core.module_permissions import DELIVERIES
            from core.notifications import CATEGORY_PROCUREMENT, PRIORITY_HIGH, notify

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
        else:
            messages.success(
                request,
                f'{proc.po_number} received in full. Warehouse stock updated — '
                f'create a Withdrawal Slip to move it onto the floor.',
                extra_tags='sheet',
            )
        return redirect('procurement_detail', pk=pk)

    return render(request, 'procurement/delivery_receive.html', {
        'procurement': proc,
        'items': items,
        # The "add unexpected item" picker. Excludes what's already on the
        # order, so the only thing it can offer is a genuinely new line.
        'addable_products': Product.objects.exclude(
            pk__in=items.values_list('product_id', flat=True)
        ).order_by('product_name'),
        'scan_count': proc.scan_logs.count(),
    })


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

    proc = get_object_or_404(Procurement, pk=pk)
    if not user_can_access_branch(request.user, proc.branch_id):
        return JsonResponse({'ok': False, 'detail': 'Wrong branch.'}, status=403)
    if proc.status in (Procurement.Status.RECEIVED, Procurement.Status.CANCELLED):
        return JsonResponse({'ok': False, 'detail': 'This order is closed.'}, status=400)

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

    proc = get_object_or_404(Procurement, pk=pk)
    if not user_can_access_branch(request.user, proc.branch_id):
        messages.error(request, 'That purchase order belongs to another branch.')
        return redirect('delivery_list')
    if proc.status in (Procurement.Status.RECEIVED, Procurement.Status.CANCELLED):
        messages.error(request, f'{proc.po_number} is closed.')
        return redirect('procurement_detail', pk=pk)

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
        cost_price=link.default_cost_price if link and link.default_cost_price else 0,
        is_unexpected=True,
    )
    messages.success(
        request,
        f'{product.product_name} added as an unexpected item. Enter the quantity '
        f'that arrived, then confirm the receipt.'
    )
    return redirect('delivery_receive', pk=pk)


@login_required(login_url='accounts:login')
def receiving_history(request):
    """Everything already received, newest first."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    received = scope_queryset(
        Procurement.objects.select_related('branch', 'supplier')
        .filter(status=Procurement.Status.RECEIVED),
        request.user,
    ).order_by('-delivery_date')

    return render(request, 'procurement/receiving_history.html', {'received': received})


# =============================================================================
# SUPPLIER PORTAL
# The Supplier is one of the five documented roles. Per the Context Diagram they
# "receive procurement requests and provide delivery information" — so they see
# their own orders and can commit to a delivery date. They deliberately CANNOT
# mark an order Received: that is the store's receipt validation (business
# rule 13), and letting a supplier self-confirm would defeat the control.
# =============================================================================

@login_required(login_url='accounts:login')
def supplier_portal(request):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    orders = Procurement.objects.filter(
        supplier=request.user
    ).select_related('branch').prefetch_related('items__product').order_by('-order_date')

    status_filter = request.GET.get('status', '')
    counts = {
        'all': orders.count(),
        'ordered': orders.filter(status=Procurement.Status.ORDERED).count(),
        'received': orders.filter(status=Procurement.Status.RECEIVED).count(),
        'cancelled': orders.filter(status=Procurement.Status.CANCELLED).count(),
    }
    # A Draft PO hasn't been placed yet — the supplier should never see it.
    orders = orders.exclude(status=Procurement.Status.DRAFT)
    if status_filter:
        orders = orders.filter(status=status_filter)

    today = timezone.now().date()
    open_orders = [o for o in orders if o.status == Procurement.Status.ORDERED]
    open_value = sum(o.total_cost for o in open_orders)
    overdue = sum(
        1 for o in open_orders if o.delivery_date and o.delivery_date < today
    )

    return render(request, 'procurement/supplier_portal.html', {
        'orders': orders,
        'counts': counts,
        'status_filter': status_filter,
        'open_count': len(open_orders),
        'open_value': open_value,
        'overdue': overdue,
        'today': today,
        'profile': getattr(request.user, 'supplier_profile', None),
    })


@login_required(login_url='accounts:login')
def supplier_set_delivery_date(request, pk):
    """The supplier commits to (or revises) a delivery date on their own order."""
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    proc = get_object_or_404(Procurement, pk=pk, supplier=request.user)

    if proc.status != Procurement.Status.ORDERED:
        messages.error(request, f'{proc.po_number} is no longer open.')
        return redirect('supplier_portal')

    if request.method == 'POST':
        new_date = request.POST.get('delivery_date')
        if not new_date:
            messages.error(request, 'Please choose a delivery date.')
            return redirect('supplier_portal')
        proc.delivery_date = new_date
        proc.save()
        messages.success(
            request,
            f'Thanks — {proc.po_number} is confirmed for delivery on {new_date}.'
        )

    return redirect('supplier_portal')


# =============================================================================
# SUPPLIERS (managed by the store)
# =============================================================================

@login_required(login_url='accounts:login')
def _supplier_list_context(request):
    from django.db.models import Count

    suppliers = User.objects.filter(role=User.Role.SUPPLIER).select_related(
        'supplier_profile'
    ).annotate(
        # Drives the Products column, which links into the per-supplier
        # product sub-view. Annotated rather than counted per row in the
        # template, which would be one query per supplier.
        product_count=Count('supplied_products', distinct=True)
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
        'can_edit': request.user.role == User.Role.OWNER,
        # The add/edit modal assigns a supplier to a branch (or to none, meaning
        # every branch). Without this the modal's select renders empty and a
        # saved edit would blank the assignment the list column shows.
        'branches': Branch.objects.all(),
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

    # Both checks were already here — the first returned to request.path, the
    # second sat inside the atomic block and returned before writing anything.
    # Collected instead of redirected so the typed form survives.
    errors = []
    if not name or not username:
        errors.append('Supplier name and username are required.')
    elif supplier is None and User.objects.filter(username=username).exists():
        errors.append(f'Username "{username}" is already taken.')

    if not errors:
        with transaction.atomic():
            if supplier is None:
                # Suppliers get a random password; the Owner shares a reset link
                # if the supplier ever needs to log in themselves.
                supplier = User.objects.create_user(
                    username=username,
                    email=request.POST.get('email', ''),
                    password=get_random_string(20),
                    role=User.Role.SUPPLIER,
                )
                # accounts.signals creates a blank profile on user creation.
                profile = supplier.supplier_profile
            else:
                supplier.username = username
                supplier.email = request.POST.get('email', '')
                supplier.save()

            profile.supplier_name = name
            profile.contact_person = request.POST.get('contact_person', '')
            profile.contact_number = request.POST.get('contact_number', '')
            profile.supplier_category = request.POST.get('supplier_category', '')
            profile.address = request.POST.get('address', '')
            profile.branch_id = request.POST.get('branch') or None
            profile.save()

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
# SUPPLIER DETAIL — the per-supplier product sub-view.
#
# Which products a supplier carries used to be answerable only by reading back
# through past purchase orders. SupplierProduct makes it a real, editable
# mapping; these views are its UI.
# -----------------------------------------------------------------------------

@login_required(login_url='accounts:login')
def supplier_detail(request, pk):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    supplier = get_object_or_404(
        User.objects.select_related('supplier_profile'),
        pk=pk, role=User.Role.SUPPLIER,
    )

    links = SupplierProduct.objects.filter(supplier=supplier).select_related(
        'product', 'product__category'
    )

    # Products this supplier doesn't already carry — the "Add Product" picker.
    # Excluding the existing ones up front is what stops a user picking a
    # product that would only bounce off the unique constraint.
    available = Product.objects.exclude(
        pk__in=links.values_list('product_id', flat=True)
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
        'available_products': available,
        'other_suppliers': other_suppliers,
        'can_edit': request.user.role == User.Role.OWNER,
        'recent_orders': Procurement.objects.filter(
            supplier=supplier
        ).select_related('branch').order_by('-order_date')[:5],
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

    cost = request.POST.get('default_cost_price') or None
    lead = request.POST.get('lead_time_days') or None

    # get_or_create rather than create: the picker already excludes what the
    # supplier carries, but a stale form or a double-submit can still arrive
    # with a duplicate, and that should be a no-op rather than a 500 off the
    # unique constraint.
    link, created = SupplierProduct.objects.get_or_create(
        supplier=supplier, product=product,
        defaults={
            'supplier_sku': request.POST.get('supplier_sku', '').strip(),
            'default_cost_price': cost,
            'lead_time_days': lead,
            'is_preferred': bool(request.POST.get('is_preferred')),
        },
    )

    if created:
        messages.success(request, f'{product.product_name} added to this supplier.')
    else:
        messages.info(request, f'{product.product_name} is already on this supplier.')
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
    link.delete()
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
        messages.error(request, 'Only the Owner can delete suppliers.')
        return redirect('supplier_list')

    supplier = get_object_or_404(User, pk=pk, role=User.Role.SUPPLIER)
    open_orders = Procurement.objects.filter(supplier=supplier).exclude(
        status__in=[Procurement.Status.RECEIVED, Procurement.Status.CANCELLED]
    ).count()
    # Deleting cascades SupplierProduct away silently, so surface the count
    # and point at the transfer flow — moving the catalog to another supplier
    # is almost always what's actually wanted before retiring one.
    linked_products = SupplierProduct.objects.filter(supplier=supplier).count()

    if request.method == 'POST':
        if open_orders:
            messages.error(
                request,
                f'Cannot delete — this supplier still has {open_orders} open order(s).'
            )
            return redirect('supplier_list')
        name = getattr(supplier.supplier_profile, 'supplier_name', supplier.username)
        supplier.delete()
        messages.success(request, f'Supplier "{name}" deleted.')
        return redirect('supplier_list')

    return render(request, 'procurement/supplier_confirm_delete.html', {
        'supplier': supplier,
        'open_orders': open_orders,
        'linked_products': linked_products,
    })


# =============================================================================
# SECTION 2 — DRF API Views (what React calls)
# Base URL: /api/procurement/
# =============================================================================

class ProcurementListCreateAPIView(APIView):
    """
    GET  /api/procurement/        -> list all POs (filtered by role)
    POST /api/procurement/        -> create a new PO
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self, user):
        qs = Procurement.objects.select_related(
            'branch', 'supplier', 'supplier__supplier_profile',
            'owner', 'owner__owner_profile',
            'created_by', 'created_by__staff_profile',
            'created_by__manager_profile',
        ).prefetch_related('items__product').order_by('-id')

        # Role-based filtering — mirrors the original template view logic
        if user.role in [User.Role.BRANCH_MANAGER, User.Role.STAFF]:
            return scope_queryset(qs, user)
        elif user.role == User.Role.SUPPLIER:
            return qs.filter(supplier=user)
        # OWNER sees all
        return qs

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

        serializer = ProcurementWriteSerializer(
            data=request.data,
            context={'request': request}
        )
        if serializer.is_valid():
            proc           = serializer.save()
            read_serializer = ProcurementReadSerializer(proc)
            return Response(read_serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ProcurementDetailAPIView(APIView):
    """
    GET   /api/procurement/<pk>/   -> retrieve single PO
    DELETE /api/procurement/<pk>/  -> cancel a Draft PO only
    """
    permission_classes = [IsAuthenticated]

    def get_object(self, pk):
        return get_object_or_404(
            Procurement.objects.select_related(
                'branch', 'supplier', 'supplier__supplier_profile',
                'owner', 'owner__owner_profile',
                'created_by', 'created_by__staff_profile',
            ).prefetch_related('items__product'),
            pk=pk
        )

    def _can_access(self, user, proc):
        if user.role == User.Role.SUPPLIER:
            return proc.supplier_id == user.id
        return user_can_access_branch(user, proc.branch_id)

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
    permission_classes = [IsAuthenticated]

    # Valid role -> allowed transitions map
    ALLOWED_TRANSITIONS = {
        Procurement.Status.DRAFT: [Procurement.Status.ORDERED, Procurement.Status.CANCELLED],
        Procurement.Status.ORDERED: [Procurement.Status.RECEIVED, Procurement.Status.CANCELLED],
        Procurement.Status.RECEIVED: [],
        Procurement.Status.CANCELLED: [],
    }

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

        with transaction.atomic():
            # Re-fetch and lock so a double-submit (double-click, retry)
            # can't both pass this same-transition check and both apply the
            # RECEIVED branch's stock credit for one delivery.
            locked_proc = get_object_or_404(
                Procurement.objects.select_for_update(), pk=pk
            )
            allowed = self.ALLOWED_TRANSITIONS.get(locked_proc.status, [])
            if new_status not in allowed:
                return Response(
                    {'detail': f"Cannot transition from '{locked_proc.status}' to '{new_status}'."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            updated = serializer.update(locked_proc, serializer.validated_data)

        read_serializer = ProcurementReadSerializer(updated)
        return Response(read_serializer.data)


class SupplierListAPIView(APIView):
    """
    GET /api/procurement/suppliers/
    Minimal supplier picker list for the PO creation form — mirrors the
    `suppliers = User.objects.filter(role=User.Role.SUPPLIER)` queryset
    the template view (procurement_add) already uses.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        suppliers = User.objects.filter(
            role=User.Role.SUPPLIER
        ).select_related('supplier_profile')
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