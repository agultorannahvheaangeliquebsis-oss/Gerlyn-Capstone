
from django.db import models
from branches.models import Branch


class ProductCategory(models.Model):
    # --- unchanged ---
    category_name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.category_name

    class Meta:
        verbose_name_plural = "Product Categories"


class Product(models.Model):
    # --- unchanged ---
    class UnitOfMeasure(models.TextChoices):
        SACK = 'Sack', 'Sack'
        KILO = 'Kilo', 'Kilo'
        PIECE = 'Piece', 'Piece'
        # Retail units used across the catalog/UI.
        CAN = 'Can', 'Can'
        PACK = 'Pack', 'Pack'
        BOTTLE = 'Bottle', 'Bottle'
        BOX = 'Box', 'Box'

    category = models.ForeignKey(
        ProductCategory, on_delete=models.SET_NULL,
        null=True, related_name='products'
    )
    sku_code = models.CharField(max_length=50, unique=True)
    barcode = models.CharField(
        max_length=64, null=True, blank=True, unique=True,
        help_text="Manufacturer barcode (UPC/EAN/etc.) as scanned off the "
                   "physical product — distinct from sku_code, which is our "
                   "own auto-assigned internal code. Optional."
    )
    product_name = models.CharField(max_length=150)
    unit_of_measure = models.CharField(
        max_length=20, choices=UnitOfMeasure.choices,
        default=UnitOfMeasure.PIECE
    )
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Retail price"
    )
    wholesale_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        null=True, blank=True,
        help_text="Price for bulk/wholesale orders"
    )
    description = models.TextField(blank=True)

    def __str__(self):
        return f"{self.product_name} ({self.sku_code})"

    @classmethod
    def next_sku_code(cls):
        """Next SKU-### in sequence, based on the highest existing numeric suffix."""
        prefix = 'SKU-'
        max_num = 0
        for code in cls.objects.filter(sku_code__startswith=prefix).values_list('sku_code', flat=True):
            suffix = code[len(prefix):]
            if suffix.isdigit():
                max_num = max(max_num, int(suffix))
        return f'{prefix}{max_num + 1:03d}'


class ProductImage(models.Model):
    """A single gallery image for a Product. A product can have many."""
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='images'
    )
    image = models.ImageField(upload_to='products/%Y/%m/')
    alt_text = models.CharField(
        max_length=150, blank=True,
        help_text="Short description for accessibility / when the image fails to load"
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="The main image shown in listings and as the detail hero"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Primary first, then oldest-uploaded — gives a stable gallery order.
        ordering = ['-is_primary', 'uploaded_at']

    def __str__(self):
        return f"Image for {self.product.product_name}"


class Warehouse(models.Model):
    # --- unchanged ---
    branch = models.OneToOneField(
        Branch, on_delete=models.CASCADE,
        related_name='warehouse'
    )
    name = models.CharField(max_length=100)
    location_notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.name} ({self.branch.branch_name})"


class WarehouseStock(models.Model):
    # --- unchanged ---
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.CASCADE,
        related_name='stock_records'
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='warehouse_stock'
    )
    quantity_on_hand = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(default=0)
    restock_flag = models.BooleanField(default=False)
    last_updated = models.DateTimeField(auto_now=True)
    expiry_date = models.DateField(
        null=True, blank=True,
        help_text="Expiry date for the stock currently sitting in the warehouse. "
                  "Independent of Inventory.expiry_date — the same product can have "
                  "a different lot/expiry on the floor vs in the warehouse."
    )

    def save(self, *args, **kwargs):
        self.restock_flag = self.quantity_on_hand <= self.reorder_level
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.product.product_name} | "
            f"{self.warehouse.name} | "
            f"{self.quantity_on_hand}"
        )

    class Meta:
        verbose_name_plural = "Warehouse Stock"
        unique_together = ('warehouse', 'product')


class Inventory(models.Model):
    """
    Store floor stock only.
    Stock enters here via a completed StockTransfer from the warehouse.
    """
    # --- existing fields (untouched) ----------------------------------------
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='inventory_records'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE,
        related_name='inventory'
    )
    quantity_on_hand = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(
        default=0,
        help_text="When floor stock hits this level, staff should request a warehouse transfer"
    )
    restock = models.BooleanField(default=False)
    last_stock_update = models.DateTimeField(auto_now=True)

    # --- new field ----------------------------------------------------------
    expiry_date = models.DateField(
        null=True, blank=True,
        help_text="Expiry date for the current stock on the floor. "
                  "For per-lot tracking use the Batch model instead."
    )

    def save(self, *args, **kwargs):
        self.restock = self.quantity_on_hand <= self.reorder_level
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.product_name} - {self.branch.branch_name} (Floor)"

    class Meta:
        verbose_name_plural = "Inventory (Store Floor)"
        unique_together = ('product', 'branch')


