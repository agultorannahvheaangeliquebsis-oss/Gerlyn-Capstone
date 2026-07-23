# procurement/views.py
# Structure:
#   SECTION 1 — original Django template views (untouched, kept for reference)
#   SECTION 2 — new DRF API views (what React calls)

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import get_random_string

from core.scoping import scope_queryset, user_can_access_branch

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import Procurement, ProcurementItem
from .serializers import (
    ProcurementReadSerializer,
    ProcurementWriteSerializer,
    ProcurementStatusSerializer,
)
from inventory.models import Product, Inventory, StockMovement
from accounts.models import User


# =============================================================================
# SECTION 1 — Original Django template views (unchanged)
# These still work for any admin/template-based pages.
# =============================================================================

@login_required(login_url='login')
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

    open_value = sum(
        p.total_cost for p in procurements
        if p.status in [Procurement.Status.DRAFT, Procurement.Status.ORDERED]
    )

    return render(request, 'procurement/procurement_list.html', {
        'procurements': procurements,
        'counts': counts,
        'status_filter': status_filter,
        'open_value': open_value,
        'today': timezone.now().date(),
        'can_create': request.user.role in [
            User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF
        ],
    })


@login_required(login_url='login')
def procurement_detail(request, pk):
    proc = get_object_or_404(
        Procurement.objects.select_related('branch', 'supplier'), pk=pk
    )

    # Scoping the list is not enough — the detail page must check too, or anyone
    # can read another branch's (or a rival supplier's) order by editing the URL.
    if request.user.role == User.Role.SUPPLIER:
        allowed = proc.supplier_id == request.user.id
    else:
        allowed = user_can_access_branch(request.user, proc.branch_id)

    if not allowed:
        messages.error(request, 'That purchase order is not available to you.')
        return redirect('procurement_list')

    items = proc.items.select_related('product').all()
    return render(request, 'procurement/procurement_detail.html', {
        'procurement': proc, 'items': items
    })


