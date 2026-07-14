from django.db import models
from branches.models import Branch


class ProductCategory(models.Model):
    category_name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.category_name

    class Meta:
        verbose_name_plural = "Product Categories"


class Product(models.Model):
    class UnitOfMeasure(models.TextChoices):
        SACK = 'Sack', 'Sack'
        KILO = 'Kilo', 'Kilo'
        PIECE = 'Piece', 'Piece'

    category = models.ForeignKey(
        ProductCategory, on_delete=models.SET_NULL,
        null=True, related_name='products'
    )
    sku_code = models.CharField(max_length=50, unique=True)
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

    description = models.TextField(blank=True, default='')

    def __str__(self):
        return f"{self.product_name} ({self.sku_code})"


class Warehouse(models.Model):
    branch = models.OneToOneField(
        Branch, on_delete=models.CASCADE,
        related_name='warehouse'
    )
    name = models.CharField(max_length=100)
    location_notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.name} ({self.branch.branch_name})"


class WarehouseStock(models.Model):
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.CASCADE,
        related_name='stock_records'
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='warehouse_stock'
    )
    quantity_on_hand = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(
        default=0,
        help_text="When stock falls to or below this level, a restock alert is triggered"
    )
    restock_flag = models.BooleanField(
        default=False,
        help_text="Auto-set to True when quantity_on_hand <= reorder_level"
    )
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return (
            f"{self.product.product_name} | "
            f"{self.warehouse.name} | "
            f"{self.quantity_on_hand}"
        )

    def save(self, *args, **kwargs):
        self.restock_flag = self.quantity_on_hand <= self.reorder_level
        super().save(*args, **kwargs)

    class Meta:
        verbose_name_plural = "Warehouse Stock"
        unique_together = ('warehouse', 'product')


class Inventory(models.Model):
    """
    Represents store floor stock only.
    Stock only enters here via a completed StockTransfer
    from the warehouse. Never directly from procurement.
    """
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

    def __str__(self):
        return f"{self.product.product_name} - {self.branch.branch_name} (Floor)"

    def save(self, *args, **kwargs):
        self.restock = self.quantity_on_hand <= self.reorder_level
        super().save(*args, **kwargs)

    class Meta:
        verbose_name_plural = "Inventory (Store Floor)"
        unique_together = ('product', 'branch')


class StockTransfer(models.Model):
    """
    Digital Withdrawal Slip.
    Staff requests stock transfer — either from warehouse to floor,
    or from one branch to another (Branch Restock).
    Branch Manager or Owner approves it.
    """
    class Status(models.TextChoices):
        PENDING = 'Pending', 'Pending'
        APPROVED = 'Approved', 'Approved'
        REJECTED = 'Rejected', 'Rejected'
        RELEASED = 'Released', 'Released'
        ACKNOWLEDGED = 'Acknowledged', 'Acknowledged'
        DISCREPANCY = 'Discrepancy', 'Discrepancy'

    class Purpose(models.TextChoices):
        BRANCH_RESTOCK = 'Branch Restock', 'Branch Restock'
        DAMAGED_SPOILED = 'Damaged/Spoiled', 'Damaged/Spoiled'
        SAMPLE_DISPLAY = 'Sample/Display', 'Sample/Display'
        OTHER = 'Other', 'Other'

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

    def save(self, *args, **kwargs):
        if not self.slip_number:
            super().save(*args, **kwargs)
            year = self.created_at.year
            self.slip_number = f"WS-{year}-{str(self.pk).zfill(3)}"
        super().save(*args, **kwargs)

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
    """
    Permanent audit log of every stock change.
    Never edited or deleted — system-generated only.
    Covers both warehouse and store floor movements.
    """
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
        help_text="Whether this movement happened in the warehouse or on the store floor",
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