from rest_framework import viewsets, status, filters, mixins
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import PermissionDenied
from django.db.models import Q
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from core.refdata import bust_categories_cache
from .models import (
    Product, ProductCategory, Warehouse, WarehouseStock, Inventory,
    Batch, StockTransfer, StockTransferItem, StockMovement,
)
from .serializers import (
    ProductSerializer,
    ProductWriteSerializer,
    ProductCategorySerializer,
    WarehouseSerializer,
    WarehouseStockSerializer,
    InventorySerializer,
    BatchSerializer,
    StockTransferSerializer,
    StockTransferWriteSerializer,
    StockTransferApproveSerializer,
    StockTransferAcknowledgeSerializer,
    StockMovementSerializer,
)
from accounts.models import User
from .views import _InsufficientStockError
from .services import (
    StockTransferReceiptError, reconcile_transfer_receipt,
    record_transfer_event, transfers_visible_to,
)


# Branch scoping lives in core.scoping so the API and the template views can
# never drift apart — this is security-critical logic, it gets ONE definition.
from core.scoping import (  # noqa: E402
    branch_for_user as get_user_branch,
    branch_ids_for_user,
    scope_queryset,
    user_can_access_branch,
)
from core.permissions import HasModuleAccess, INTERNAL_ROLES  # OWNER, BRANCH_MANAGER, STAFF


