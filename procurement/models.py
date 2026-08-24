
from django.db import models
from branches.models import Branch
from inventory.models import Product
from accounts.models import User


class Procurement(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'Draft', 'Draft'               # <-- new
        ORDERED = 'Ordered', 'Ordered'
        RECEIVED = 'Received', 'Received'
        CANCELLED = 'Cancelled', 'Cancelled'

    class ReceiptFlag(models.TextChoices):      # <-- new
        NONE = 'None', 'None'
        PARTIAL = 'Partial', 'Partial'
        DISCREPANCY = 'Discrepancy', 'Discrepancy'

    # --- existing fields (untouched) ----------------------------------------
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, related_name='procurements'
    )
    supplier = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'SUPPLIER'},
        related_name='fulfilled_orders'
    )
    owner = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'OWNER'},
        related_name='authorized_procurements'
    )
    order_date = models.DateField()
    delivery_date = models.DateField(null=True, blank=True)
    total_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    status = models.CharField(
        max_length=15, choices=Status.choices,
        default=Status.DRAFT                   # <-- changed default to Draft
    )

    # --- new fields ---------------------------------------------------------
    po_number = models.CharField(
        max_length=20, unique=True, blank=True,
        help_text="Auto-generated PO number e.g. PO-2026-001"
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['STAFF', 'BRANCH_MANAGER', 'OWNER']},
        related_name='created_procurements',
        help_text="Staff or manager who raised this purchase order"
    )
    receipt_flag = models.CharField(
        max_length=15, choices=ReceiptFlag.choices,
        default=ReceiptFlag.NONE,
        help_text="Flags whether received quantities had discrepancies"
    )

    def save(self, *args, **kwargs):
        # Auto-generate po_number on first save, same pattern as slip_number
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.po_number:
            from django.utils import timezone
            year = timezone.now().year
            self.po_number = f"PO-{year}-{str(self.pk).zfill(3)}"
            super().save(update_fields=['po_number'])

    def __str__(self):
        return f"{self.po_number or f'Procurement #{self.id}'} - {self.branch} - {self.status}"


class ProcurementItem(models.Model):
    # --- unchanged ---
    procurement = models.ForeignKey(
        Procurement, on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL,
        null=True, related_name='procurement_items'
    )
    quantity_ordered = models.PositiveIntegerField()
    quantity_received = models.PositiveIntegerField(default=0)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2)

    # --- new field ----------------------------------------------------------
    is_unexpected = models.BooleanField(
        default=False,
        help_text="Added during receiving — was not on the original purchase "
                  "order. Kept as a real line so the receipt reflects what "
                  "physically arrived, but flagged for the Owner to review."
    )

    @property
    def line_total(self):
        """Ordered quantity x cost price.

        Both procurement_detail.html and the printable PO render this column;
        without it the template's `item.line_total` silently resolved to
        nothing and every line printed as ₱0.00.
        """
        return self.quantity_ordered * self.cost_price

    def __str__(self):
        return f"{self.product.product_name} x{self.quantity_ordered}"


class SupplierProduct(models.Model):
    """Which products a supplier currently carries.

    Deliberately separate from ProcurementItem: that table records what was
    actually ordered from whom at the time and must never be rewritten, so it
    answers "what did we buy last March" but not "who do we buy this from
    now". This one is the forward-looking mapping — it's what a supplier's
    product sub-view lists and what a transfer moves between suppliers.
    """
    supplier = models.ForeignKey(
        User, on_delete=models.CASCADE,
        limit_choices_to={'role': 'SUPPLIER'},
        related_name='supplied_products'
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='supplier_links'
    )
    supplier_sku = models.CharField(
        max_length=50, blank=True,
        help_text="The supplier's own code for this product, if it differs "
                  "from our sku_code"
    )
    default_cost_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Pre-fills cost_price when this product is added to a new "
                  "purchase order for this supplier"
    )
    lead_time_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Typical days from order to delivery"
    )
    is_preferred = models.BooleanField(
        default=False,
        help_text="Preferred source when more than one supplier carries this product"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('supplier', 'product')
        ordering = ['product__product_name']

    def __str__(self):
        return f"{self.product.product_name} <- {self.supplier}"


class DeliveryScanLog(models.Model):
    """One row per barcode scanned on the receiving screen.

    ProcurementItem.quantity_received already stores the final tally; this
    stores how that tally was arrived at, so "who scanned what, when" is
    answerable if a discrepancy is disputed later. Same audit posture as
    inventory.StockMovement, which records every stock change individually
    rather than just the net result.
    """
    procurement = models.ForeignKey(
        Procurement, on_delete=models.CASCADE,
        related_name='scan_logs'
    )
    procurement_item = models.ForeignKey(
        ProcurementItem, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='scan_logs',
        help_text="Null when the scanned code matched nothing on this order"
    )
    scanned_code = models.CharField(max_length=64)
    matched_product = models.ForeignKey(
        Product, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='delivery_scans'
    )
    was_unexpected = models.BooleanField(
        default=False,
        help_text="The code resolved to no product at all, or to a product "
                  "that wasn't on this purchase order"
    )
    scanned_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='delivery_scans'
    )
    scanned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-scanned_at']

    def __str__(self):
        return f"{self.scanned_code} on {self.procurement.po_number}"