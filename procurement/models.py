
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from branches.models import Branch
from inventory.models import Product
from accounts.models import User


class Procurement(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'Draft', 'Draft'
        APPROVED = 'Approved', 'Approved'
        # Price and delivery date are negotiated between the two sides once
        # a PO is Approved — never set by whoever raised the order. These
        # three sit between Approved and Incoming:
        #   Approved                -> supplier submits the first offer
        #   Pending Owner Review    -> buyer's turn: accept, counter, or cancel
        #   Pending Supplier Review -> supplier's turn: accept, counter, or cancel
        #   Terms Agreed            -> either side accepted; only the
        #                              supplier's own final confirmation
        #                              (never an accept alone) moves it to
        #                              Incoming — see procurement.views.
        PENDING_OWNER_REVIEW = 'Pending Owner Review', 'Pending Owner Review'
        PENDING_SUPPLIER_REVIEW = 'Pending Supplier Review', 'Pending Supplier Review'
        TERMS_AGREED = 'Terms Agreed', 'Terms Agreed'
        INCOMING = 'Incoming', 'Incoming'
        PARTIALLY_RECEIVED = 'Partially Received', 'Partially Received'
        RECEIVED = 'Received', 'Received'
        CANCELLED = 'Cancelled', 'Cancelled'

    class FundingSource(models.TextChoices):
        CASH = 'Cash on Hand', 'Cash on Hand'
        BANK = 'Bank Transfer', 'Bank Transfer'
        GCASH = 'GCash', 'GCash'
        OWNER_FUNDS = "Owner's Funds", "Owner's Funds"
        OTHER = 'Other', 'Other'

    # Once the buyer and supplier have agreed terms the spend is committed, so
    # where the money comes from stops being editable. Cancelled is locked too:
    # a closed order's paperwork shouldn't change.
    FUNDING_LOCKED_STATUSES = frozenset({
        Status.TERMS_AGREED, Status.INCOMING, Status.PARTIALLY_RECEIVED,
        Status.RECEIVED, Status.CANCELLED,
    })

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
    #   Draft                   -> Approved | Cancelled
    #   Approved                -> Pending Owner Review | Cancelled
    #   Pending Owner Review    -> Pending Supplier Review | Terms Agreed | Cancelled
    #   Pending Supplier Review -> Pending Owner Review | Terms Agreed | Cancelled
    #   Terms Agreed            -> Incoming | Cancelled
    #   Incoming                -> Partially Received | Received | Cancelled
    #   Partially Received      -> Received | Cancelled
    #   Received/Cancelled      -> (terminal)
    #
    # Approved -> Incoming/Received used to be legal too, for direct/
    # backwards-compatible receiving with no in-app negotiation. That
    # backdoor is gone: it let anyone with status-update access skip price/
    # date negotiation entirely by POSTing a bare status, which is exactly
    # what negotiation exists to prevent. Incoming is now reachable only
    # through the supplier's own final confirmation
    # (procurement_supplier_confirm), once Terms Agreed — never through
    # procurement_update_status's generic status POST, which explicitly
    # refuses every negotiation-related target (see that view).
    ALLOWED_TRANSITIONS = {
        Status.DRAFT: {Status.APPROVED, Status.CANCELLED},
        Status.APPROVED: {Status.PENDING_OWNER_REVIEW, Status.CANCELLED},
        Status.PENDING_OWNER_REVIEW: {
            Status.PENDING_SUPPLIER_REVIEW, Status.TERMS_AGREED, Status.CANCELLED,
        },
        Status.PENDING_SUPPLIER_REVIEW: {
            Status.PENDING_OWNER_REVIEW, Status.TERMS_AGREED, Status.CANCELLED,
        },
        Status.TERMS_AGREED: {Status.INCOMING, Status.CANCELLED},
        Status.INCOMING: {
            Status.PARTIALLY_RECEIVED, Status.RECEIVED, Status.CANCELLED,
        },
        Status.PARTIALLY_RECEIVED: {
            Status.RECEIVED, Status.CANCELLED,
        },
        Status.RECEIVED: set(),
        Status.CANCELLED: set(),
    }

    @property
    def funding_locked(self):
        return self.status in self.FUNDING_LOCKED_STATUSES

    def can_transition_to(self, new_status):
        """True when a status view may move this order to `new_status`."""
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, set())

    # A PO mid-negotiation is real, committed spend (it already survived
    # Draft -> Approved) but has no delivery_date yet and no locked-in cost
    # — anything that used to treat "Approved...Partially Received" as the
    # open/uncompleted span must widen to include these three, or a
    # negotiating PO silently drops out of dashboards, reports and KPI
    # counts while it's arguably the order most worth surfacing. Shared here
    # so reports/views.py, reports/api_views.py and accounts/views.py's own
    # "open orders" tallies read the same span instead of each hand-copying
    # a status list that quietly goes stale the next time this changes.
    NEGOTIATION_STATUSES = frozenset({
        Status.PENDING_OWNER_REVIEW, Status.PENDING_SUPPLIER_REVIEW, Status.TERMS_AGREED,
    })
    # Every status short of Received/Cancelled, including Draft.
    OPEN_STATUSES = frozenset({Status.DRAFT, Status.APPROVED}) | NEGOTIATION_STATUSES | frozenset({
        Status.INCOMING, Status.PARTIALLY_RECEIVED,
    })
    # Same, but Draft excluded — Draft has no owner/authorized_at yet and
    # commits no spend, so it's left out of anything counting "committed"
    # open orders (matches supplier_portal.html's own Draft exclusion).
    COMMITTED_OPEN_STATUSES = OPEN_STATUSES - {Status.DRAFT}

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
    # The AGREED delivery date, final once negotiation completes — null
    # until then. What the supplier is currently offering, mid-negotiation,
    # lives separately in proposed_delivery_date below and is never copied
    # here until the supplier's own final confirmation.
    delivery_date = models.DateField(null=True, blank=True)
    actual_receipt_date = models.DateField(
        null=True, blank=True,
        help_text='Date goods were most recently received against this PO.',
    )
    # Zero is a legitimate value here — a Draft order with no lines yet, or
    # any order before snapshot_supplier_costs/apply_agreed_terms has run,
    # genuinely has no cost yet. This is a computed total (quantity x unit
    # cost, summed), not a price or cost entered by a person, so it is only
    # ever guarded against going negative, not against being zero.
    total_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        validators=[MinValueValidator(Decimal('0'))],
    )
    status = models.CharField(
        max_length=24, choices=Status.choices,
        default=Status.DRAFT                   # <-- changed default to Draft
    )

    # --- negotiation (price/date) --------------------------------------------
    # The date currently on the table — whoever's turn it ISN'T proposed it.
    # Overwritten on every offer/counter; each round's history is already on
    # record via the audit log (Procurement is audit-tracked), so nothing
    # here needs to remember prior rounds itself. Copied onto delivery_date
    # only by the supplier's final confirmation (procurement_supplier_confirm).
    # Where the money for this order comes from (P19). Blank only on orders
    # raised before the field existed; every new order must name a source.
    funding_source = models.CharField(
        max_length=20, choices=FundingSource.choices, blank=True, default='',
    )
    funding_note = models.CharField(
        max_length=200, blank=True,
        help_text="Detail for the funding source; required when it is Other.",
    )

    proposed_delivery_date = models.DateField(null=True, blank=True)
    # Who is waiting on a response — i.e. whoever did NOT submit the
    # currently-pending proposed_delivery_date/ProcurementItem.proposed_unit_cost.
    # Kept distinct from `status` (which already encodes whose turn it is)
    # purely so the review screen can say "Century Pacific proposed…" /
    # "You countered with…" without re-deriving who from status + role.
    proposed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
        help_text='Who submitted the currently pending price/date proposal.',
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

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_cost__gte=0),
                name='procurement_total_cost_not_negative',
            ),
        ]


