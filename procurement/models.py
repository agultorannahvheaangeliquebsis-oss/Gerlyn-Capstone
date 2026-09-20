
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

    # The only status changes procurement_update_status (template) and
    # ProcurementStatusUpdateAPIView (DRF) will apply. One definition, two
    # doors: the template view previously wrote request.POST['status'] straight
    # onto the row with no check, so a Received PO could be forced back to
    # Ordered — or to an arbitrary string (finding C-3). Content is exactly
    # what the DRF endpoint's local map already allowed:
    #
    #   Draft    -> Ordered | Cancelled
    #   Ordered  -> Received | Cancelled
    #   Received -> (terminal)
    #   Cancelled-> (terminal)
    #
    # Whether an Ordered -> Received move is legal is checked here, but the
    # receipt itself still runs through delivery_receive's own atomic block.
    ALLOWED_TRANSITIONS = {
        Status.DRAFT: {Status.ORDERED, Status.CANCELLED},
        Status.ORDERED: {Status.RECEIVED, Status.CANCELLED},
        Status.RECEIVED: set(),
        Status.CANCELLED: set(),
    }

    def can_transition_to(self, new_status):
        """True when a status view may move this order to `new_status`."""
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, set())

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
    # When the Owner authorized this order out of Draft. `owner` records WHO
    # authorized it and `created_by` who raised it — both already existed —
    # so this is the only field the authorization gate adds. Null on a Draft
    # order (nobody has approved it yet) and on legacy rows created before
    # the gate, which is why it stays nullable rather than defaulting to the
    # order_date: an approval that never happened must not look like one that
    # did (finding C-1).
    authorized_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the Owner approved this order out of Draft"
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
    def __init__(self, *args, **kwargs):
        if 'cost_price' in kwargs and 'unit_cost_snapshot' not in kwargs:
            kwargs['unit_cost_snapshot'] = kwargs.pop('cost_price')
        super().__init__(*args, **kwargs)
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
    unit_cost_snapshot = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        editable=False,
        help_text='Supplier cost copied when this purchase order is confirmed.',
    )

    # --- new field ----------------------------------------------------------
    is_unexpected = models.BooleanField(
        default=False,
        help_text="Added during receiving — was not on the original purchase "
                  "order. Kept as a real line so the receipt reflects what "
                  "physically arrived, but flagged for the Owner to review."
    )

    @property
    def line_total(self):
        """Ordered quantity x the confirmed supplier-cost snapshot.

        Both procurement_detail.html and the printable PO render this column;
        without it the template's `item.line_total` silently resolved to
        nothing and every line printed as ₱0.00.
        """
        return self.quantity_ordered * (self.unit_cost_snapshot or 0)

    @property
    def cost_price(self):
        return self.unit_cost_snapshot

    @cost_price.setter
    def cost_price(self, value):
        self.unit_cost_snapshot = value

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
    def __init__(self, *args, **kwargs):
        if 'default_cost_price' in kwargs and 'current_cost_price' not in kwargs:
            kwargs['current_cost_price'] = kwargs.pop('default_cost_price')
        super().__init__(*args, **kwargs)

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
    current_cost_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Supplier current cost, copied when a PO is confirmed.'
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

    @property
    def default_cost_price(self):
        return self.current_cost_price

    @default_cost_price.setter
    def default_cost_price(self, value):
        self.current_cost_price = value


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
