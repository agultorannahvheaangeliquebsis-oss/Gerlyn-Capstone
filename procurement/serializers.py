# procurement/serializers.py

from decimal import Decimal

from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from .models import DeliveryReceipt, Procurement, ProcurementItem, SupplierProduct
from .services import (
    ExternalOrderError, SupplierCostError, record_external_order,
    snapshot_supplier_costs, validate_expected_delivery_date,
)
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
    fundingSource    = serializers.CharField(source='funding_source', read_only=True)
    fundingNote      = serializers.CharField(source='funding_note', read_only=True)
    items            = ProcurementItemSerializer(many=True, read_only=True)
    receipts         = serializers.SerializerMethodField()
    communicatedAt   = serializers.DateTimeField(source='communicated_at', read_only=True, allow_null=True)
    communicationMethod = serializers.CharField(source='communication_method', read_only=True)
    communicationNote = serializers.CharField(source='communication_note', read_only=True)
    supplierAcknowledgedAt = serializers.DateTimeField(source='supplier_acknowledged_at', read_only=True, allow_null=True)

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
            'fundingSource',
            'fundingNote',
            'items',
            'receipts',
            'communicatedAt',
            'communicationMethod',
            'communicationNote',
            'supplierAcknowledgedAt',
        ]

    def get_receipts(self, obj) -> list:
        from core.people import person_display_name

        return [
            {
                'receivedBy': person_display_name(r.received_by) or None,
                'deliveredBy': r.delivered_by or None,
                'receiptDate': r.receipt_date.isoformat() if r.receipt_date else None,
                'recordedAt': r.received_at.isoformat(),
                'outcome': r.outcome,
                'rejectionReason': r.rejection_reason or None,
                'items': [
                    {
                        'itemId': line.procurement_item_id,
                        'productId': line.product_id,
                        'quantityReceived': line.quantity_received,
                    }
                    for line in r.items.all()
                ],
            }
            for r in obj.receipts.all()
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
    submissionKey    = serializers.CharField(required=False, allow_blank=True, max_length=64)
    fundingSource    = serializers.ChoiceField(choices=Procurement.FundingSource.choices)
    fundingNote      = serializers.CharField(required=False, allow_blank=True, max_length=200)
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
        if not User.objects.filter(
            id=value, role=User.Role.SUPPLIER, is_active=True
        ).exists():
            raise serializers.ValidationError("Active supplier not found.")
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
        if attrs.get('expectedDelivery') is not None:
            raise serializers.ValidationError({
                'expectedDelivery': (
                    'Delivery date may be set only through supplier negotiation '
                    'or external-order communication after approval.'
                )
            })
        # Same rule as the web form: Other has to say what it is.
        if (attrs['fundingSource'] == Procurement.FundingSource.OTHER
                and not (attrs.get('fundingNote') or '').strip()):
            raise serializers.ValidationError({
                'fundingNote': 'Describe the funding source when you choose Other.'
            })
        return attrs

    def create(self, validated_data):
        request      = self.context['request']
        items_data   = validated_data.pop('items')
        branch       = Branch.objects.get(id=validated_data['branchId'])
        supplier     = User.objects.get(id=validated_data['supplierId'])
        owner_user   = request.user if request.user.role == User.Role.OWNER else None
        created_by   = request.user
        submission_key = (validated_data.get('submissionKey') or '').strip() or None

        with transaction.atomic():
            proc = Procurement.objects.create(
                branch=branch,
                supplier=supplier,
                owner=owner_user,
                created_by=created_by,
                order_date=validated_data['orderDate'],
                delivery_date=None,
                status=(Procurement.Status.APPROVED
                        if request.user.role == User.Role.OWNER
                        else Procurement.Status.DRAFT),
                total_cost=0,
                authorized_at=(timezone.now()
                               if request.user.role == User.Role.OWNER else None),
                submission_key=submission_key,
                funding_source=validated_data['fundingSource'],
                funding_note=(validated_data.get('fundingNote') or '').strip(),
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


class DeliveryEventItemSerializer(ProcurementReceivedItemSerializer):
    expiryDate = serializers.DateField(required=False, allow_null=True)


class DeliveryEventSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(
        choices=DeliveryReceipt.Outcome.choices,
        default=DeliveryReceipt.Outcome.ACCEPTED,
    )
    submissionKey = serializers.CharField(max_length=64)
    actualReceiptDate = serializers.DateField(required=False)
    deliveredBy = serializers.CharField(required=False, allow_blank=True, max_length=100)
    rejectionReason = serializers.CharField(required=False, allow_blank=True, max_length=300)
    items = DeliveryEventItemSerializer(many=True, required=False)

    def validate(self, attrs):
        rejected = attrs['outcome'] == DeliveryReceipt.Outcome.REJECTED
        if rejected and not (attrs.get('rejectionReason') or '').strip():
            raise serializers.ValidationError({
                'rejectionReason': 'Enter a reason for the failed or rejected delivery.'
            })
        if not rejected and not attrs.get('items'):
            raise serializers.ValidationError({'items': 'Provide at least one delivery line.'})
        receipt_date = attrs.get('actualReceiptDate')
        if receipt_date and receipt_date > timezone.localdate():
            raise serializers.ValidationError({
                'actualReceiptDate': 'Actual receipt date cannot be in the future.'
            })
        ids = [row['itemId'] for row in attrs.get('items', [])]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError({'items': 'Each PO line may appear only once.'})
        return attrs


class ProcurementExternalCostSerializer(serializers.Serializer):
    itemId = serializers.IntegerField()
    unitCost = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))


