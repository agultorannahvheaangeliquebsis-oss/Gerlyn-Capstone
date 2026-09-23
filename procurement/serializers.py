# procurement/serializers.py

from rest_framework import serializers
from django.db import transaction
from django.utils import timezone

from .models import Procurement, ProcurementItem, SupplierProduct
from .services import SupplierCostError, snapshot_supplier_costs
from inventory.models import Product, Inventory, Batch, Warehouse, WarehouseStock, StockMovement
from accounts.models import User
from branches.models import Branch
from core.scoping import user_can_access_branch


# =============================================================================
# PROCUREMENT ITEM
# =============================================================================

class ProcurementItemSerializer(serializers.ModelSerializer):
    """
    Nested inside ProcurementSerializer.
    Translates Django field names to React-expected camelCase keys.
    """
    productId    = serializers.SerializerMethodField()
    productName  = serializers.SerializerMethodField()
    productSku   = serializers.SerializerMethodField()
    quantityOrdered   = serializers.IntegerField(source='quantity_ordered')
    quantityReceived  = serializers.IntegerField(source='quantity_received', read_only=True)
    unitCostSnapshot = serializers.DecimalField(
        source='unit_cost_snapshot', max_digits=10, decimal_places=2,
        read_only=True, allow_null=True,
    )
    subtotal     = serializers.SerializerMethodField()

    class Meta:
        model  = ProcurementItem
        fields = [
            'id',
            'productId',
            'productName',
            'productSku',
            'quantityOrdered',
            'quantityReceived',
            'unitCostSnapshot',
            'subtotal',
        ]

    def get_productId(self, obj) -> str | None:
        return f"P{obj.product.id:03d}" if obj.product else None

    def get_productName(self, obj) -> str | None:
        return obj.product.product_name if obj.product else None

    def get_productSku(self, obj) -> str | None:
        return obj.product.sku_code if obj.product else None

    def get_subtotal(self, obj):
        return float(obj.line_total)


# =============================================================================
# PROCUREMENT (READ)
# Full detail serializer — used for GET list and GET detail responses.
# =============================================================================

class ProcurementReadSerializer(serializers.ModelSerializer):
    """
    Translates Procurement model to the React PurchaseOrder interface.

    Mapping (Django -> JSON):
        id (integer)        -> id (string 'PO-2026-001' via po_number)
        branch (FK)         -> branch (string name)
        supplier (FK User)  -> supplierId, supplierName
        owner (FK User)     -> authorizedBy (string name)
        created_by (FK)     -> createdBy (string name)
        order_date          -> orderDate
        delivery_date       -> expectedDelivery
        total_cost          -> total
        status              -> status (direct)
        receipt_flag        -> receiptFlag
        items (related)     -> items (nested array)
    """
    poNumber         = serializers.CharField(source='po_number', read_only=True)
    branch           = serializers.SerializerMethodField()
    supplierId       = serializers.SerializerMethodField()
    supplierName     = serializers.SerializerMethodField()
    authorizedBy     = serializers.SerializerMethodField()
    createdBy        = serializers.SerializerMethodField()
    orderDate        = serializers.DateField(source='order_date', read_only=True)
    expectedDelivery = serializers.DateField(source='delivery_date', read_only=True, allow_null=True)
    actualReceiptDate = serializers.DateField(
        source='actual_receipt_date', read_only=True, allow_null=True
    )
    total            = serializers.DecimalField(source='total_cost', max_digits=12, decimal_places=2, read_only=True)
    receiptFlag      = serializers.CharField(source='receipt_flag', read_only=True)
    items            = ProcurementItemSerializer(many=True, read_only=True)

    class Meta:
        model  = Procurement
        fields = [
            'id',
            'poNumber',
            'branch',
            'supplierId',
            'supplierName',
            'authorizedBy',
            'createdBy',
            'orderDate',
            'expectedDelivery',
            'actualReceiptDate',
            'total',
            'status',
            'receiptFlag',
            'items',
        ]

    def get_branch(self, obj) -> str | None:
        return obj.branch.branch_name if obj.branch else None

    def get_supplierId(self, obj) -> str | None:
        if obj.supplier:
            return f"SUP{obj.supplier.id:03d}"
        return None

    def get_supplierName(self, obj) -> str | None:
        if obj.supplier:
            try:
                return obj.supplier.supplier_profile.supplier_name
            except Exception:
                return obj.supplier.get_full_name() or obj.supplier.username
        return None

    def get_authorizedBy(self, obj) -> str | None:
        if obj.owner:
            try:
                return obj.owner.owner_profile.owner_name
            except Exception:
                return obj.owner.get_full_name() or obj.owner.username
        return None

    def get_createdBy(self, obj) -> str | None:
        if obj.created_by:
            # Check all possible profile types
            for attr, name_field in [
                ('staff_profile', 'staff_name'),
                ('manager_profile', 'manager_name'),
                ('owner_profile', 'owner_name'),
            ]:
                profile = getattr(obj.created_by, attr, None)
                if profile:
                    return getattr(profile, name_field, None)
            return obj.created_by.get_full_name() or obj.created_by.username
        return None


