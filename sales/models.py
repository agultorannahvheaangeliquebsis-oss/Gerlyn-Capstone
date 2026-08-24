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
        CASH = 'Cash', 'Cash'
        GCASH = 'GCash', 'GCash'
        CARD = 'Card', 'Card'
        # Online-order options. The system only RECORDS these — no payment
        # gateway is integrated (see Chapter 1, Limitation 1).
        COD = 'Cash on Delivery', 'Cash on Delivery'
        PAY_AT_STORE = 'Pay at Store', 'Pay at Store'

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
    # Indexed: every range-scoped surface in the back office filters on this
    # column — the Dashboard's daily series and its previous-window
    # comparison, the Reports Sales tab's KPIs, trend panel and donut, and
    # every CSV export — and the daily charts hit it twice per request
    # (selected window plus the one before it). The §2e "All time" preset
    # also takes a MIN() on it. No Meta ordering or index exists on this
    # model, so this is the one the queries actually need.
    transaction_date = models.DateTimeField(auto_now_add=True, db_index=True)
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    transaction_type = models.CharField(
        max_length=10, choices=TransactionType.choices,
        default=TransactionType.PHYSICAL
    )
    mode_of_payment = models.CharField(
        max_length=20, choices=ModeOfPayment.choices,
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

    # --- statutory discount (PWD / Senior Citizen) --------------------------
    # Claimed once per transaction, not per line: the discount belongs to a
    # cardholder, and one sale has one cardholder. Which LINES it actually
    # reaches is decided per product by Product.is_pwd_senior_eligible.
    class DiscountType(models.TextChoices):
        NONE = 'None', 'None'
        PWD = 'PWD', 'PWD'
        SENIOR = 'Senior', 'Senior Citizen'

    discount_type = models.CharField(
        max_length=10, choices=DiscountType.choices,
        default=DiscountType.NONE
    )
    discount_id_number = models.CharField(
        max_length=50, blank=True,
        help_text="PWD / OSCA ID presented at the counter. Required when a "
                  "discount is claimed — this is the recordkeeping the "
                  "statutory discount is conditioned on."
    )
    discount_holder_name = models.CharField(max_length=150, blank=True)
    total_discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    total_vat_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="VAT actually embedded across all lines, summed. Recorded "
                  "for reporting — it is already inside total_amount, not "
                  "added on top of it."
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

    # --- tax / discount breakdown, frozen at sale time ----------------------
    # Stored rather than recomputed on read: a product's price and its
    # VAT/discount flags can both change later, but a past receipt has to keep
    # showing what the customer was actually charged.
    is_vat_exempt_line = models.BooleanField(
        default=False,
        help_text="This line carried no VAT — either the product is flagged "
                  "VAT-exempt, or a PWD/Senior discount applied to it"
    )
    vat_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="VAT embedded in this line's subtotal (not added on top)"
    )
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="PWD/Senior discount taken off this line"
    )

    def save(self, *args, **kwargs):
        # unit_price is the price this line was actually sold at — for a
        # discounted line, sales.pricing has already reduced it, so subtotal
        # stays a plain qty x price and needs no discount arithmetic here.
        self.subtotal = self.quantity_sold * self.unit_price
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.product_name} x{self.quantity_sold}"