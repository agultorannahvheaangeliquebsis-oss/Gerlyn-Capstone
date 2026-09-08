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
    # reaches is decided per product, by whichever eligibility flag matches
    # the claim — Product.is_pwd_eligible or Product.is_senior_eligible.
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

    # --- cash tendered (Walk-in Sale, Cash payments only) -------------------
    # Null rather than 0 for anything that isn't a counter cash sale — an
    # Online order or a GCash/Card payment never had cash handed over, and
    # "not applicable" needs to read differently from "tendered nothing".
    amount_tendered = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Cash handed over at the counter. Only recorded for "
                  "Cash walk-in sales."
    )
    change_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="amount_tendered minus total_amount, frozen at sale time."
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


class SalesReturn(models.Model):
    """A customer return against a COMPLETED sale — per line item, unlike
    transaction_set_status's whole-order void (Completed -> Cancelled). A
    customer returning 2 of the 5 items they bought does not need the other
    3 cancelled, and the order itself is never touched by a return.

    Two independently-decided outcomes travel together on one record:
      - condition: whether the returned stock goes back to the floor
        (Resellable) or is written off (Damaged/Write-off) — decided at
        Approve, same moment sales.views._restock_return runs.
      - refund: money owed back, tracked to Refunded separately from
        Approved so "approved, stock is back, refund not yet paid" is a
        real, visible state rather than being folded into Approved.

    Deliberately does NOT touch billing.BillingRecord.status — that field
    describes the ORDER's bill as a whole (Pending/Paid/Rejected/Cancelled),
    and a partial return leaves the original sale exactly as billed. Refund
    status lives here instead, same as PaymentMethod.COD's "recorded only,
    no gateway" limitation documented on BillingRecord.
    """

    class Status(models.TextChoices):
        PENDING = 'Pending', 'Pending Review'
        APPROVED = 'Approved', 'Approved'
        REFUNDED = 'Refunded', 'Refunded'
        REJECTED = 'Rejected', 'Rejected'

    class Reason(models.TextChoices):
        WRONG_ITEM = 'Wrong Item', 'Wrong item given'
        DEFECTIVE = 'Defective', 'Defective / damaged'
        DAMAGED_IN_TRANSIT = 'Damaged in Transit', 'Damaged in transit'
        EXPIRED = 'Expired', 'Expired stock'
        CHANGED_MIND = 'Changed Mind', 'Changed mind'
        OTHER = 'Other', 'Other'

    class Condition(models.TextChoices):
        RESELLABLE = 'Resellable', 'Resellable'
        WRITE_OFF = 'Write-off', 'Damaged / Write-off'

    class RefundMethod(models.TextChoices):
        STORE_CREDIT = 'Store Credit', 'Store Credit'
        CASH = 'Cash', 'Cash'
        ORIGINAL = 'Original Payment Method', 'Original Payment Method'

    transaction = models.ForeignKey(
        SalesTransaction, on_delete=models.CASCADE, related_name='returns'
    )
    return_number = models.CharField(
        max_length=20, unique=True, blank=True,
        help_text="Auto-generated e.g. RET-2026-001"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    reason = models.CharField(max_length=20, choices=Reason.choices)
    condition = models.CharField(max_length=12, choices=Condition.choices)
    refund_method = models.CharField(max_length=30, choices=RefundMethod.choices)
    refund_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Sum of every line's refund_subtotal, frozen at request time."
    )
    notes = models.TextField(blank=True)

    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='requested_returns'
    )
    decided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='decided_returns',
        help_text="Who approved or rejected this return."
    )
    decision_notes = models.CharField(
        max_length=255, blank=True,
        help_text="Reason given when rejecting a return."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.return_number:
            from django.utils import timezone
            year = timezone.now().year
            self.return_number = f"RET-{year}-{str(self.pk).zfill(3)}"
            super().save(update_fields=['return_number'])

    def __str__(self):
        return f"{self.return_number or f'Return #{self.id}'} - {self.transaction.order_number}"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Sales Return"
        verbose_name_plural = "Sales Returns"


class SalesReturnItem(models.Model):
    sales_return = models.ForeignKey(
        SalesReturn, on_delete=models.CASCADE, related_name='items'
    )
    # The ORIGINAL line being returned against, not just the product — so a
    # sale with the same product on two lines (e.g. two different discount
    # treatments) can't have a return misattributed to the wrong one.
    sales_item = models.ForeignKey(
        SalesItem, on_delete=models.CASCADE, related_name='return_items'
    )
    quantity_returned = models.PositiveIntegerField()
    refund_subtotal = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="quantity_returned * the line's unit_price, frozen at request time."
    )

    def __str__(self):
        return f"{self.sales_item.product.product_name} x{self.quantity_returned}"