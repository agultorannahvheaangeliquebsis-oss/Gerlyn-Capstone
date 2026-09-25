# sales/models.py
# Changes from original:
#   - Added order_number (auto-generated)
#   - Added order_status (lifecycle: Pending → Approved → Preparing → Completed → Cancelled)
#   - Added delivery_address (for Online orders)
#   - All original fields untouched

from datetime import timedelta

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

    # --- refunds --------------------------------------------------------------
    # When this order actually became Completed — set by transaction_set_status
    # the moment it moves there, and at creation for a walk-in Cash sale (which
    # is Completed on the spot, never passing through transaction_set_status at
    # all). NOT the same as transaction_date: an online order can sit Pending
    # for days first, so transaction_date would understate its refund window.
    # Null for an order that has never been Completed. Orders that were already
    # Completed before this field existed were backfilled from transaction_date
    # (the closest honest approximation — see the migration).
    completed_at = models.DateTimeField(null=True, blank=True)

    # A refund is only offered within this many days of completion — a
    # grocery-appropriate return window, not a statutory one. One named
    # constant instead of a bare "3" wherever the rule is checked.
    REFUND_WINDOW = timedelta(days=3)

    # P14 — the running total of APPROVED refunds against this order, kept in
    # step by sales.refunds.approve_refund under the same row lock that flips
    # the Refund to Approved. total_amount is deliberately left alone: it is
    # the frozen figure the customer was charged and the receipt prints, so
    # what the order is worth NOW is net_amount below, never an edit to it.
    # Pending and Rejected requests never touch this — only money that has
    # actually been handed back does.
    refunded_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Sum of this order's approved refunds. total_amount is "
                  "never reduced; net_amount is total_amount minus this."
    )

    @property
    def net_amount(self):
        """What this order is worth after approved refunds."""
        return self.total_amount - self.refunded_amount

    def refund_window_expired(self):
        """True once REFUND_WINDOW has passed since this order was completed,
        or when there is no completion date to measure from at all (an order
        that has never been Completed has nothing to refund yet either)."""
        if self.completed_at is None:
            return True
        from django.utils import timezone
        return timezone.now() - self.completed_at > self.REFUND_WINDOW

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


class Refund(models.Model):
    """One refund against a Completed, Paid order — a financial correction
    after a legitimate sale, distinct from voiding a completed sale
    (transaction_set_status's Completed -> Cancelled path): a void undoes the
    whole order because something was wrong with it; a refund hands back money
    for specific items on an order that was otherwise correct.

    P14 — a refund is a REQUEST first. Any internal user with access to the
    order's branch may raise one (requested_by), and it sits Pending until an
    Owner/Branch Manager approves or rejects it. Nothing moves until then:
    approval (sales.refunds.approve_refund) is the single place that restocks
    Sellable lines, adds to SalesTransaction.refunded_amount and steps the
    billing record to Partially Refunded / Refunded, all in one transaction. A
    Pending request only RESERVES its quantities, so the same units can't be
    requested twice; a Rejected one gives them back. An Owner/Branch Manager
    submitting a refund themselves is approved in the same step
    (sales.refunds.SELF_APPROVE_ROLES) — the same trust A-1 gives them over
    their own sales.

    No payment gateway is integrated (Chapter 1, Limitation 1), so exactly
    like a payment being recorded rather than processed, an Approved refund is
    a RECORD that money was handed back — refund_method says how, but nothing
    here moves anything. Order status is never changed by a refund; the
    Refund/RefundItem rows (Approved ones) are the source of truth for how much
    of an order has been refunded, which is why an order can carry more than
    one — a customer bringing back 2 items today and 1 more next week is two
    Refund rows, not an edit to the first one. Every transition is written to
    RefundEvent, the append-only trail behind the order page's refund log.
    """
    class RefundMethod(models.TextChoices):
        CASH = 'Cash', 'Cash'
        GCASH = 'GCash', 'GCash'
        BANK_TRANSFER = 'Bank Transfer', 'Bank Transfer'

    class Status(models.TextChoices):
        PENDING = 'Pending', 'Pending approval'
        APPROVED = 'Approved', 'Approved'
        REJECTED = 'Rejected', 'Rejected'

    class ReasonCode(models.TextChoices):
        DEFECTIVE = 'Defective', 'Defective / damaged item'
        WRONG_ITEM = 'Wrong Item', 'Wrong item supplied'
        EXPIRED = 'Expired', 'Expired / spoiled'
        NOT_AS_DESCRIBED = 'Not As Described', 'Not as described'
        PRICING_ERROR = 'Pricing Error', 'Pricing / billing error'
        CHANGED_MIND = 'Changed Mind', 'Customer changed mind'
        OTHER = 'Other', 'Other (explain below)'

    class RefundType(models.TextChoices):
        # Decided by the server from what the request covers, never taken from
        # the client: Full only when it returns every unit the order sold.
        FULL = 'Full', 'Full refund'
        PARTIAL = 'Partial', 'Partial refund'

    # A request in one of these states holds its quantities against the order
    # (see sales.refunds.refundable_lines); a Rejected one holds nothing.
    RESERVING_STATUSES = ('Pending', 'Approved')

    transaction = models.ForeignKey(
        SalesTransaction, on_delete=models.CASCADE, related_name='refunds'
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    reason_code = models.CharField(max_length=20, choices=ReasonCode.choices)
    # The requester's own words. Required when reason_code is Other (enforced
    # in sales.refunds, not here), optional detail otherwise.
    reason = models.CharField(max_length=255, blank=True)
    refund_type = models.CharField(max_length=7, choices=RefundType.choices)
    refund_method = models.CharField(max_length=20, choices=RefundMethod.choices)
    # The sum of this refund's RefundItem.amount, stored rather than summed on
    # every read — same reasoning as SalesTransaction.total_amount.
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)

    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['OWNER', 'BRANCH_MANAGER', 'STAFF']},
        related_name='refunds_requested',
        help_text="Whoever raised this refund."
    )
    requested_at = models.DateTimeField(auto_now_add=True)

    # Set only on approval — who released the money and when. A rejection's
    # actor and time live on its RefundEvent, with decision_note holding why.
    refunded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['OWNER', 'BRANCH_MANAGER']},
        related_name='refunds_issued',
        help_text="The Owner or Branch Manager who approved this refund."
    )
    refunded_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(
        max_length=255, blank=True,
        help_text="Required when a request is rejected — why."
    )

    def __str__(self):
        return f"Refund #{self.id} - {self.transaction.order_number} - ₱{self.total_amount}"

    class Meta:
        ordering = ['-requested_at', '-id']


