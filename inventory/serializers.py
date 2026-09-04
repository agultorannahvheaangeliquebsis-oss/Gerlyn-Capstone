from rest_framework import serializers
from .models import (
    Product, ProductCategory, Inventory, Warehouse, WarehouseStock,
    Batch, StockTransfer, StockTransferItem, StockMovement,
)


class ProductCategorySerializer(serializers.ModelSerializer):
    categoryName = serializers.CharField(source='category_name')

    class Meta:
        model = ProductCategory
        fields = ['id', 'categoryName', 'description']


class ProductSerializer(serializers.ModelSerializer):
    # ── Field name translations (Django → React camelCase) ──────────────────
    # React expects: sku, name, unit, retailPrice, wholesalePrice, category (string)
    # Django stores: sku_code, product_name, unit_of_measure, unit_price, wholesale_price, category (FK)

    sku = serializers.CharField(source='sku_code')
    name = serializers.CharField(source='product_name')
    unit = serializers.CharField(source='unit_of_measure')
    retailPrice = serializers.DecimalField(
        source='unit_price', max_digits=10, decimal_places=2
    )
    wholesalePrice = serializers.DecimalField(
        source='wholesale_price', max_digits=10, decimal_places=2,
        allow_null=True, required=False
    )

    # ── Nested / computed fields ──────────────────────────────────────────────
    # React expects category as a plain string (e.g. "Canned Goods")
    # Django stores it as a FK — we resolve the name here
    category = serializers.SerializerMethodField()

    # React expects availability as an array of branch name strings
    # e.g. ["Tarcan", "Makinabang"]
    # Django derives this from Inventory records — computed here
    availability = serializers.SerializerMethodField()

    # Only populated when the request scopes to a branch (?branchId=) —
    # the storefront uses this to show/cap stock for the selected branch.
    stockAtBranch = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id',
            'sku',
            'name',
            'category',
            'unit',
            'retailPrice',
            'wholesalePrice',
            'availability',
            'stockAtBranch',
        ]

    def get_category(self, obj):
        """Return the category name string, not an integer FK."""
        return obj.category.category_name if obj.category else None

    def get_availability(self, obj):
        """
        Return the list of branch names where this product
        has at least 1 unit of floor stock (Inventory records).
        React expects: ["Tarcan", "Makinabang"]
        """
        return list(
            obj.inventory_records
            .filter(quantity_on_hand__gt=0)
            .values_list('branch__branch_name', flat=True)
        )

    def get_stockAtBranch(self, obj):
        branch_id = self.context.get('branch_id')
        if not branch_id:
            return None
        record = obj.inventory_records.filter(branch_id=branch_id).first()
        return record.quantity_on_hand if record else 0


class ProductWriteSerializer(serializers.ModelSerializer):
    """
    Separate serializer for POST/PUT/PATCH requests.
    Accepts camelCase from React and writes to Django snake_case fields.
    We use a write serializer because the read serializer
    has computed fields that are not writable.
    """
    sku = serializers.CharField(source='sku_code')
    name = serializers.CharField(source='product_name')
    unit = serializers.CharField(source='unit_of_measure')
    retailPrice = serializers.DecimalField(
        source='unit_price', max_digits=10, decimal_places=2
    )
    wholesalePrice = serializers.DecimalField(
        source='wholesale_price', max_digits=10, decimal_places=2,
        allow_null=True, required=False
    )
    categoryId = serializers.PrimaryKeyRelatedField(
        source='category', queryset=ProductCategory.objects.all()
    )

    class Meta:
        model = Product
        fields = [
            'sku',
            'name',
            'categoryId',
            'unit',
            'retailPrice',
            'wholesalePrice',
            'description',
        ]


# =============================================================================
# WAREHOUSE
# =============================================================================

class WarehouseSerializer(serializers.ModelSerializer):
    branchId = serializers.PrimaryKeyRelatedField(source='branch', read_only=True)
    branchName = serializers.CharField(source='branch.branch_name', read_only=True)

    class Meta:
        model = Warehouse
        fields = ['id', 'branchId', 'branchName', 'name', 'location_notes']


class WarehouseStockSerializer(serializers.ModelSerializer):
    warehouseId = serializers.PrimaryKeyRelatedField(source='warehouse', read_only=True)
    warehouseName = serializers.CharField(source='warehouse.name', read_only=True)
    branchName = serializers.CharField(source='warehouse.branch.branch_name', read_only=True)
    productId = serializers.PrimaryKeyRelatedField(source='product', read_only=True)
    productName = serializers.CharField(source='product.product_name', read_only=True)
    productSku = serializers.CharField(source='product.sku_code', read_only=True)
    quantityOnHand = serializers.IntegerField(source='quantity_on_hand')
    reorderLevel = serializers.IntegerField(source='reorder_level')
    restockFlag = serializers.BooleanField(source='restock_flag', read_only=True)
    lastUpdated = serializers.DateTimeField(source='last_updated', read_only=True)

    class Meta:
        model = WarehouseStock
        fields = [
            'id', 'warehouseId', 'warehouseName', 'branchName',
            'productId', 'productName', 'productSku',
            'quantityOnHand', 'reorderLevel', 'restockFlag', 'lastUpdated',
        ]


