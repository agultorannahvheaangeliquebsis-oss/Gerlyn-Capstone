
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

    def __str__(self):
        return f"{self.product.product_name} x{self.quantity_ordered}"