# Temporary source-code compatibility for integrations that imported the old
# enum member. Its value is now the explicit Approved state; no new database
# row is written as Ordered.
Procurement.Status.ORDERED = Procurement.Status.APPROVED


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
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='Supplier cost copied when this purchase order is confirmed. '
                  'Null until then; once set it must be greater than zero.',
    )

    # --- negotiation ----------------------------------------------------------
    # This line's cost as of the CURRENT pending proposal (Procurement.
    # proposed_by says who's turn it now is). Overwritten on every offer/
    # counter, copied onto unit_cost_snapshot only once terms are agreed and
    # the supplier gives their final confirmation — see
    # procurement.views.procurement_supplier_confirm.
    proposed_unit_cost = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='Cost currently on the table for this line. Null until a '
                  'proposal exists; once set it must be greater than zero.',
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

    @property
    def remaining_quantity(self):
        if self.is_unexpected:
            return 0
        return max(self.quantity_ordered - self.quantity_received, 0)

    @cost_price.setter
    def cost_price(self, value):
        self.unit_cost_snapshot = value

    def __str__(self):
        return f"{self.product.product_name} x{self.quantity_ordered}"

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(unit_cost_snapshot__isnull=True)
                    | models.Q(unit_cost_snapshot__gt=0)
                ),
                name='procurementitem_unit_cost_snapshot_positive_or_null',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(proposed_unit_cost__isnull=True)
                    | models.Q(proposed_unit_cost__gt=0)
                ),
                name='procurementitem_proposed_unit_cost_positive_or_null',
            ),
        ]


