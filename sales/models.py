# sales/models.py
# Changes from original:
#   - Added order_number (auto-generated)
#   - Added order_type (Retail / Wholesale) — separate from transaction_type
#   - Added order_status (lifecycle: Pending → Approved → Preparing → Completed → Cancelled)
#   - Added delivery_address (for Online orders)
#   - All original fields untouched

from django.db import models
from branches.models import Branch
from inventory.models import Product
from accounts.models import User


class SalesTransaction(models.Model):
    class TransactionType(models.TextChoices):
        # Original — channel (how the order was placed)
        ONLINE = 'Online', 'Online'
        PHYSICAL = 'Physical', 'Physical'

    class ModeOfPayment(models.TextChoices):
        # Original — unchanged
        CASH = 'Cash', 'Cash'
        GCASH = 'GCash', 'GCash'
        CARD = 'Card', 'Card'

    class OrderType(models.TextChoices):        # <-- new: pricing tier
        RETAIL = 'Retail', 'Retail'
        WHOLESALE = 'Wholesale', 'Wholesale'

    class OrderStatus(models.TextChoices):      # <-- new: lifecycle
        PENDING = 'Pending', 'Pending'
        APPROVED = 'Approved', 'Approved'
        PREPARING = 'Preparing', 'Preparing'
        COMPLETED = 'Completed', 'Completed'
        CANCELLED = 'Cancelled', 'Cancelled'

    # --- existing fields (untouched) ----------------------------------------
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, related_name='transactions'
    )
    staff = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'STAFF'},
        related_name='processed_transactions'
    )
    customer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role': 'CUSTOMER'},
        related_name='purchases'
    )
    transaction_date = models.DateTimeField(auto_now_add=True)
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    transaction_type = models.CharField(
        max_length=10, choices=TransactionType.choices,
        default=TransactionType.PHYSICAL
    )
    mode_of_payment = models.CharField(
        max_length=10, choices=ModeOfPayment.choices,
        default=ModeOfPayment.CASH
    )

    # --- new fields ---------------------------------------------------------
    order_number = models.CharField(
        max_length=20, unique=True, blank=True,
        help_text="Auto-generated e.g. ORD-2026-001"
    )
    order_type = models.CharField(
        max_length=10, choices=OrderType.choices,
        default=OrderType.RETAIL,
        help_text="Retail uses unit_price; Wholesale uses wholesale_price"
    )
    order_status = models.CharField(
        max_length=10, choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
        help_text="Order lifecycle status"
    )
    delivery_address = models.TextField(
        blank=True,
        help_text="Required for Online orders; blank for Physical"
    )

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.order_number:
            from django.utils import timezone
            year = timezone.now().year
            self.order_number = f"ORD-{year}-{str(self.pk).zfill(3)}"
            super().save(update_fields=['order_number'])

    def __str__(self):
        return (
            f"{self.order_number or f'Transaction #{self.id}'} - "
            f"{self.branch} - {self.transaction_date.strftime('%Y-%m-%d')}"
        )


class SalesItem(models.Model):
    # --- unchanged ---
    transaction = models.ForeignKey(
        SalesTransaction, on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL,
        null=True, related_name='sales_items'
    )
    quantity_sold = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(
        max_digits=12, decimal_places=2, editable=False
    )

    def save(self, *args, **kwargs):
        self.subtotal = self.quantity_sold * self.unit_price
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.product_name} x{self.quantity_sold}"