# =============================================================================
# NEW MODEL: Batch (per-lot expiry tracking)
# Answers the panel question: "How do you prevent expired products from
# being sold?" via FEFO (First Expired, First Out) logic.
# =============================================================================

class Batch(models.Model):
    """
    Represents one delivery lot of a product at a specific branch.
    Multiple batches can exist per product per branch, each with
    their own expiry date.

    FEFO rule: when stock is deducted (via a sale), the batch with
    the nearest expiry_date is consumed first.
    """

    class ExpiryStatus(models.TextChoices):
        FRESH = 'Fresh', 'Fresh'
        EXPIRING_SOON = 'Expiring Soon', 'Expiring Soon'   # within 30 days
        EXPIRED = 'Expired', 'Expired'

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='batches'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE,
        related_name='batches'
    )
    # Link to the procurement that brought this batch in (optional but useful
    # for traceability — which supplier delivered this lot?)
    procurement = models.ForeignKey(
        'procurement.Procurement', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='batches',
        help_text="The purchase order that delivered this batch"
    )
    batch_number = models.CharField(
        max_length=20, unique=True, blank=True,
        help_text="Auto-generated e.g. BAT-2026-001"
    )
    quantity = models.PositiveIntegerField(
        help_text="Remaining quantity in this batch"
    )
    received_date = models.DateField(
        help_text="Date this batch was received into the warehouse/floor"
    )
    expiry_date = models.DateField(
        help_text="Expiry date printed on the product lot"
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.batch_number:
            from django.utils import timezone
            year = timezone.now().year
            self.batch_number = f"BAT-{year}-{str(self.pk).zfill(3)}"
            super().save(update_fields=['batch_number'])

    @property
    def expiry_status(self) -> str:
        """
        Computed property — not stored in DB.
        Called by the serializer's SerializerMethodField.
        Thresholds: expired = past today, expiring soon = within 30 days.
        """
        from django.utils import timezone
        today = timezone.now().date()
        delta = (self.expiry_date - today).days

        if delta < 0:
            return self.ExpiryStatus.EXPIRED
        elif delta <= 30:
            return self.ExpiryStatus.EXPIRING_SOON
        return self.ExpiryStatus.FRESH

    @property
    def days_until_expiry(self) -> int:
        from django.utils import timezone
        today = timezone.now().date()
        return (self.expiry_date - today).days

    def __str__(self):
        return (
            f"{self.batch_number} | "
            f"{self.product.product_name} | "
            f"{self.branch.branch_name} | "
            f"Exp: {self.expiry_date} | "
            f"Qty: {self.quantity}"
        )

    class Meta:
        ordering = ['expiry_date']   # FEFO: nearest expiry first
        verbose_name_plural = "Batches"


class StockTransfer(models.Model):
    """
    A stock withdrawal: goods issued out of the warehouse under an
    approval chain (request -> approve -> release -> acknowledge),
    either to a branch or as a write-off.

    Named "Stock Withdrawals" in the UI. Not to be confused with
    stock_adjustment_add, which is a direct count correction with no
    approval chain and keeps the name "Stock Adjustment".

    slip_number/WS- are left alone on purpose: they identify records
    that already exist.
    """
    class Status(models.TextChoices):
        # --- unchanged ---
        PENDING = 'Pending', 'Pending'
        APPROVED = 'Approved', 'Approved'
        REJECTED = 'Rejected', 'Rejected'
        RELEASED = 'Released', 'Released'
        ACKNOWLEDGED = 'Acknowledged', 'Acknowledged'
        DISCREPANCY = 'Discrepancy', 'Discrepancy'

    class Purpose(models.TextChoices):
        # --- unchanged ---
        BRANCH_RESTOCK = 'Branch Restock', 'Branch Restock'
        DAMAGED_SPOILED = 'Damaged/Spoiled', 'Damaged/Spoiled'
        SAMPLE_DISPLAY = 'Sample/Display', 'Sample/Display'
        OTHER = 'Other', 'Other'

    # --- existing fields (untouched) ----------------------------------------
    slip_number = models.CharField(
        max_length=20, unique=True, blank=True,
        help_text="Auto-generated slip ID e.g. WS-2026-001"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.CASCADE,
        related_name='transfers'
    )
    source_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, related_name='outgoing_transfers',
        help_text="Branch sending the stock"
    )
    destination_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='incoming_transfers',
        help_text="Branch receiving the stock. Null if write-off or display."
    )
    purpose = models.CharField(
        max_length=20, choices=Purpose.choices,
        default=Purpose.BRANCH_RESTOCK
    )
    requested_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL,
        null=True, related_name='transfer_requests',
        limit_choices_to={'role__in': ['STAFF', 'BRANCH_MANAGER']}
    )
    approved_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transfer_approvals',
        limit_choices_to={'role__in': ['BRANCH_MANAGER', 'OWNER']}
    )
    status = models.CharField(
        max_length=15, choices=Status.choices,
        default=Status.PENDING
    )
    transfer_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # --- new fields ---------------------------------------------------------
    acknowledged_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='acknowledged_transfers',
        limit_choices_to={'role__in': ['STAFF', 'BRANCH_MANAGER']},
        help_text="Staff who confirmed receipt of the transferred stock"
    )
    flag_reason = models.TextField(
        blank=True,
        help_text="Reason for Discrepancy status — required when status=Discrepancy"
    )
    received_qty = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Actual quantity received. May differ from approved qty if discrepancy."
    )

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.slip_number:
            year = self.created_at.year
            self.slip_number = f"WS-{year}-{str(self.pk).zfill(3)}"
            super().save(update_fields=['slip_number'])

    def __str__(self):
        return (
            f"{self.slip_number} | "
            f"{self.source_branch} -> "
            f"{self.destination_branch or 'N/A'} | "
            f"{self.status}"
        )

    class Meta:
        ordering = ['-created_at']