@login_required(login_url='login')
def procurement_add(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to create procurement orders.')
        return redirect('procurement_list')

    products  = Product.objects.all()
    suppliers = User.objects.filter(role=User.Role.SUPPLIER)

    from branches.models import Branch
    branches = Branch.objects.all()

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
            for product_id, qty, cost in zip(product_ids, quantities, cost_prices):
                if not product_id or not qty or not cost:
                    continue
                qty  = int(qty)
                cost = float(cost)
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

    return render(request, 'procurement/procurement_form.html', {
        'products': products,
        'suppliers': suppliers,
        'branches': branches,
    })


@login_required(login_url='login')
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

    return redirect('procurement_detail', pk=pk)


# =============================================================================
# DELIVERIES — receive a purchase order into stock.
# Business rule 13: a procurement transaction updates branch inventory only
# after a formal receipt validation, so digital stock matches physical goods.
# =============================================================================

@login_required(login_url='login')
def delivery_list(request):
    """Pick a purchase order to receive."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('home')

    pending = scope_queryset(
        Procurement.objects.select_related('branch', 'supplier')
        .filter(status=Procurement.Status.ORDERED),
        request.user,
    ).order_by('delivery_date')

    return render(request, 'procurement/delivery_list.html', {
        'pending': pending,
        'today': timezone.now().date(),
    })


@login_required(login_url='login')
def delivery_receive(request, pk):
    """Record the quantities actually delivered against a PO."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('home')

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
        received_map = request.POST.getlist('quantity_received')
        item_ids = request.POST.getlist('item_id')

        with transaction.atomic():
            any_short = False
            for item_id, qty_str in zip(item_ids, received_map):
                item = proc.items.select_related('product').get(pk=item_id)
                qty_received = int(qty_str or 0)

                if qty_received != item.quantity_ordered:
                    any_short = True

                item.quantity_received = qty_received
                item.save()

                if qty_received <= 0:
                    continue

                inv, _ = Inventory.objects.get_or_create(
                    product=item.product, branch=proc.branch,
                    defaults={'quantity_on_hand': 0, 'reorder_level': 0},
                )
                before = inv.quantity_on_hand
                inv.quantity_on_hand += qty_received
                inv.save()

                # Audit trail — every stock change is attributable.
                StockMovement.objects.create(
                    inventory=inv,
                    location_type=StockMovement.LocationType.FLOOR,
                    movement_type=StockMovement.MovementType.PROCUREMENT_RECEIVED,
                    quantity_change=qty_received,
                    quantity_before=before,
                    quantity_after=inv.quantity_on_hand,
                    reference_id=proc.pk,
                    notes=f'Delivery for {proc.po_number}',
                    created_by=request.user,
                )

            proc.status = Procurement.Status.RECEIVED
            proc.receipt_flag = (
                Procurement.ReceiptFlag.DISCREPANCY if any_short
                else Procurement.ReceiptFlag.NONE
            )
            proc.save()

        if any_short:
            messages.warning(
                request,
                f'{proc.po_number} received with a discrepancy — '
                f'delivered quantities differ from what was ordered.'
            )
        else:
            messages.success(request, f'{proc.po_number} received in full. Stock updated.')
        return redirect('procurement_detail', pk=pk)

    return render(request, 'procurement/delivery_receive.html', {
        'procurement': proc,
        'items': items,
    })


@login_required(login_url='login')
def receiving_history(request):
    """Everything already received, newest first."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('home')

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

@login_required(login_url='login')
def supplier_portal(request):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('home')

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


@login_required(login_url='login')
def supplier_set_delivery_date(request, pk):
    """The supplier commits to (or revises) a delivery date on their own order."""
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('home')

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

@login_required(login_url='login')
def supplier_list(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'Access denied.')
        return redirect('home')

    suppliers = User.objects.filter(role=User.Role.SUPPLIER).select_related(
        'supplier_profile'
    ).order_by('supplier_profile__supplier_name')

    search_query = request.GET.get('q', '')
    if search_query:
        suppliers = suppliers.filter(
            supplier_profile__supplier_name__icontains=search_query
        )

    return render(request, 'procurement/supplier_list.html', {
        'suppliers': suppliers,
        'search_query': search_query,
        'can_edit': request.user.role == User.Role.OWNER,
    })


@login_required(login_url='login')
def supplier_form(request, pk=None):
    """Add or edit a supplier (Owner only)."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage suppliers.')
        return redirect('supplier_list')

    supplier = get_object_or_404(User, pk=pk, role=User.Role.SUPPLIER) if pk else None
    profile = getattr(supplier, 'supplier_profile', None) if supplier else None

    if request.method == 'POST':
        name = request.POST.get('supplier_name', '').strip()
        username = request.POST.get('username', '').strip()

        if not name or not username:
            messages.error(request, 'Supplier name and username are required.')
            return redirect(request.path)

        with transaction.atomic():
            if supplier is None:
                if User.objects.filter(username=username).exists():
                    messages.error(request, f'Username "{username}" is already taken.')
                    return redirect('supplier_add')
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
            profile.save()

        messages.success(request, f'Supplier "{name}" saved.')
        return redirect('supplier_list')

    return render(request, 'procurement/supplier_form.html', {
        'supplier': supplier,
        'profile': profile,
    })


@login_required(login_url='login')
def supplier_delete(request, pk):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can delete suppliers.')
        return redirect('supplier_list')

    supplier = get_object_or_404(User, pk=pk, role=User.Role.SUPPLIER)
    open_orders = Procurement.objects.filter(supplier=supplier).exclude(
        status__in=[Procurement.Status.RECEIVED, Procurement.Status.CANCELLED]
    ).count()

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
        - Sets quantity_received on each item
        - Updates Inventory.quantity_on_hand for the branch
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

        serializer = ProcurementStatusSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        new_status = serializer.validated_data['status']
        allowed    = self.ALLOWED_TRANSITIONS.get(proc.status, [])

        if new_status not in allowed:
            return Response(
                {'detail': f"Cannot transition from '{proc.status}' to '{new_status}'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        updated    = serializer.update(proc, serializer.validated_data)
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