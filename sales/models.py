# sales/models.py
# Changes from original:
#   - Added order_number (auto-generated)
#   - Added order_status (lifecycle: Pending → Approved → Preparing → Completed → Cancelled)
#   - Added delivery_address (for Online orders)
#   - All original fields untouched

from django.db import models, transaction
from branches.models import Branch
from inventory.models import Product
from accounts.models import User


class OrderNumberSequence(models.Model):
    """Per-calendar-year counter behind SalesTransaction.order_number.

    order_number is ``ORD-{year}-{NNN}``. NNN used to be the global
    auto-increment pk, which never resets — so the first order of a new
    year came out as ORD-2027-104, reading as if a hundred orders had
    vanished at the year boundary. This table holds the last sequence
    value handed out for each year instead, so NNN restarts at 001 every
    January.

    SalesTransaction.save bumps the row under ``select_for_update`` in the
    same transaction as the INSERT, so two concurrent creates can't read
    the same last_value — the row-lock pattern billing_confirm (D-1) and
    the billing confirm API (F-02) already use against this same class of
    race.
    """
    year = models.PositiveIntegerField(primary_key=True)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.year}: {self.last_value}"


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

    class OrderStatus(models.TextChoices):      # <-- new: lifecycle
        PENDING = 'Pending', 'Pending'
        APPROVED = 'Approved', 'Approved'
        PREPARING = 'Preparing', 'Preparing'
        COMPLETED = 'Completed', 'Completed'
        CANCELLED = 'Cancelled', 'Cancelled'

    class FulfillmentType(models.TextChoices):  # <-- new: how it's handed over
        # An EXPLICIT choice made at checkout, never inferred from whether
        # delivery_address came back blank. A genuine pickup order and a
        # delivery order that lost its address used to be indistinguishable
        # to everything downstream, the Order Console included (F-11).
        #
        # Values are Capitalised to match every other TextChoices on this
        # model (Online/Physical, Retail/Wholesale, Pending/Approved...).
        DELIVERY = 'Delivery', 'Delivery'
        PICKUP = 'Pickup', 'Pickup'

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
    # X-3 — `staff` above only ever holds a STAFF account (see
    # limit_choices_to), so a walk-in sale rung up by the Owner or a Branch
    # Manager wrote staff=None, exactly like a sale with no operator at all.
    # The Order Console showed "Recorded by —", the printed receipt's
    # "Served by" line was blank, and is_self_confirmation (A-1) could never
    # fire for an Owner/Manager sale because there was no actor on the
    # record to compare against. `staff` is left exactly as-is — nothing
    # downstream that reads it (the wholesale-price limit_choices_to, the
    # Staff Sales report) should start seeing Owner/Manager rows — and this
    # is the actual operator, any internal role, written on every creation
    # path: the POS (transaction_add), online checkout (nulled there — an
    # online order has no staff-side operator, the customer places it) and
    # the API write path.
    recorded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['OWNER', 'BRANCH_MANAGER', 'STAFF']},
        related_name='recorded_transactions',
        help_text="Whoever operated the till for this sale — Owner, Branch "
                  "Manager or Staff. Null for an online order (the customer "
                  "placed it themselves) and for a walk-in sale recorded "
                  "before this field existed."
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
    order_status = models.CharField(
        max_length=10, choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
        help_text="Order lifecycle status"
    )
    delivery_address = models.TextField(
        blank=True,
        help_text="Required for a Delivery order; blank for Pickup and for "
                  "walk-in (Physical) sales"
    )
    fulfillment_type = models.CharField(
        max_length=10, choices=FulfillmentType.choices,
        default=FulfillmentType.PICKUP,
        help_text="How the customer takes the order. 'Delivery' always "
                  "carries a delivery_address; 'Pickup' never does. A walk-in "
                  "(Physical) sale is handed over at the counter, so it is "
                  "Pickup."
    )

    # Line items are immutable once an order exists — there is no view that
    # edits an existing SalesItem, by design. Correcting a mistake means
    # cancelling this order (with a reason, so the record says why) and
    # ringing up a new one; `replaces` is what keeps that new order visibly
    # connected to the one it stands in for, rather than looking like an
    # unrelated sale that happens to follow a cancellation.
    cancel_reason = models.CharField(
        max_length=255, blank=True,
        help_text="Required when order_status is set to Cancelled — why the "
                  "order was voided rather than fulfilled."
    )
    replaces = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='replaced_by',
        help_text="The cancelled order this one was rung up to correct, if any."
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

    # Who granted the statutory discount, and when. The same maker/checker
    # audit trail BillingRecord.confirmed_by / date_confirmed keeps for a
    # payment confirmation (A-1), for the same reason: this is a decision a
    # named person made on the strength of an ID they say they saw, and a
    # 20% reduction with nobody's name against it is exactly what an audit
    # asks about.
    #
    # Null on a sale that carries no discount, and on one rung up through
    # the POS before this field existed.
    discount_applied_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['OWNER', 'BRANCH_MANAGER', 'STAFF']},
        related_name='applied_discounts',
        help_text="Staff member who verified the PWD/OSCA ID and applied "
                  "the discount."
    )
    discount_applied_at = models.DateTimeField(null=True, blank=True)
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

    def can_apply_pwd_senior_discount(self):
        """Whether staff may apply the statutory PWD/Senior discount here.

        Only where a staff member actually sees the ID face to face:

          * a walk-in (Physical) sale, rung up at the counter, and
          * an Online order the customer collects in store (Pickup), at the
            moment they arrive.

        A Delivery order has no in-person verification step anywhere in this
        system — nobody at the branch ever meets the cardholder — so it can
        never receive the discount (F-04). The customer-facing checkout does
        not apply it either: every order it writes is undiscounted, and staff
        grant it afterwards from the Order Console if the ID warrants it.

        This is the single definition of the rule; the view enforces it and
        the template asks it whether to render the form, so the control
        shown and the control allowed can never drift apart.
        """
        if self.transaction_type == self.TransactionType.PHYSICAL:
            return True
        return self.fulfillment_type == self.FulfillmentType.PICKUP

    def save(self, *args, **kwargs):
        # Existing row, or a caller that set order_number explicitly: nothing
        # to allocate, save straight through.
        if self.pk is not None or self.order_number:
            super().save(*args, **kwargs)
            return

        from django.utils import timezone
        year = timezone.now().year
        # Allocate the number and INSERT the row in one transaction:
        #   * the per-year counter is read under select_for_update, so two
        #     concurrent creates can't be handed the same NNN (the row-lock
        #     pattern billing_confirm / the billing API already use — D-1,
        #     F-02);
        #   * order_number is written on the INSERT itself, so the old
        #     create-then-patch step is gone — no row ever sits under the
        #     UNIQUE index carrying order_number='' for two concurrent
        #     creates to collide on.
        with transaction.atomic():
            seq, _ = (
                OrderNumberSequence.objects
                .select_for_update()
                .get_or_create(year=year)
            )
            seq.last_value += 1
            seq.save(update_fields=['last_value'])
            self.order_number = f"ORD-{year}-{seq.last_value:03d}"
            super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.order_number or f'Transaction #{self.id}'} - "
            f"{self.branch} - {self.transaction_date.strftime('%Y-%m-%d')}"
        )

    class Meta:
        indexes = [
            # Every branch-scoped surface in the back office filters on
            # exactly this pair — core.scoping narrows to a branch, then the
            # date range is applied on top (the Dashboard's daily series and
            # its previous-window comparison, the Reports Sales tab, the
            # branch-breakdown donut, every CSV export). The standalone
            # db_index on transaction_date can only serve the date half, so
            # a Branch Manager's report still scanned every branch's rows in
            # the window before narrowing. Descending on the date because
            # every one of those queries reads newest-first.
            models.Index(
                fields=['branch', '-transaction_date'],
                name='sales_txn_branch_date_idx',
            ),
        ]


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