# =============================================================================
# PROCUREMENT (WRITE)
# Separate write serializer — used for POST (create) requests.
# Keeps read serializer clean and validation logic separate.
# =============================================================================

class ProcurementItemWriteSerializer(serializers.Serializer):
    """Flat item input — used inside ProcurementWriteSerializer."""
    productId       = serializers.IntegerField()   # DB pk, not 'P001' string
    quantityOrdered = serializers.IntegerField(min_value=1)


class ProcurementWriteSerializer(serializers.Serializer):
    """
    Handles POST /api/procurement/ — creates a Procurement with nested items.
    Returns a ProcurementReadSerializer response on success.
    """
    branchId         = serializers.IntegerField()
    supplierId       = serializers.IntegerField()
    orderDate        = serializers.DateField()
    expectedDelivery = serializers.DateField(required=False, allow_null=True)
    items            = ProcurementItemWriteSerializer(many=True, min_length=1)

    def validate_branchId(self, value):
        if not Branch.objects.filter(id=value).exists():
            raise serializers.ValidationError("Branch not found.")
        request = self.context.get('request')
        if not request or not user_can_access_branch(request.user, value):
            raise serializers.ValidationError(
                "You do not have access to this branch."
            )
        return value

    def validate_supplierId(self, value):
        if not User.objects.filter(id=value, role=User.Role.SUPPLIER).exists():
            raise serializers.ValidationError("Supplier not found.")
        return value

    def validate_items(self, value):
        product_ids = [item['productId'] for item in value]
        if len(product_ids) != len(set(product_ids)):
            raise serializers.ValidationError("Duplicate products in order.")
        existing = Product.objects.filter(id__in=product_ids).count()
        if existing != len(product_ids):
            raise serializers.ValidationError("One or more products not found.")
        return value

    def validate(self, attrs):
        product_ids = [item['productId'] for item in attrs.get('items', [])]
        mapped_ids = set(SupplierProduct.objects.filter(
            supplier_id=attrs.get('supplierId'),
            product_id__in=product_ids,
        ).values_list('product_id', flat=True))
        if set(product_ids) != mapped_ids:
            raise serializers.ValidationError({
                'items': 'Every product must belong to the selected supplier catalog.'
            })
        return attrs

    def create(self, validated_data):
        request      = self.context['request']
        items_data   = validated_data.pop('items')
        branch       = Branch.objects.get(id=validated_data['branchId'])
        supplier     = User.objects.get(id=validated_data['supplierId'])
        owner_user   = request.user if request.user.role == User.Role.OWNER else None
        created_by   = request.user

        with transaction.atomic():
            proc = Procurement.objects.create(
                branch=branch,
                supplier=supplier,
                owner=owner_user,
                created_by=created_by,
                order_date=validated_data['orderDate'],
                delivery_date=validated_data.get('expectedDelivery'),
                status=Procurement.Status.DRAFT,
                total_cost=0,
            )

            for item_data in items_data:
                product = Product.objects.get(id=item_data['productId'])
                qty     = item_data['quantityOrdered']

                ProcurementItem.objects.create(
                    procurement=proc,
                    product=product,
                    quantity_ordered=qty,
                )

        return proc


# =============================================================================
# PROCUREMENT STATUS UPDATE
# Handles PATCH /api/procurement/<id>/update_status/
# Preserves existing template view logic + adds Batch creation on Received.
# =============================================================================

class ProcurementReceivedItemSerializer(serializers.Serializer):
    """One line of the optional `items` payload on a RECEIVED transition —
    the actual delivered quantity for one ProcurementItem."""
    itemId = serializers.IntegerField()
    quantityReceived = serializers.IntegerField(min_value=0)