# =============================================================================
# STORE FLOOR INVENTORY (read-only — no template view exists to create/edit
# a floor Inventory row directly; rows are created via procurement receipt
# or a released StockTransfer)
# =============================================================================

class InventorySerializer(serializers.ModelSerializer):
    productId = serializers.PrimaryKeyRelatedField(source='product', read_only=True)
    productName = serializers.CharField(source='product.product_name', read_only=True)
    productSku = serializers.CharField(source='product.sku_code', read_only=True)
    branchId = serializers.PrimaryKeyRelatedField(source='branch', read_only=True)
    branchName = serializers.CharField(source='branch.branch_name', read_only=True)
    quantityOnHand = serializers.IntegerField(source='quantity_on_hand', read_only=True)
    reorderLevel = serializers.IntegerField(source='reorder_level', read_only=True)
    restock = serializers.BooleanField(read_only=True)
    expiryDate = serializers.DateField(source='expiry_date', read_only=True)
    lastStockUpdate = serializers.DateTimeField(source='last_stock_update', read_only=True)

    class Meta:
        model = Inventory
        fields = [
            'id', 'productId', 'productName', 'productSku',
            'branchId', 'branchName',
            'quantityOnHand', 'reorderLevel', 'restock',
            'expiryDate', 'lastStockUpdate',
        ]


# =============================================================================
# BATCH (FEFO expiry tracking) — read-only
# =============================================================================

class BatchSerializer(serializers.ModelSerializer):
    productId = serializers.PrimaryKeyRelatedField(source='product', read_only=True)
    productName = serializers.CharField(source='product.product_name', read_only=True)
    branchId = serializers.PrimaryKeyRelatedField(source='branch', read_only=True)
    branchName = serializers.CharField(source='branch.branch_name', read_only=True)
    batchNumber = serializers.CharField(source='batch_number', read_only=True)
    location = serializers.CharField(read_only=True)
    receivedDate = serializers.DateField(source='received_date', read_only=True)
    expiryDate = serializers.DateField(source='expiry_date', read_only=True, allow_null=True)
    expiryStatus = serializers.CharField(source='expiry_status', read_only=True)
    daysUntilExpiry = serializers.IntegerField(source='days_until_expiry', read_only=True, allow_null=True)

    class Meta:
        model = Batch
        fields = [
            'id', 'productId', 'productName', 'branchId', 'branchName',
            'location', 'batchNumber', 'quantity', 'receivedDate', 'expiryDate',
            'expiryStatus', 'daysUntilExpiry', 'notes',
        ]


# =============================================================================
# STOCK TRANSFER (Withdrawal Slip)
# =============================================================================

class StockTransferItemSerializer(serializers.ModelSerializer):
    productId = serializers.PrimaryKeyRelatedField(source='product', read_only=True)
    productName = serializers.CharField(source='product.product_name', read_only=True)
    quantityRequested = serializers.IntegerField(source='quantity_requested', read_only=True)
    quantityApproved = serializers.IntegerField(source='quantity_approved', read_only=True)

    class Meta:
        model = StockTransferItem
        fields = ['id', 'productId', 'productName', 'quantityRequested', 'quantityApproved']