class SalesItemBatchConsumption(models.Model):
    """Which Batch(es) a SalesItem's quantity was actually drawn from, and
    how much from each.

    O-1 — voiding a completed sale used to reverse only the aggregate
    Inventory row (quantity_on_hand), because nothing recorded which
    specific batch(es) a sale had drawn down, so there was no correct batch
    to credit back. From that moment the aggregate figure and the
    batch-level FEFO breakdown disagreed permanently, and every sale after
    it drew against a batch pool that was quietly short.

    This is written once, at deduction time (sales.views._deduct_stock and
    the matching SalesTransactionWriteSerializer.create path), directly from
    the (batch, quantity_taken) pairs inventory.models.Batch.consume_fefo
    already returns — no new arithmetic, just keeping the answer it already
    computes. It is read once, at void time (sales.views._reverse_stock), to
    credit exactly those batches back. Never edited after creation.

    One SalesItem can span several rows here: consume_fefo drains the
    nearest-expiry batch first and moves on to the next one if it doesn't
    cover the whole line.
    """
    sales_item = models.ForeignKey(
        SalesItem, on_delete=models.CASCADE, related_name='batch_consumptions'
    )
    # SET_NULL, not CASCADE: a Batch row being deleted for some unrelated
    # reason must not erase the fact that this sale drew quantity_taken units
    # from somewhere — _reverse_stock falls back to a fresh no-expiry batch
    # for that portion when batch is null, rather than silently dropping it.
    batch = models.ForeignKey(
        'inventory.Batch', on_delete=models.SET_NULL, null=True,
        related_name='sales_consumptions',
    )
    quantity_taken = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Sales item batch consumption"
        verbose_name_plural = "Sales item batch consumptions"