class StockTransferItem(models.Model):
    # --- unchanged ---
    transfer = models.ForeignKey(
        StockTransfer, on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='transfer_items'
    )
    quantity_requested = models.PositiveIntegerField()
    quantity_approved = models.PositiveIntegerField(
        default=0,
        help_text="Can differ from requested — approver may release partial quantity"
    )

    def __str__(self):
        return (
            f"{self.product.product_name} | "
            f"Requested: {self.quantity_requested} | "
            f"Approved: {self.quantity_approved}"
        )


class StockMovement(models.Model):
    # --- unchanged ---
    class MovementType(models.TextChoices):
        SALE = 'Sale', 'Sale (Floor Out)'
        PROCUREMENT_RECEIVED = 'Procurement Received', 'Procurement Received (Warehouse In)'
        WAREHOUSE_TO_FLOOR = 'Warehouse to Floor', 'Warehouse to Floor (Transfer Out/In)'
        FLOOR_TO_WAREHOUSE = 'Floor to Warehouse', 'Floor to Warehouse (Return)'
        MANUAL_IN = 'Manual In', 'Manual Stock In'
        MANUAL_OUT = 'Manual Out', 'Manual Stock Out'
        ADJUSTMENT = 'Adjustment', 'Stock Adjustment'

    class LocationType(models.TextChoices):
        WAREHOUSE = 'Warehouse', 'Warehouse'
        FLOOR = 'Floor', 'Store Floor'

    inventory = models.ForeignKey(
        Inventory, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='movements'
    )
    warehouse_stock = models.ForeignKey(
        WarehouseStock, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='movements'
    )
    location_type = models.CharField(
        max_length=10, choices=LocationType.choices,
        default=LocationType.FLOOR
    )
    movement_type = models.CharField(
        max_length=30, choices=MovementType.choices
    )
    quantity_change = models.IntegerField(
        help_text="Positive = stock in, Negative = stock out"
    )
    quantity_before = models.PositiveIntegerField()
    quantity_after = models.PositiveIntegerField()
    reference_id = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="ID of the Sale, Procurement, or StockTransfer that caused this"
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='stock_movements'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        direction = "+" if self.quantity_change > 0 else ""
        product_name = (
            self.inventory.product.product_name if self.inventory
            else self.warehouse_stock.product.product_name
            if self.warehouse_stock else "Unknown"
        )
        return (
            f"{product_name} | "
            f"{self.movement_type} | "
            f"{direction}{self.quantity_change}"
        )

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Stock Movement"
        verbose_name_plural = "Stock Movements"