class StockTransferSerializer(serializers.ModelSerializer):
    """Read serializer — mirrors the fields shown in transfer_list/transfer_detail."""
    slipNumber = serializers.CharField(source='slip_number', read_only=True)
    sourceBranch = serializers.CharField(source='source_branch.branch_name', read_only=True, allow_null=True)
    destinationBranch = serializers.CharField(
        source='destination_branch.branch_name', read_only=True, allow_null=True
    )
    requestedBy = serializers.SerializerMethodField()
    approvedBy = serializers.SerializerMethodField()
    acknowledgedBy = serializers.SerializerMethodField()
    transferDate = serializers.DateField(source='transfer_date', read_only=True)
    flagReason = serializers.CharField(source='flag_reason', read_only=True)
    receivedQty = serializers.IntegerField(source='received_qty', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    items = StockTransferItemSerializer(many=True, read_only=True)

    class Meta:
        model = StockTransfer
        fields = [
            'id', 'slipNumber', 'sourceBranch', 'destinationBranch',
            'purpose', 'requestedBy', 'approvedBy', 'acknowledgedBy',
            'status', 'transferDate', 'notes', 'flagReason', 'receivedQty',
            'createdAt', 'items',
        ]

    def _name(self, user):
        if not user:
            return None
        for attr, field in [
            ('staff_profile', 'staff_name'),
            ('manager_profile', 'manager_name'),
            ('owner_profile', 'owner_name'),
        ]:
            profile = getattr(user, attr, None)
            if profile:
                return getattr(profile, field, None)
        return user.get_full_name() or user.username

    def get_requestedBy(self, obj):
        return self._name(obj.requested_by)

    def get_approvedBy(self, obj):
        return self._name(obj.approved_by)

    def get_acknowledgedBy(self, obj):
        return self._name(obj.acknowledged_by)


class StockTransferItemWriteSerializer(serializers.Serializer):
    productId = serializers.IntegerField()
    quantityRequested = serializers.IntegerField(min_value=1)


class StockTransferWriteSerializer(serializers.Serializer):
    """
    Handles POST /api/stock-transfers/ — mirrors transfer_create's logic:
    the warehouse and source branch are derived from the requesting user's
    own branch (Owner may pass sourceBranchId explicitly).
    """
    sourceBranchId = serializers.IntegerField(required=False)
    destinationBranchId = serializers.IntegerField(required=False, allow_null=True)
    purpose = serializers.ChoiceField(choices=StockTransfer.Purpose.choices)
    transferDate = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)
    items = StockTransferItemWriteSerializer(many=True, min_length=1)

    def validate_items(self, value):
        product_ids = [item['productId'] for item in value]
        if len(product_ids) != len(set(product_ids)):
            raise serializers.ValidationError("Duplicate products in this request.")
        return value

    def create(self, validated_data):
        from django.utils.timezone import now

        request = self.context['request']
        user = request.user
        items_data = validated_data.pop('items')

        source_branch_id = validated_data.get('sourceBranchId') or self.context.get('user_branch_id')
        if not source_branch_id:
            raise serializers.ValidationError({'sourceBranchId': 'No branch assigned to your account.'})

        warehouse = Warehouse.objects.get(branch_id=source_branch_id)

        transfer = StockTransfer.objects.create(
            warehouse=warehouse,
            source_branch_id=source_branch_id,
            destination_branch_id=validated_data.get('destinationBranchId'),
            purpose=validated_data['purpose'],
            requested_by=user,
            status=StockTransfer.Status.PENDING,
            transfer_date=validated_data.get('transferDate') or now().date(),
            notes=validated_data.get('notes', ''),
        )

        for item in items_data:
            StockTransferItem.objects.create(
                transfer=transfer,
                product_id=item['productId'],
                quantity_requested=item['quantityRequested'],
                quantity_approved=0,
            )

        return transfer


class StockTransferApproveSerializer(serializers.Serializer):
    """
    Handles the approve/reject action — mirrors transfer_approve.
    `items` maps each StockTransferItem id to its approved quantity;
    required only when action == 'approve'.
    """
    action = serializers.ChoiceField(choices=['approve', 'reject'])
    items = serializers.DictField(
        child=serializers.IntegerField(min_value=0), required=False
    )

    def validate(self, data):
        if data['action'] == 'approve' and not data.get('items'):
            raise serializers.ValidationError("items is required when approving.")
        return data


class StockTransferAcknowledgeSerializer(serializers.Serializer):
    """
    Handles PATCH /api/stock-transfers/<pk>/acknowledge/ — confirms receipt
    of a Released transfer at the destination branch. If the received
    quantity doesn't match the total approved quantity, the transfer is
    flagged as Discrepancy instead of Acknowledged (fields already exist
    on the model for exactly this: received_qty, flag_reason, acknowledged_by).
    """
    receivedQty = serializers.IntegerField(min_value=0)
    flagReason = serializers.CharField(required=False, allow_blank=True)


# =============================================================================
# STOCK MOVEMENT (read-only audit log)
# =============================================================================

class StockMovementSerializer(serializers.ModelSerializer):
    productName = serializers.SerializerMethodField()
    locationType = serializers.CharField(source='location_type', read_only=True)
    movementType = serializers.CharField(source='movement_type', read_only=True)
    quantityChange = serializers.IntegerField(source='quantity_change', read_only=True)
    quantityBefore = serializers.IntegerField(source='quantity_before', read_only=True)
    quantityAfter = serializers.IntegerField(source='quantity_after', read_only=True)
    referenceId = serializers.IntegerField(source='reference_id', read_only=True)
    createdBy = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            'id', 'productName', 'locationType', 'movementType',
            'quantityChange', 'quantityBefore', 'quantityAfter',
            'referenceId', 'notes', 'createdBy', 'createdAt',
        ]

    def get_productName(self, obj):
        if obj.inventory:
            return obj.inventory.product.product_name
        if obj.warehouse_stock:
            return obj.warehouse_stock.product.product_name
        return None

    def get_createdBy(self, obj):
        return obj.created_by.get_full_name() or obj.created_by.username if obj.created_by else None