class RefundItem(models.Model):
    """One line of a Refund — how much of one SalesItem came back, and in
    what condition. condition decides the stock effect
    (sales.views._restock_refunded_item): Sellable goes back onto the branch's
    floor as a fresh, honestly-unknown-provenance batch (the same fallback
    _create_void_reversal_batch already uses for a void reversal that can't be
    traced to a real lot); Damaged changes nothing in stock at all.
    """
    class Condition(models.TextChoices):
        SELLABLE = 'Sellable', 'Sellable'
        DAMAGED = 'Damaged', 'Damaged / unsellable'

    refund = models.ForeignKey(Refund, on_delete=models.CASCADE, related_name='items')
    # CASCADE: a SalesItem is only ever removed by its SalesTransaction being
    # removed (nothing edits/deletes one on its own), which would take this
    # refund with it via `refund` above anyway — consistent with
    # SalesItemBatchConsumption.sales_item just above.
    sales_item = models.ForeignKey(
        SalesItem, on_delete=models.CASCADE, related_name='refund_items'
    )
    quantity = models.PositiveIntegerField()
    condition = models.CharField(max_length=10, choices=Condition.choices)
    # quantity x the SalesItem's own frozen unit_price — already the price the
    # customer actually paid (a PWD/Senior line's unit_price is already the
    # discounted figure), so no discount arithmetic is redone here.
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.sales_item.product.product_name} x{self.quantity} ({self.condition})"


class RefundEvent(models.Model):
    """Append-only log of a Refund's life: requested, approved, rejected.

    Written by sales.refunds only, never edited — the same role StockMovement
    plays for stock. The refund itself keeps just its current state; this is
    who moved it there, when, and what they said. The approval event's note
    also records the payment-status step the approval caused ("Payment: Paid
    -> Partially Refunded"), so the order page's log reads end to end.
    """
    refund = models.ForeignKey(Refund, on_delete=models.CASCADE, related_name='events')
    from_status = models.CharField(max_length=10, blank=True)
    to_status = models.CharField(max_length=10, choices=Refund.Status.choices)
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='refund_events'
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f"Refund #{self.refund_id}: {self.from_status or '—'} -> {self.to_status}"


class OrderStatusHistory(models.Model):
    """Append-only trail of an order's lifecycle status (Pending -> Approved
    -> Preparing -> Completed / Cancelled).

    SalesTransaction keeps only the current order_status. This is every step
    that led there — who, when, and (for a cancellation or void) why — shown
    on the order page and served by the sales API. The first row of an order
    (from_status blank) is written when the order is created
    (sales.signals.record_initial_status); every later one by
    transaction_set_status. Orders that predate this table simply have no
    rows; nothing is invented for them.
    """
    transaction = models.ForeignKey(
        SalesTransaction, on_delete=models.CASCADE, related_name='status_history'
    )
    from_status = models.CharField(max_length=10, blank=True)
    to_status = models.CharField(max_length=10, choices=SalesTransaction.OrderStatus.choices)
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='order_status_changes'
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']
        verbose_name_plural = "Order status history"

    def __str__(self):
        return f"{self.transaction_id}: {self.from_status or '—'} -> {self.to_status}"