class ProcurementExternalOrderSerializer(serializers.Serializer):
    """Terms confirmed by phone/message instead of through supplier login."""
    deliveryDate = serializers.DateField(validators=[validate_expected_delivery_date])
    communicationMethod = serializers.ChoiceField(
        choices=Procurement.CommunicationMethod.choices,
    )
    communicationNote = serializers.CharField(
        required=False, allow_blank=True, max_length=300,
    )
    unitCosts = ProcurementExternalCostSerializer(many=True, min_length=1)

    def validate_unitCosts(self, value):
        ids = [entry['itemId'] for entry in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError('Each PO item may appear only once.')
        return value

    def update(self, instance, validated_data):
        costs = {
            entry['itemId']: entry['unitCost']
            for entry in validated_data['unitCosts']
        }
        expected = set(
            instance.items.filter(is_unexpected=False).values_list('pk', flat=True)
        )
        if set(costs) != expected:
            raise serializers.ValidationError({
                'unitCosts': 'Provide one agreed unit cost for every PO item.'
            })
        try:
            return record_external_order(
                instance,
                actor=self.context['request'].user,
                delivery_date=validated_data['deliveryDate'],
                unit_costs=costs,
                method=validated_data['communicationMethod'],
                note=validated_data.get('communicationNote', ''),
            )
        except ExternalOrderError as exc:
            raise serializers.ValidationError({'detail': str(exc)}) from exc


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
    actualReceiptDate = serializers.DateField(required=False)
    # Optional courier/driver name, recorded on the DeliveryReceipt.
    deliveredBy = serializers.CharField(
        required=False, allow_blank=True, max_length=100,
    )
    # Optional per-item received quantities for a RECEIVED transition. If
    # omitted entirely, every item is assumed received in full — the same
    # assumption this endpoint always made, kept as the default for any
    # client that doesn't send per-item quantities yet.
    items = ProcurementReceivedItemSerializer(many=True, required=False)

    def update(self, instance, validated_data):
        new_status   = validated_data['status']
        receipt_flag = validated_data.get('receiptFlag', Procurement.ReceiptFlag.NONE)
        receipt_date = validated_data.get('actualReceiptDate') or timezone.localdate()
        submitted_items = {
            entry['itemId']: entry['quantityReceived']
            for entry in validated_data.get('items', [])
        }
        user = self.context.get('request').user if self.context.get('request') else None

        with transaction.atomic():
            if (instance.status == Procurement.Status.DRAFT
                    and new_status == Procurement.Status.APPROVED):
                instance.owner = user
                instance.authorized_at = timezone.now()

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
                # Same record the template view writes: who took this
                # shipment in. The API's actor is the authenticated user.
                DeliveryReceipt.objects.create(
                    procurement=instance,
                    received_by=user,
                    receipt_date=receipt_date,
                    delivered_by=(validated_data.get('deliveredBy') or '').strip(),
                )

        return instance