class ProductCategoryViewSet(viewsets.ModelViewSet):
    """
    /api/categories/
    GET    → list all categories
    POST/PUT/PATCH/DELETE → Owner/Branch Manager only
    """
    queryset = ProductCategory.objects.all().order_by('category_name')
    serializer_class = ProductCategorySerializer
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get_permissions(self):
        """Categories are public read (storefront filter dropdown); writes stay gated."""
        if self.action in ['list', 'retrieve']:
            return [AllowAny(), HasModuleAccess()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        denied = check_owner_or_manager(request)
        if denied:
            return denied
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        denied = check_owner_or_manager(request)
        if denied:
            return denied
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        denied = check_owner_or_manager(request)
        if denied:
            return denied
        category = self.get_object()
        # Same guard category_delete (the template view) applies — products
        # are SET_NULL, which would silently uncategorise the catalog.
        product_count = category.products.count()
        if product_count:
            return Response(
                {'detail': f'Cannot delete "{category.category_name}" — '
                           f'{product_count} product'
                           f'{"s" if product_count != 1 else ""} still use it.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        response = super().destroy(request, *args, **kwargs)
        bust_categories_cache()
        return response


class ProductViewSet(viewsets.ModelViewSet):
    """
    /api/products/
    GET    /api/products/          → list all products (with search + filter)
    GET    /api/products/<id>/     → retrieve single product
    POST   /api/products/          → create product (Owner/Manager only)
    PUT    /api/products/<id>/     → full update (Owner/Manager only)
    PATCH  /api/products/<id>/     → partial update (Owner/Manager only)
    DELETE /api/products/<id>/     → delete (Owner/Manager only)
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]
    filter_backends = [filters.SearchFilter]
    search_fields = ['product_name', 'sku_code']

    def get_permissions(self):
        """Products are public read (storefront catalog); writes stay gated."""
        if self.action in ['list', 'retrieve']:
            return [AllowAny(), HasModuleAccess()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = Product.objects.select_related(
            'category'
        ).prefetch_related(
            'inventory_records__branch'
        ).all()

        # ?category=Canned Goods  → filter by category name
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(
                category__category_name__icontains=category
            )

        # ?search=tuna  → search by product name or SKU (handled by filter_backends)
        # ?branch=Tarcan → filter products available at a specific branch (internal, by name)
        branch = self.request.query_params.get('branch')
        if branch:
            queryset = queryset.filter(
                inventory_records__branch__branch_name__icontains=branch,
                inventory_records__quantity_on_hand__gt=0
            ).distinct()

        # ?branchId=3 → storefront: exact-id filter, also drives stockAtBranch below
        branch_id = self.request.query_params.get('branchId')
        if branch_id:
            queryset = queryset.filter(
                inventory_records__branch_id=branch_id,
                inventory_records__quantity_on_hand__gt=0
            ).distinct()

        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        branch_id = self.request.query_params.get('branchId')
        if branch_id:
            context['branch_id'] = branch_id
        return context

    def get_serializer_class(self):
        """
        Use the read serializer for GET requests (has computed fields).
        Use the write serializer for POST/PUT/PATCH (accepts camelCase input).
        """
        if self.action in ['create', 'update', 'partial_update']:
            return ProductWriteSerializer
        return ProductSerializer

    def check_write_permission(self, request):
        """Only Owners and Branch Managers can create/edit/delete products."""
        if request.user.role not in [
            User.Role.OWNER, User.Role.BRANCH_MANAGER
        ]:
            return Response(
                {'detail': 'You do not have permission to modify products.'},
                status=status.HTTP_403_FORBIDDEN
            )
        return None

    def create(self, request, *args, **kwargs):
        denied = self.check_write_permission(request)
        if denied:
            return denied
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        denied = self.check_write_permission(request)
        if denied:
            return denied
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        denied = self.check_write_permission(request)
        if denied:
            return denied
        return super().destroy(request, *args, **kwargs)


def check_owner_or_manager(request):
    """Shared write-gate for warehouse-side endpoints, same rule as Product."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        return Response(
            {'detail': 'You do not have permission to modify this resource.'},
            status=status.HTTP_403_FORBIDDEN
        )
    return None


class WarehouseViewSet(viewsets.ModelViewSet):
    """
    /api/warehouses/
    GET → list/retrieve (any authenticated user)
    POST/PUT/PATCH/DELETE → Owner/Branch Manager only
    """
    serializer_class = WarehouseSerializer
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get_queryset(self):
        if self.request.user.role not in INTERNAL_ROLES:
            return Warehouse.objects.none()
        qs = Warehouse.objects.select_related('branch').all()
        return scope_queryset(qs, self.request.user, 'branch')

    def _check_branch(self, serializer):
        branch = serializer.validated_data.get('branch')
        if branch and not user_can_access_branch(self.request.user, branch.id):
            raise PermissionDenied('You do not have access to that branch.')

    def perform_create(self, serializer):
        self._check_branch(serializer)
        serializer.save()

    def perform_update(self, serializer):
        self._check_branch(serializer)
        serializer.save()

    def create(self, request, *args, **kwargs):
        denied = check_owner_or_manager(request)
        if denied:
            return denied
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        denied = check_owner_or_manager(request)
        if denied:
            return denied
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        denied = check_owner_or_manager(request)
        if denied:
            return denied
        return super().destroy(request, *args, **kwargs)


class WarehouseStockViewSet(mixins.UpdateModelMixin, viewsets.ReadOnlyModelViewSet):
    """
    /api/warehouse-stock/
    Mirrors warehouse_stock_list: Owner sees every warehouse, Manager/Staff
    are scoped to their own branch's warehouse.
    """
    serializer_class = WarehouseStockSerializer
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get_queryset(self):
        if self.request.user.role not in INTERNAL_ROLES:
            return WarehouseStock.objects.none()
        qs = WarehouseStock.objects.select_related(
            'product', 'product__category', 'warehouse', 'warehouse__branch'
        ).all()
        # scope_queryset, not `if user_branch:`. branch_for_user() returns None
        # for BOTH an Owner (sees every branch) and a Manager/Staff whose
        # profile has no branch — and a NULL branch is reachable, since
        # StaffProfile.branch / BranchManagerProfile.branch are
        # on_delete=SET_NULL. The old form treated the second case like the
        # first and silently handed that account every branch's stock.
        # scope_queryset goes through branch_ids_for_user, which returns None
        # only for the Owner and [] for an unassigned account — it fails CLOSED.
        return scope_queryset(qs, self.request.user, 'warehouse__branch')

    def update(self, request, *args, **kwargs):
        denied = check_owner_or_manager(request)
        if denied:
            return denied
        instance = self.get_object()
        for field_name in ('quantityOnHand', 'quantity_on_hand'):
            if field_name not in request.data:
                continue
            submitted = request.data[field_name]
            if isinstance(submitted, bool):
                submitted_quantity = None
            else:
                try:
                    submitted_quantity = int(submitted)
                except (TypeError, ValueError):
                    submitted_quantity = None
            if submitted_quantity != instance.quantity_on_hand:
                return Response(
                    {
                        field_name: (
                            'Warehouse quantity is read-only. Use the appropriate '
                            'audited receiving or stock-movement workflow.'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return super().update(request, *args, **kwargs)


class InventoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    /api/inventory/
    Store-floor stock. Read-only — rows are created by procurement receipt
    or a released StockTransfer, never directly. Mirrors inventory_list.
    """
    serializer_class = InventorySerializer
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get_queryset(self):
        if self.request.user.role not in INTERNAL_ROLES:
            return Inventory.objects.none()
        qs = Inventory.objects.select_related('product', 'branch').all()
        # Fails closed for an unassigned Manager/Staff — see WarehouseStockViewSet.
        return scope_queryset(qs, self.request.user, 'branch')


class BatchViewSet(viewsets.ReadOnlyModelViewSet):
    """
    /api/batches/
    FEFO expiry tracking. Read-only — Batches are auto-created when a
    Procurement is marked Received. Ordered nearest-expiry-first (see
    Batch.Meta.ordering). Supports ?status=Expired|Expiring Soon|Fresh.
    """
    serializer_class = BatchSerializer
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get_queryset(self):
        if self.request.user.role not in INTERNAL_ROLES:
            return Batch.objects.none()
        qs = Batch.objects.select_related('product', 'branch').all()
        # Fails closed for an unassigned Manager/Staff — see WarehouseStockViewSet.
        qs = scope_queryset(qs, self.request.user, 'branch')

        status_filter = self.request.query_params.get('status')
        if status_filter:
            # expiry_status is a computed property, not a DB field —
            # filter in Python after the queryset is evaluated.
            ids = [b.id for b in qs if b.expiry_status == status_filter]
            qs = qs.filter(id__in=ids)
        return qs


class StockMovementListAPIView(APIView):
    """
    GET /api/stock-movements/?product=<id>&branch=<id>
    Read-only audit log.
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request):
        if request.user.role not in INTERNAL_ROLES:
            return Response([])

        qs = StockMovement.objects.select_related(
            'inventory__product', 'inventory__branch',
            'warehouse_stock__product', 'warehouse_stock__warehouse__branch',
            'batch', 'created_by',
        ).all()

        # A movement hangs off either a floor Inventory row or a WarehouseStock
        # row, so the branch is reached by two different paths and this can't go
        # through scope_queryset (one branch_field). Same guarantee though:
        # branch_ids_for_user returns None only for the Owner, and [] — which
        # matches no rows — for a Manager/Staff with no branch assigned.
        branch_ids = branch_ids_for_user(request.user)
        if branch_ids is not None:
            qs = qs.filter(
                Q(inventory__branch_id__in=branch_ids) |
                Q(warehouse_stock__warehouse__branch_id__in=branch_ids)
            )

        product_id = request.query_params.get('product')
        if product_id:
            qs = qs.filter(
                Q(inventory__product_id=product_id) |
                Q(warehouse_stock__product_id=product_id)
            )

        serializer = StockMovementSerializer(qs, many=True)
        return Response(serializer.data)


# =============================================================================
# STOCK TRANSFER (Withdrawal Slip) — full request/approve/release/acknowledge
# workflow. Mirrors inventory/views.py: transfer_list, transfer_create,
# transfer_approve, transfer_complete. The Acknowledge step has no template
# view yet, but the model already carries the fields for it, so it's
# implemented directly against those fields (see StockTransferAcknowledgeSerializer).
# =============================================================================

class StockTransferListCreateAPIView(APIView):
    """
    GET  /api/stock-transfers/   → list, scoped like transfer_list
    POST /api/stock-transfers/   → create, mirrors transfer_create
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request):
        if request.user.role not in INTERNAL_ROLES:
            return Response([])

        qs = StockTransfer.objects.select_related(
            'source_branch', 'destination_branch', 'requested_by',
            'approved_by', 'acknowledged_by', 'warehouse',
        ).prefetch_related('items__product', 'events__actor').all()

        # Fails closed for an unassigned Manager/Staff — see WarehouseStockViewSet.
        qs = transfers_visible_to(request.user, qs)

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        purpose_filter = request.query_params.get('purpose')
        if purpose_filter:
            qs = qs.filter(purpose=purpose_filter)

        serializer = StockTransferSerializer(qs.order_by('-created_at'), many=True)
        return Response(serializer.data)

    def post(self, request):
        if request.user.role not in [
            User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER
        ]:
            return Response(
                {'detail': 'You do not have permission to create stock transfers or write-offs.'},
                status=status.HTTP_403_FORBIDDEN
            )

        user_branch = get_user_branch(request.user)
        serializer = StockTransferWriteSerializer(
            data=request.data,
            context={'request': request, 'user_branch_id': user_branch.id if user_branch else None},
        )
        if serializer.is_valid():
            transfer = serializer.save()
            return Response(
                StockTransferSerializer(transfer).data, status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class StockTransferDetailAPIView(APIView):
    """GET /api/stock-transfers/<pk>/"""
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request, pk):
        transfer = get_object_or_404(
            StockTransfer.objects.select_related(
                'source_branch', 'destination_branch', 'requested_by',
                'approved_by', 'acknowledged_by', 'warehouse',
            ).prefetch_related('items__product', 'events__actor'),
            pk=pk
        )
        # A slip concerns two branches, so either side may view it — mirrors
        # transfer_detail's template-layer check.
        if not (user_can_access_branch(request.user, transfer.source_branch_id)
                or user_can_access_branch(request.user, transfer.destination_branch_id)):
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(StockTransferSerializer(transfer).data)


class StockTransferApproveAPIView(APIView):
    """
    PATCH /api/stock-transfers/<pk>/approve/
    Body: { "action": "approve", "items": { "<itemId>": <approvedQty>, ... } }
         | { "action": "reject" }
    Mirrors transfer_approve exactly, including the warehouse-stock check.
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def patch(self, request, pk):
        if request.user.role not in [User.Role.BRANCH_MANAGER, User.Role.OWNER]:
            return Response(
                {'detail': 'Only Branch Managers and Owners can approve transfers.'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = StockTransferApproveSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        with transaction.atomic():
            transfer = get_object_or_404(
                StockTransfer.objects.select_for_update(), pk=pk,
            )
            if not user_can_access_branch(request.user, transfer.source_branch_id):
                return Response(
                    {'detail': 'That stock transfer or write-off belongs to another branch.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if transfer.status != StockTransfer.Status.PENDING:
                return Response(
                    {'detail': 'This transfer has already been processed.'},
                    status=status.HTTP_400_BAD_REQUEST,
            )

            if data['action'] == 'approve':
                try:
                    items_map = {int(k): v for k, v in data['items'].items()}
                except (TypeError, ValueError):
                    return Response(
                        {'items': 'Every item key must be a valid transfer-item id.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                items = list(transfer.items.select_related('product').all())
                if set(items_map) != {item.pk for item in items}:
                    return Response(
                        {'items': 'Provide an approved quantity for every transfer item only.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                is_restock = transfer.is_warehouse_floor_transfer

                for item in items:
                    approved_qty = items_map.get(item.id, 0)
                    if is_restock:
                        stock = WarehouseStock.objects.select_for_update().filter(
                            warehouse=transfer.warehouse, product=item.product
                        ).first()
                        location_label = 'warehouse'
                    else:
                        stock = Inventory.objects.select_for_update().filter(
                            branch=transfer.source_branch, product=item.product,
                        ).first()
                        location_label = 'floor'
                    if stock is None:
                        transaction.set_rollback(True)
                        return Response(
                            {'detail': f'{item.product.product_name} not found in {location_label} stock.'},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    if approved_qty > stock.quantity_on_hand:
                        transaction.set_rollback(True)
                        return Response(
                            {'detail': (
                                f'Not enough {location_label} stock for '
                                f'{item.product.product_name}. Available: {stock.quantity_on_hand}'
                            )},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    item.quantity_approved = approved_qty
                    item.save()

                previous = transfer.status
                transfer.status = StockTransfer.Status.APPROVED
                transfer.approved_by = request.user
                transfer.save()
                record_transfer_event(
                    transfer, previous, transfer.status, request.user, 'Request approved.',
                )

            else:  # reject
                previous = transfer.status
                transfer.status = StockTransfer.Status.REJECTED
                transfer.approved_by = request.user
                transfer.save()
                record_transfer_event(
                    transfer, previous, transfer.status, request.user,
                    request.data.get('reason') or 'Request rejected.',
                )

        return Response(StockTransferSerializer(transfer).data)


class StockTransferReleaseAPIView(APIView):
    """
    PATCH /api/stock-transfers/<pk>/release/
    Mirrors transfer_complete: deducts warehouse stock, credits floor
    inventory at the destination branch, logs both StockMovements.
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def patch(self, request, pk):
        if request.user.role not in [User.Role.BRANCH_MANAGER, User.Role.OWNER]:
            return Response(
                {'detail': 'Only Branch Managers and Owners can release transfers.'},
                status=status.HTTP_403_FORBIDDEN
            )

        transfer = get_object_or_404(StockTransfer, pk=pk)
        if not user_can_access_branch(request.user, transfer.source_branch_id):
            return Response(
                {'detail': 'That stock transfer belongs to another branch.'},
                status=status.HTTP_403_FORBIDDEN
            )
        if transfer.status != StockTransfer.Status.APPROVED:
            return Response(
                {'detail': 'Transfer must be approved before it can be released.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if transfer.is_write_off:
            return self._complete_write_off(request, transfer.pk)

        try:
            with transaction.atomic():
                transfer = StockTransfer.objects.select_for_update().get(pk=pk)
                if not user_can_access_branch(request.user, transfer.source_branch_id):
                    return Response(
                        {'detail': 'That stock transfer belongs to another branch.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                if transfer.status != StockTransfer.Status.APPROVED:
                    return Response(
                        {'detail': 'Transfer must be approved before it can be released.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                for item in transfer.items.select_related('product').all():
                    if item.quantity_approved <= 0:
                        continue
                    item.quantity_released = item.quantity_approved
                    item.release_method = StockTransferItem.ReleaseMethod.MANUAL
                    item.save(update_fields=['quantity_released', 'release_method'])

                    # Locked + re-checked against the CURRENT quantity_on_hand
                    # rather than trusting the snapshot from approval time —
                    # mirrors the template path's transfer_complete fix.
                    ws = get_object_or_404(
                        WarehouseStock.objects.select_for_update(),
                        warehouse=transfer.warehouse, product=item.product
                    )
                    batch_available = Batch.available_quantity(
                        item.product, transfer.source_branch,
                        Batch.Location.WAREHOUSE,
                    )
                    available = (
                        ws.quantity_on_hand if batch_available is None
                        else min(ws.quantity_on_hand, batch_available)
                    )
                    if item.quantity_approved > available:
                        raise _InsufficientStockError(
                            f'Not enough warehouse stock for {item.product.product_name} to '
                            f'release this transfer — available: {available}, '
                            f'approved: {item.quantity_approved}.'
                        )
                    qty_before_warehouse = ws.quantity_on_hand
                    ws.quantity_on_hand -= item.quantity_approved
                    ws.save()

                    StockMovement.objects.create(
                        warehouse_stock=ws,
                        location_type=StockMovement.LocationType.WAREHOUSE,
                        movement_type=StockMovement.MovementType.WAREHOUSE_TO_FLOOR,
                        quantity_change=-item.quantity_approved,
                        quantity_before=qty_before_warehouse,
                        quantity_after=ws.quantity_on_hand,
                        reference_id=transfer.pk,
                        notes=f'Transfer {transfer.slip_number}: warehouse dispatch',
                        created_by=request.user,
                    )

                    if transfer.destination_branch:
                        inv, _ = Inventory.objects.select_for_update().get_or_create(
                            product=item.product,
                            branch=transfer.destination_branch,
                            defaults={'quantity_on_hand': 0, 'reorder_level': 0}
                        )
                        qty_before_floor = inv.quantity_on_hand
                        inv.quantity_on_hand += item.quantity_approved
                        inv.save()

                        StockMovement.objects.create(
                            inventory=inv,
                            location_type=StockMovement.LocationType.FLOOR,
                            movement_type=StockMovement.MovementType.WAREHOUSE_TO_FLOOR,
                            quantity_change=item.quantity_approved,
                            quantity_before=qty_before_floor,
                            quantity_after=inv.quantity_on_hand,
                            reference_id=transfer.pk,
                            notes=f'Transfer {transfer.slip_number}: received on floor',
                            created_by=request.user,
                        )

                        # Batches: consume the source warehouse's
                        # nearest-expiry lots and create matching floor
                        # batches — mirrors inventory.views.transfer_complete,
                        # so a lot's real expiry survives a release through
                        # this API path too.
                        consumed = Batch.consume_fefo(
                            item.product, transfer.source_branch,
                            Batch.Location.WAREHOUSE, item.quantity_approved,
                        )
                        for source_batch, taken in consumed:
                            Batch.objects.create(
                                product=item.product,
                                branch=transfer.destination_branch,
                                location=Batch.Location.FLOOR,
                                source_batch=source_batch,
                                procurement=source_batch.procurement,
                                quantity=taken,
                                received_date=timezone.localdate(),
                                expiry_date=source_batch.expiry_date,
                                notes=f'Restocked via {transfer.slip_number}',
                            )
                        untracked = item.quantity_approved - sum(t for _, t in consumed)
                        if untracked > 0:
                            Batch.objects.create(
                                product=item.product,
                                branch=transfer.destination_branch,
                                location=Batch.Location.FLOOR,
                                quantity=untracked,
                                received_date=timezone.localdate(),
                                expiry_date=None,
                                notes=f'Restocked via {transfer.slip_number} '
                                      f'(no matching warehouse batch found)',
                            )

                previous = transfer.status
                transfer.status = StockTransfer.Status.RELEASED
                transfer.save()
                record_transfer_event(
                    transfer, previous, transfer.status, request.user,
                    'Stock released by the source branch.',
                )
        except _InsufficientStockError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(StockTransferSerializer(transfer).data)

    def _complete_write_off(self, request, transfer_id):
        try:
            with transaction.atomic():
                transfer = StockTransfer.objects.select_for_update().get(pk=transfer_id)
                if not user_can_access_branch(request.user, transfer.source_branch_id):
                    return Response(
                        {'detail': 'That write-off belongs to another branch.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                if transfer.status != StockTransfer.Status.APPROVED:
                    return Response(
                        {'detail': 'Transfer must be approved before it can be released.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                for item in transfer.items.select_related('product'):
                    quantity = item.quantity_approved
                    if quantity <= 0:
                        continue
                    inventory = get_object_or_404(
                        Inventory.objects.select_for_update(),
                        branch=transfer.source_branch, product=item.product,
                    )
                    if quantity > inventory.quantity_on_hand:
                        raise _InsufficientStockError(
                            f'Not enough floor stock for {item.product.product_name} to '
                            f'write off — available: {inventory.quantity_on_hand}, '
                            f'approved: {quantity}.'
                        )
                    before = inventory.quantity_on_hand
                    inventory.quantity_on_hand -= quantity
                    inventory.save()
                    StockMovement.objects.create(
                        inventory=inventory,
                        location_type=StockMovement.LocationType.FLOOR,
                        movement_type=StockMovement.MovementType.WRITE_OFF,
                        quantity_change=-quantity,
                        quantity_before=before,
                        quantity_after=inventory.quantity_on_hand,
                        reference_id=transfer.pk,
                        notes=f'Write-off {transfer.slip_number} — {transfer.get_purpose_display()}',
                        created_by=request.user,
                    )
                    Batch.consume_fefo(
                        item.product, transfer.source_branch,
                        Batch.Location.FLOOR, quantity, include_expired=True,
                    )
                    item.quantity_released = quantity
                    item.release_method = StockTransferItem.ReleaseMethod.MANUAL
                    item.save(update_fields=['quantity_released', 'release_method'])

                previous = transfer.status
                transfer.status = StockTransfer.Status.ACKNOWLEDGED
                transfer.acknowledged_by = request.user
                transfer.received_qty = sum(
                    item.quantity_released for item in transfer.items.all()
                )
                transfer.save()
                record_transfer_event(
                    transfer, previous, transfer.status, request.user,
                    'Floor stock written off by the source branch.',
                )
        except _InsufficientStockError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(StockTransferSerializer(transfer).data)


class StockTransferAcknowledgeAPIView(APIView):
    """
    PATCH /api/stock-transfers/<pk>/acknowledge/
    Body: { "receivedQty": <int>, "flagReason": "..." (optional) }

    Confirms receipt at the destination branch. If receivedQty matches the
    sum of approved quantities, status becomes Acknowledged; otherwise it
    becomes Discrepancy and flagReason is required.
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def patch(self, request, pk):
        if request.user.role not in [
            User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER,
        ]:
            return Response(
                {'detail': 'Only Staff, Branch Managers, or Owners can confirm stock received on the floor.'},
                status=status.HTTP_403_FORBIDDEN
            )

        transfer = get_object_or_404(StockTransfer, pk=pk)
        if not transfer.is_warehouse_floor_transfer:
            return Response(
                {'detail': 'Write-offs do not have a floor-receipt step.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not user_can_access_branch(request.user, transfer.destination_branch_id):
            return Response(
                {'detail': 'That stock transfer belongs to another branch.'},
                status=status.HTTP_403_FORBIDDEN
            )
        if transfer.status != StockTransfer.Status.RELEASED:
            return Response(
                {'detail': 'Stock must be moved to the floor before receipt can be confirmed.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = StockTransferAcknowledgeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        total_released = sum(item.quantity_released for item in transfer.items.all())
        is_match = data['receivedQty'] == total_released

        if not is_match and not data.get('flagReason'):
            return Response(
                {'flagReason': 'Required when received quantity does not match released quantity.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not is_match:
            if not data.get('items'):
                return Response(
                    {'items': 'Per-item received quantities are required for a discrepancy.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if sum(data['items'].values()) != data['receivedQty']:
                return Response(
                    {'receivedQty': 'Must equal the sum of the per-item quantities.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                transfer = reconcile_transfer_receipt(
                    transfer_id=pk,
                    actor=request.user,
                    received_by_item=data['items'],
                    reason=data['flagReason'],
                )
            except StockTransferReceiptError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            return Response(StockTransferSerializer(transfer).data)

        with transaction.atomic():
            transfer = StockTransfer.objects.select_for_update().get(pk=pk)
            if not user_can_access_branch(request.user, transfer.destination_branch_id):
                return Response(
                    {'detail': 'That stock transfer belongs to another branch.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if transfer.status != StockTransfer.Status.RELEASED:
                return Response(
                    {'detail': 'Stock must be moved to the floor before receipt can be confirmed.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            previous = transfer.status
            transfer.received_qty = data['receivedQty']
            transfer.flag_reason = ''
            transfer.acknowledged_by = request.user
            transfer.status = StockTransfer.Status.ACKNOWLEDGED
            transfer.save()
            record_transfer_event(
                transfer, previous, transfer.status, request.user,
                f'Confirmed receipt of {data["receivedQty"]} units.',
            )

        return Response(StockTransferSerializer(transfer).data)
