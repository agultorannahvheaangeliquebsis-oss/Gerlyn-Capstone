# procurement/views.py
# Structure:
#   SECTION 1 — original Django template views (untouched, kept for reference)
#   SECTION 2 — new DRF API views (what React calls)

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction

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
from inventory.models import Product, Inventory
from accounts.models import User


# =============================================================================
# SECTION 1 — Original Django template views (unchanged)
# These still work for any admin/template-based pages.
# =============================================================================

@login_required(login_url='login')
def procurement_list(request):
    procurements = Procurement.objects.select_related('branch', 'supplier', 'owner').all()

    if request.user.role == User.Role.BRANCH_MANAGER:
        try:
            branch = request.user.manager_profile.branch
            procurements = procurements.filter(branch=branch)
        except Exception:
            procurements = procurements.none()
    elif request.user.role == User.Role.SUPPLIER:
        procurements = procurements.filter(supplier=request.user)

    return render(request, 'procurement/procurement_list.html', {'procurements': procurements})


@login_required(login_url='login')
def procurement_detail(request, pk):
    proc  = get_object_or_404(Procurement, pk=pk)
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

        with transaction.atomic():
            proc.status = new_status
            proc.save()

            if new_status == Procurement.Status.RECEIVED:
                for item in proc.items.all():
                    item.quantity_received = item.quantity_ordered
                    item.save()

                    inv, created = Inventory.objects.get_or_create(
                        product=item.product,
                        branch=proc.branch,
                        defaults={'quantity_on_hand': 0, 'reorder_level': 0}
                    )
                    inv.quantity_on_hand += item.quantity_ordered
                    inv.save()

        messages.success(request, 'Status updated.')

    return redirect('procurement_detail', pk=pk)


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
        if user.role == User.Role.BRANCH_MANAGER:
            try:
                branch = user.manager_profile.branch
                return qs.filter(branch=branch)
            except Exception:
                return qs.none()
        elif user.role == User.Role.SUPPLIER:
            return qs.filter(supplier=user)
        # OWNER and STAFF see all
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

    def get(self, request, pk):
        proc       = self.get_object(pk)
        serializer = ProcurementReadSerializer(proc)
        return Response(serializer.data)

    def delete(self, request, pk):
        proc = self.get_object(pk)

        if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
            return Response(
                {'detail': 'Permission denied.'},
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

    Allowed transitions:
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

        if request.user.role not in [
            User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.SUPPLIER
        ]:
            return Response(
                {'detail': 'Permission denied.'},
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