class ProcurementStatusSerializer(serializers.Serializer):
    """
    Validates and applies a status transition.
    On RECEIVED: updates quantity_received, credits WarehouseStock (never
    Inventory — a delivery stocks the warehouse; floor stock only moves via
    an approved, released withdrawal slip, same rule delivery_receive
    enforces), and auto-creates a Batch record per item (for FEFO tracking).
    """
    status      = serializers.ChoiceField(choices=Procurement.Status.choices)
    receiptFlag = serializers.ChoiceField(
        choices=Procurement.ReceiptFlag.choices,
        required=False,
        default=Procurement.ReceiptFlag.NONE
    )
    # Optional: override delivery date when marking as received
    deliveryDate = serializers.DateField(required=False, allow_null=True)
    actualReceiptDate = serializers.DateField(required=False)
    # Optional per-item received quantities for a RECEIVED transition. If
    # omitted entirely, every item is assumed received in full — the same
    # assumption this endpoint always made, kept as the default for any
    # client that doesn't send per-item quantities yet.
    items = ProcurementReceivedItemSerializer(many=True, required=False)

    def update(self, instance, validated_data):
        new_status   = validated_data['status']
        receipt_flag = validated_data.get('receiptFlag', Procurement.ReceiptFlag.NONE)
        delivery_date = validated_data.get('deliveryDate') or instance.delivery_date
        receipt_date = validated_data.get('actualReceiptDate') or timezone.localdate()
        submitted_items = {
            entry['itemId']: entry['quantityReceived']
            for entry in validated_data.get('items', [])
        }
        user = self.context.get('request').user if self.context.get('request') else None

        with transaction.atomic():
            if (instance.status == Procurement.Status.DRAFT
                    and new_status == Procurement.Status.APPROVED):
                try:
                    snapshot_supplier_costs(instance)
                except SupplierCostError as exc:
                    raise serializers.ValidationError({'detail': str(exc)})
                instance.owner = user
                instance.authorized_at = timezone.now()

            if delivery_date:
                instance.delivery_date = delivery_date
            if new_status not in (
                Procurement.Status.PARTIALLY_RECEIVED,
                Procurement.Status.RECEIVED,
            ):
                instance.status = new_status
                instance.receipt_flag = receipt_flag
                instance.save()

            if new_status in (
                Procurement.Status.PARTIALLY_RECEIVED,
                Procurement.Status.RECEIVED,
            ):
                warehouse = Warehouse.objects.filter(branch=instance.branch).first()
                if warehouse is None:
                    raise serializers.ValidationError(
                        {'detail': f'No warehouse is configured for {instance.branch.branch_name} — cannot receive stock.'}
                    )

                items = list(instance.items.select_related('product').all())
                any_received = False
                for item in items:
                    # A quantity greater than what was ordered is rejected
                    # rather than silently inflating stock — clamp to the
                    # valid [0, quantity_ordered] range.
                    remaining = max(
                        item.quantity_ordered - item.quantity_received, 0
                    )
                    default_qty = 0 if submitted_items else remaining
                    qty_received = submitted_items.get(item.id, default_qty)
                    if not item.is_unexpected and qty_received > remaining:
                        raise serializers.ValidationError({
                            'items': (
                                f'{item.product.product_name} has only '
                                f'{remaining} remaining to receive.'
                            )
                        })

                    if qty_received <= 0:
                        continue

                    any_received = True
                    item.quantity_received += qty_received
                    item.save(update_fields=['quantity_received'])

                    # A delivery stocks the WAREHOUSE, not the branch floor —
                    # Inventory (floor stock) only increases via an approved,
                    # released withdrawal slip. Locked for the rest of this
                    # transaction so a double-submit of the same delivery
                    # can't credit it twice.
                    ws, _ = WarehouseStock.objects.select_for_update().get_or_create(
                        product=item.product, warehouse=warehouse,
                        defaults={'quantity_on_hand': 0, 'reorder_level': 0},
                    )
                    before = ws.quantity_on_hand
                    ws.quantity_on_hand += qty_received
                    ws.save()

                    StockMovement.objects.create(
                        warehouse_stock=ws,
                        location_type=StockMovement.LocationType.WAREHOUSE,
                        movement_type=StockMovement.MovementType.PROCUREMENT_RECEIVED,
                        quantity_change=qty_received,
                        quantity_before=before,
                        quantity_after=ws.quantity_on_hand,
                        reference_id=instance.pk,
                        notes=f'Delivery for {instance.po_number}',
                        created_by=user,
                    )

                    # Auto-create a Batch record for FEFO tracking. Uses
                    # delivery_date as received_date. expiry_date is left
                    # blank — this API path has no field to collect it from
                    # (the web receiving screen, procurement.views.
                    # delivery_receive, takes a real per-item expiry date).
                    Batch.objects.create(
                        product=item.product,
                        branch=instance.branch,
                        location=Batch.Location.WAREHOUSE,
                        procurement=instance,
                        quantity=qty_received,
                        received_date=receipt_date,
                        expiry_date=None,
                        notes=f"Auto-created from {instance.po_number}.",
                    )

                if not any_received:
                    raise serializers.ValidationError({
                        'items': 'At least one received quantity must be greater than zero.'
                    })

                has_remaining = any(
                    not item.is_unexpected
                    and item.quantity_received < item.quantity_ordered
                    for item in items
                )
                has_unexpected = any(
                    item.is_unexpected and item.quantity_received > 0
                    for item in items
                )
                instance.status = (
                    Procurement.Status.PARTIALLY_RECEIVED
                    if has_remaining else Procurement.Status.RECEIVED
                )
                instance.receipt_flag = (
                    Procurement.ReceiptFlag.PARTIAL
                    if has_remaining else (
                        Procurement.ReceiptFlag.DISCREPANCY
                        if has_unexpected
                        or receipt_flag == Procurement.ReceiptFlag.DISCREPANCY
                        else Procurement.ReceiptFlag.NONE
                    )
                )
                instance.actual_receipt_date = receipt_date
                instance.save()

        return instance