class SupplierProductQuerySet(models.QuerySet):
    def approved(self):
        """Offers the store has accepted. Only these may appear on a purchase
        order or have their cost snapshotted — a supplier's own proposal
        means nothing to procurement until the Owner has approved it."""
        return self.filter(status=SupplierProduct.Status.APPROVED)


class SupplierProduct(models.Model):
    """Which products a supplier currently carries.

    Deliberately separate from ProcurementItem: that table records what was
    actually ordered from whom at the time and must never be rewritten, so it
    answers "what did we buy last March" but not "who do we buy this from
    now". This one is the forward-looking mapping — it's what a supplier's
    product sub-view lists and what a transfer moves between suppliers.
    """
    class Status(models.TextChoices):
        APPROVED = 'Approved', 'Approved'
        PROPOSED = 'Proposed', 'Proposed'
        REJECTED = 'Rejected', 'Rejected'

    objects = SupplierProductQuerySet.as_manager()

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
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='Supplier current cost, copied when a PO is confirmed. '
                  'Null until the supplier sets one; once set it must be '
                  'greater than zero.'
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

    # Offer workflow (P01). Default Approved so rows the Owner creates, and
    # every row that existed before the workflow did, keep working untouched;
    # only a supplier's own self-add starts as Proposed.
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.APPROVED,
    )
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_supplier_offers',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(
        max_length=200, blank=True,
        help_text="Why an offer was rejected, shown to the supplier.",
    )

    class Meta:
        unique_together = ('supplier', 'product')
        ordering = ['product__product_name']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(current_cost_price__isnull=True)
                    | models.Q(current_cost_price__gt=0)
                ),
                name='supplierproduct_current_cost_price_positive_or_null',
            ),
        ]

    def __str__(self):
        return f"{self.product.product_name} <- {self.supplier}"

    @property
    def default_cost_price(self):
        return self.current_cost_price

    @default_cost_price.setter
    def default_cost_price(self, value):
        self.current_cost_price = value


class SupplierProductRequest(models.Model):
    """A supplier asking the store to start carrying a product that is not in
    the Product Master List yet.

    Separate from SupplierProduct because that row needs a real Product, and
    only the Owner creates those: approving a request is what mints the
    Product (SKU, retail price and category are the Owner's call) plus the
    Approved SupplierProduct link at the supplier's proposed cost. Until then
    the request can't be ordered or stocked. Pending -> Approved | Rejected, or
    Withdrawn by the supplier.
    """

    class Status(models.TextChoices):
        PENDING = 'Pending', 'Pending'
        APPROVED = 'Approved', 'Approved'
        REJECTED = 'Rejected', 'Rejected'
        WITHDRAWN = 'Withdrawn', 'Withdrawn'

    supplier = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='product_requests',
    )
    product_name = models.CharField(max_length=150)
    unit_of_measure = models.CharField(
        max_length=20, choices=Product.UnitOfMeasure.choices,
        default=Product.UnitOfMeasure.PIECE,
    )
    proposed_cost = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING,
    )
    created_product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_product_requests',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(proposed_cost__gt=0),
                name='supplierproductrequest_proposed_cost_positive',
            ),
        ]

    def __str__(self):
        return f"{self.product_name} requested by {self.supplier} ({self.status})"


class DeliveryReceipt(models.Model):
    """One recorded receiving event against a purchase order.

    A PO can arrive in several shipments, so "who received it" is a fact about
    each receipt, not about the order — this table keeps them all. received_by
    is always the logged-in user who submitted the receive form (never typed,
    so it can't be faked); delivered_by is the optional courier/driver name
    from the loading bay. The stock itself is recorded separately as
    StockMovement rows, which carry the same actor.
    """

    procurement = models.ForeignKey(
        Procurement, on_delete=models.CASCADE, related_name='receipts',
    )
    received_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='delivery_receipts',
    )
    received_at = models.DateTimeField(default=timezone.now)
    receipt_date = models.DateField(
        null=True, blank=True,
        help_text="The actual receipt date entered on the receive form.",
    )
    delivered_by = models.CharField(
        max_length=100, blank=True,
        help_text="Courier or driver who handed the goods over, if noted.",
    )

    class Meta:
        ordering = ['received_at', 'id']

    @property
    def received_by_name(self):
        from core.people import person_display_name
        return person_display_name(self.received_by)

    def __str__(self):
        return f"{self.procurement} received by {self.received_by} at {self.received_at:%Y-%m-%d %H:%M}"


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
