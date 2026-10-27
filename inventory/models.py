
from decimal import Decimal

from django.core.validators import MinValueValidator
from core.limits import price_validators
from django.db import models
from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.urls import reverse
from branches.models import Branch


class ProductCategory(models.Model):
    """A single flat grouping products are filed under. No hierarchy — every
    category is a peer of every other one."""

    category_name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    image = models.ImageField(
        upload_to='categories/', blank=True, null=True,
        help_text="Shown on the storefront's Shop by Category tiles. "
                   "Falls back to a tinted initial when left blank."
    )

    def __str__(self):
        return self.category_name

    class Meta:
        verbose_name_plural = "Product Categories"
        ordering = ['category_name']


class Product(models.Model):
    # --- unchanged ---
    class UnitOfMeasure(models.TextChoices):
        SACK = 'Sack', 'Sack'
        KILO = 'Kilo', 'Kilo'
        PIECE = 'Piece', 'Piece'
        # Retail units used across the catalog/UI.
        CAN = 'Can', 'Can'
        PACK = 'Pack', 'Pack'
        BOTTLE = 'Bottle', 'Bottle'
        BOX = 'Box', 'Box'

    category = models.ForeignKey(
        ProductCategory, on_delete=models.SET_NULL,
        null=True, related_name='products'
    )
    sku_code = models.CharField(max_length=50, unique=True)
    barcode = models.CharField(
        max_length=64, null=True, blank=True, unique=True,
        help_text="Manufacturer barcode (UPC/EAN/etc.) as scanned off the "
                   "physical product — distinct from sku_code, which is our "
                   "own auto-assigned internal code. Optional."
    )
    product_name = models.CharField(max_length=150)
    unit_of_measure = models.CharField(
        max_length=20, choices=UnitOfMeasure.choices,
        default=UnitOfMeasure.PIECE
    )
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=price_validators(),
        help_text="Retail price — what one unit costs below the first bulk tier. "
                  "Must be greater than zero; there is no valid product with a "
                  "free or negative retail price."
    )
    description = models.TextField(blank=True)

    # What one unit costs the store to buy -- the "latest cost price" the
    # Profitability report falls back on when a sold lot has no purchase order
    # behind it (opening stock, a customer return, a count correction). Kept
    # current automatically from each received purchase-order line
    # (inventory.costing.note_purchase_cost) and editable by hand on the
    # product form. Distinct from bulk_floor_price, which is a SELLING price
    # used as a cost proxy for inventory valuation only. Null = unknown, and
    # the report says so rather than treating the product as free.
    cost_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=price_validators(),
        help_text="Latest known cost of one unit. Used for cost of goods sold "
                  "when a sale cannot be traced to a purchase order. Leave "
                  "blank if unknown."
    )

    # --- tax / statutory-discount flags -------------------------------------
    # Three independent booleans, not one: a product can be VAT-exempt without
    # qualifying for either statutory discount (and vice versa). See
    # sales.pricing for how they are actually applied at checkout.
    is_vat_exempt = models.BooleanField(
        default=False,
        help_text="This product never carries VAT, regardless of who buys it "
                  "(e.g. agricultural goods in their original state)."
    )
    # PWD and Senior Citizen are two separate statutes (RA 10754 and RA 9994)
    # with their own exclusion lists, so a product can qualify for one and not
    # the other. Both default OFF: eligibility is a decision someone has to
    # make per product, and a wrong tick quietly under-charges every sale.
    is_pwd_eligible = models.BooleanField(
        default=False,
        help_text="This product qualifies for the statutory 20% PWD discount "
                  "(RA 10754). Off by default — tick it only for goods the "
                  "discount actually covers."
    )
    is_senior_eligible = models.BooleanField(
        default=False,
        help_text="This product qualifies for the statutory 20% Senior "
                  "Citizen discount (RA 9994). Off by default — tick it only "
                  "for goods the discount actually covers."
    )

    def __str__(self):
        return f"{self.product_name} ({self.sku_code})"

    def price_at(self, quantity):
        """Unit price for buying `quantity` of this product in one go.

        Walks the bulk tiers and takes the deepest one the quantity reaches,
        falling back to retail when it reaches none. Iterates the prefetched
        list rather than filtering, so a cart loop that has already called
        prefetch_related('price_tiers') costs no extra queries.
        """
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            return self.unit_price
        best = None
        for tier in self.price_tiers.all():  # Meta.ordering: min_quantity ascending
            if quantity >= tier.min_quantity:
                best = tier
        return best.unit_price if best else self.unit_price

    @property
    def bulk_floor_price(self):
        """The cheapest per-unit price this product can reach, retail if untiered.

        Used as the cost-basis proxy for inventory valuation and as the PO
        cost prefill — both of which used to read the old flat wholesale_price.
        """
        tiers = list(self.price_tiers.all())
        return min((t.unit_price for t in tiers), default=self.unit_price)

    @classmethod
    def next_sku_code(cls):
        """Next SKU-### in sequence, based on the highest existing numeric suffix."""
        prefix = 'SKU-'
        max_num = 0
        for code in cls.objects.filter(sku_code__startswith=prefix).values_list('sku_code', flat=True):
            suffix = code[len(prefix):]
            if suffix.isdigit():
                max_num = max(max_num, int(suffix))
        return f'{prefix}{max_num + 1:03d}'

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(unit_price__gt=0),
                name='product_unit_price_positive',
            ),
            models.CheckConstraint(
                condition=models.Q(cost_price__isnull=True) | models.Q(cost_price__gt=0),
                name='product_cost_price_positive_or_null',
            ),
        ]


class ProductPriceTier(models.Model):
    """One bulk-price break for a product: buy `min_quantity` or more, pay `unit_price`.

    This is tiered unit pricing as a grocery actually runs it — a second (and
    third, and fourth) real price per SKU, not a percentage off retail. ₱54/kg
    up to 9kg, ₱48/kg from 10kg, ₱45/kg from a full sack. The tier is a
    property of the product and the quantity bought, so it is public and
    applies to whoever hits the quantity: there is no per-account wholesale
    price and nothing for a cashier to choose at the register.

    Tiers start at 2 because a tier at quantity 1 would simply be the retail
    price under another name, and prices must fall as quantities rise, which
    the product form enforces across the whole ladder — the database can only
    police one row at a time.
    """
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='price_tiers'
    )
    min_quantity = models.PositiveIntegerField(
        help_text="Buy this many or more to get the tier price"
    )
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=price_validators(),
        help_text="Price per unit once the quantity is reached"
    )

    class Meta:
        ordering = ['min_quantity']
        constraints = [
            models.UniqueConstraint(
                fields=['product', 'min_quantity'],
                name='unique_price_tier_quantity_per_product'
            ),
            models.CheckConstraint(
                condition=models.Q(min_quantity__gte=2),
                name='price_tier_min_quantity_above_one'
            ),
            models.CheckConstraint(
                condition=models.Q(unit_price__gt=0),
                name='price_tier_price_positive'
            ),
        ]

    def __str__(self):
        return f"{self.product.product_name}: {self.min_quantity}+ @ {self.unit_price}"


class ProductImage(models.Model):
    """A single gallery image for a Product. A product can have many."""
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='images'
    )
    image = models.ImageField(upload_to='products/%Y/%m/')
    alt_text = models.CharField(
        max_length=150, blank=True,
        help_text="Short description for accessibility / when the image fails to load"
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="The main image shown in listings and as the detail hero"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Primary first, then oldest-uploaded — gives a stable gallery order.
        ordering = ['-is_primary', 'uploaded_at']

    def __str__(self):
        return f"Image for {self.product.product_name}"


class Warehouse(models.Model):
    # --- unchanged ---
    branch = models.OneToOneField(
        Branch, on_delete=models.CASCADE,
        related_name='warehouse'
    )
    name = models.CharField(max_length=100)
    location_notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.name} ({self.branch.branch_name})"


class WarehouseStockQuerySet(models.QuerySet):
    def sellable(self):
        """The WAREHOUSE-location twin of InventoryQuerySet.sellable() —
        same annotation, same reasoning (see that docstring), just keyed on
        Batch.Location.WAREHOUSE and joined through warehouse__branch_id
        instead of a direct branch_id. Used so Warehouse Stock's own
        management screens can stop counting an expired lot as available
        stock immediately, without waiting for the daily sweep to actually
        remove it from quantity_on_hand.
        """
        today = timezone.localdate()
        warehouse_batches = Batch.objects.filter(
            product_id=models.OuterRef('product_id'),
            branch_id=models.OuterRef('warehouse__branch_id'),
            location=Batch.Location.WAREHOUSE,
        )
        non_expired_sum = (
            warehouse_batches
            .filter(models.Q(expiry_date__isnull=True) | models.Q(expiry_date__gt=today))
            .values('product_id')
            .annotate(total=models.Sum('quantity'))
            .values('total')
        )
        return self.annotate(
            has_batches=models.Exists(warehouse_batches),
            available_qty=models.Case(
                models.When(
                    has_batches=True,
                    then=Coalesce(
                        models.Subquery(non_expired_sum, output_field=models.IntegerField()),
                        models.Value(0),
                    ),
                ),
                default=models.F('quantity_on_hand'),
                output_field=models.IntegerField(),
            ),
        )


class WarehouseStock(models.Model):
    # --- unchanged ---
    objects = WarehouseStockQuerySet.as_manager()

    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.CASCADE,
        related_name='stock_records'
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='warehouse_stock'
    )
    quantity_on_hand = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(default=0)
    restock_flag = models.BooleanField(default=False)
    last_updated = models.DateTimeField(auto_now=True)
    expiry_date = models.DateField(
        null=True, blank=True,
        help_text="Expiry date for the stock currently sitting in the warehouse. "
                  "Independent of Inventory.expiry_date — the same product can have "
                  "a different lot/expiry on the floor vs in the warehouse."
    )

    def save(self, *args, **kwargs):
        previous = None
        if self.pk:
            previous = WarehouseStock.objects.filter(pk=self.pk).values_list(
                'quantity_on_hand', 'reorder_level'
            ).first()

        self.restock_flag = self.quantity_on_hand <= self.reorder_level
        super().save(*args, **kwargs)

        if previous is not None:
            prev_qty, prev_reorder = previous
            was_out = prev_qty == 0
            was_low = prev_qty <= prev_reorder
            now_out = self.quantity_on_hand == 0
            now_low = self.quantity_on_hand <= self.reorder_level
            if now_out and not was_out:
                self._notify_stock_crossing(critical=True)
            elif now_low and not was_low:
                self._notify_stock_crossing(critical=False)

    def _notify_stock_crossing(self, critical):
        from django.urls import reverse

        from core.module_permissions import WAREHOUSE_STOCK
        from core.notifications import (
            EVENT_LOW_STOCK, PRIORITY_CRITICAL, PRIORITY_MEDIUM, notify_event,
        )

        branch = self.warehouse.branch
        if critical:
            title = f'Out of Stock Alert: {self.product.product_name} (Warehouse)'
            message = (
                f'{branch.branch_name} has 0 units of '
                f'{self.product.product_name} in its warehouse.'
            )
            priority = PRIORITY_CRITICAL
        else:
            title = f'Low Stock Alert: {self.product.product_name} (Warehouse)'
            message = (
                f'{branch.branch_name}: {self.quantity_on_hand} warehouse '
                f'units remain, at or below the reorder level of '
                f'{self.reorder_level}.'
            )
            priority = PRIORITY_MEDIUM

        notify_event(
            EVENT_LOW_STOCK, title, message, branch=branch,
            action_url=f'{reverse("alerts_center")}#warehouse',
            action_label='Review Warehouse Stock',
            reference_type='warehouse_stock', reference_id=self.pk,
            priority=priority, required_module=WAREHOUSE_STOCK,
        )

    def __str__(self):
        return (
            f"{self.product.product_name} | "
            f"{self.warehouse.name} | "
            f"{self.quantity_on_hand}"
        )

    class Meta:
        verbose_name_plural = "Warehouse Stock"
        unique_together = ('warehouse', 'product')


class InventoryQuerySet(models.QuerySet):
    def sellable(self):
        """Annotates every row with `available_qty` — the portion of this
        product/branch's FLOOR stock whose expiry date has NOT arrived,
        summed across its Batches.

        This is batch-aware, not a whole-row exclusion: a product with one
        expired lot and one fresh lot still shows the fresh quantity as
        buyable, rather than the product disappearing entirely just because
        SOME of its stock has expired. Used by every customer-facing and
        point-of-sale code path that checks what can actually be sold — read
        `available_qty` there, never the raw `quantity_on_hand`, which can
        briefly include expired stock until the daily sweep writes it off.

        Deliberately NOT applied to internal Floor Stock management. Those
        screens show the aggregate that the daily sweep reconciles and may
        surface a due lot during the short interval before that sweep runs.

        A row with NO batches at all falls back to its raw quantity_on_hand
        rather than reading as 0. Every real write path creates a matching
        batch now (see the Batch model's docstring), so this only matters
        for stock that predates batch-tracking or was written by something
        that bypassed it — in that case there is no expiry information to
        act on, so the safe assumption is "available", not "expired". This
        is different from a row whose batches ARE all expired, which
        correctly still nets out to 0 below.
        """
        today = timezone.localdate()
        floor_batches = Batch.objects.filter(
            product_id=models.OuterRef('product_id'),
            branch_id=models.OuterRef('branch_id'),
            location=Batch.Location.FLOOR,
        )
        non_expired_sum = (
            floor_batches
            .filter(models.Q(expiry_date__isnull=True) | models.Q(expiry_date__gt=today))
            .values('product_id')
            .annotate(total=models.Sum('quantity'))
            .values('total')
        )
        return self.annotate(
            has_batches=models.Exists(floor_batches),
            available_qty=models.Case(
                models.When(
                    has_batches=True,
                    then=Coalesce(
                        models.Subquery(non_expired_sum, output_field=models.IntegerField()),
                        models.Value(0),
                    ),
                ),
                default=models.F('quantity_on_hand'),
                output_field=models.IntegerField(),
            ),
        )


class Inventory(models.Model):
    """
    Store floor stock only.
    Stock enters here via a completed StockTransfer from the warehouse.
    """
    objects = InventoryQuerySet.as_manager()

    # --- existing fields (untouched) ----------------------------------------
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

    # --- new field ----------------------------------------------------------
    expiry_date = models.DateField(
        null=True, blank=True,
        help_text="Expiry date for the current stock on the floor. "
                  "For per-lot tracking use the Batch model instead."
    )

    def save(self, *args, **kwargs):
        # Fetched BEFORE super().save() so the Owner Notification Center
        # trigger below fires once, on the transition, not on every save of
        # a row that's already below reorder — see low_stock_alerts() in
        # inventory/alerts.py for the read-time version of this same
        # threshold, which this deliberately doesn't duplicate.
        previous = None
        if self.pk:
            previous = Inventory.objects.filter(pk=self.pk).values_list(
                'quantity_on_hand', 'reorder_level'
            ).first()

        self.restock = self.quantity_on_hand <= self.reorder_level
        super().save(*args, **kwargs)

        if previous is not None:
            prev_qty, prev_reorder = previous
            was_out = prev_qty == 0
            was_low = prev_qty <= prev_reorder
            now_out = self.quantity_on_hand == 0
            now_low = self.quantity_on_hand <= self.reorder_level
            if now_out and not was_out:
                self._notify_stock_crossing(critical=True)
            elif now_low and not was_low:
                self._notify_stock_crossing(critical=False)

    def _notify_stock_crossing(self, critical):
        from core.notifications import (
            EVENT_LOW_STOCK, PRIORITY_CRITICAL, PRIORITY_MEDIUM, notify_event,
        )

        if critical:
            title = f"Out of Stock Alert: {self.product.product_name}"
            message = (
                f"{self.branch.branch_name} has 0 units of "
                f"{self.product.product_name} on the floor."
            )
            priority = PRIORITY_CRITICAL
        else:
            title = f"Low Stock Alert: {self.product.product_name}"
            message = (
                f"{self.branch.branch_name}: {self.quantity_on_hand} units left, "
                f"at or below the reorder level of {self.reorder_level}."
            )
            priority = PRIORITY_MEDIUM

        notify_event(
            EVENT_LOW_STOCK, title, message, branch=self.branch,
            action_url=reverse('inventory_list'), action_label='View Floor Stock',
            reference_type='inventory', reference_id=self.pk,
            priority=priority,
        )

    def __str__(self):
        return f"{self.product.product_name} - {self.branch.branch_name} (Floor)"

    class Meta:
        verbose_name_plural = "Inventory (Store Floor)"
        unique_together = ('product', 'branch')


# =============================================================================
# NEW MODEL: Batch (per-lot expiry tracking)
# Answers the panel question: "How do you prevent expired products from
# being sold?" via FEFO (First Expired, First Out) logic.
# =============================================================================

class Batch(models.Model):
    """
    One received lot of a product at a specific branch, in ONE location
    (warehouse or floor) — the real, authoritative record of "how much of
    this product, at this expiry, is sitting where". Inventory.quantity_on_hand
    and WarehouseStock.quantity_on_hand remain the aggregate totals (what the
    Floor/Warehouse Stock KPI cards and reorder-level alerts key off), but
    that total is now always the sum of that product/branch/location's
    batches — every write path that changes quantity_on_hand also creates or
    consumes a matching Batch, so the two can't drift apart.

    A product/branch/location can have several batches at once, each with
    its own expiry_date — restocking with a new delivery never overwrites an
    older lot's expiry, it just adds another batch alongside it.

    FEFO rule: stock always moves out of the nearest-expiry batch first,
    whether that's a sale (floor), a write-off (floor), or a warehouse->floor
    release (warehouse) — see consume_fefo().
    """

    class Location(models.TextChoices):
        WAREHOUSE = 'Warehouse', 'Warehouse'
        FLOOR = 'Floor', 'Floor'

    class ExpiryStatus(models.TextChoices):
        FRESH = 'Fresh', 'Fresh'
        EXPIRING_SOON = 'Expiring Soon', 'Expiring Soon'   # within 30 days
        EXPIRED = 'Expired', 'Expired'
        NO_EXPIRY = 'No Expiry', 'No Expiry'

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='batches'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE,
        related_name='batches'
    )
    location = models.CharField(
        max_length=10, choices=Location.choices,
        help_text="Which stock this batch is part of — Warehouse (backroom) "
                  "or Floor (shelf). A restock moves quantity from a "
                  "Warehouse batch into a new or matching Floor batch; it "
                  "never edits the Warehouse batch's location in place."
    )
    # Link to the procurement that brought this batch in (optional but useful
    # for traceability — which supplier delivered this lot?)
    procurement = models.ForeignKey(
        'procurement.Procurement', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='batches',
        help_text="The purchase order that delivered this batch"
    )
    # A Floor batch created by moving stock off a Warehouse batch (a
    # restock) links back to it, so the floor lot's provenance — and which
    # warehouse batch it drew down — stays traceable. Null for a batch
    # received directly (procurement) or one with no known prior batch
    # (e.g. the one-time migration of pre-batch-tracking stock).
    source_batch = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='descendant_batches',
    )
    batch_number = models.CharField(
        max_length=20, unique=True, blank=True,
        help_text="Auto-generated e.g. BAT-2026-001"
    )
    quantity = models.PositiveIntegerField(
        help_text="Remaining quantity in this batch"
    )
    received_date = models.DateField(
        help_text="Date this batch was received into the warehouse/floor"
    )
    expiry_date = models.DateField(
        null=True, blank=True,
        help_text="Expiry date printed on the product lot. Blank means "
                  "not tracked (e.g. a manual stock adjustment with no "
                  "expiry entered) — FEFO treats it as expiring last, "
                  "never first, so an unknown date can't jump the queue "
                  "ahead of stock that IS known to be expiring soon."
    )
    notes = models.TextField(blank=True)
    external_lot_code = models.CharField(
        max_length=100, blank=True,
        help_text="Manufacturer/supplier lot code printed on the product. "
                  "Separate from batch_number, which is the system's own ID."
    )
    opening_import_row = models.ForeignKey(
        'OpeningStockImportRow', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='batches',
        help_text="Opening-stock row that created this lot, when applicable."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.batch_number:
            from django.utils import timezone
            year = timezone.now().year
            self.batch_number = f"BAT-{year}-{str(self.pk).zfill(3)}"
            super().save(update_fields=['batch_number'])

    @property
    def expiry_status(self) -> str:
        """
        Computed property — not stored in DB.
        Called by the serializer's SerializerMethodField.
        Thresholds: expired = today or earlier, expiring soon = within 30 days.
        """
        if self.expiry_date is None:
            return self.ExpiryStatus.NO_EXPIRY
        from django.utils import timezone
        today = timezone.localdate()
        delta = (self.expiry_date - today).days

        # An expiry date is the first day the lot may no longer be sold.
        # This matches expiry_alerts(), the opening-stock warning, and the
        # daily expiry sweep.
        if delta <= 0:
            return self.ExpiryStatus.EXPIRED
        elif delta <= 30:
            return self.ExpiryStatus.EXPIRING_SOON
        return self.ExpiryStatus.FRESH

    @property
    def days_until_expiry(self):
        if self.expiry_date is None:
            return None
        from django.utils import timezone
        today = timezone.localdate()
        return (self.expiry_date - today).days

    @classmethod
    def available_quantity(cls, product, branch, location, *, as_of_date=None):
        """Return non-expired batch quantity, or ``None`` when no batches
        exist and the caller should use the legacy aggregate-row fallback.

        Floor-sale reads use ``InventoryQuerySet.sellable()`` to annotate
        many products at once. This single-product version protects warehouse
        releases from moving an expired lot onto the floor.
        """
        as_of_date = as_of_date or timezone.localdate()
        batches = cls.objects.filter(
            product=product, branch=branch, location=location,
        )
        if not batches.exists():
            return None
        return (
            batches.filter(
                models.Q(expiry_date__isnull=True)
                | models.Q(expiry_date__gt=as_of_date),
                quantity__gt=0,
            ).aggregate(total=models.Sum('quantity'))['total'] or 0
        )

    @classmethod
    def consume_fefo(
        cls, product, branch, location, quantity, *,
        include_expired=False, as_of_date=None,
    ):
        """Deduct `quantity` units from this product/branch/location's
        batches, nearest-expiry-first — the FEFO rule described in this
        model's own docstring, actually enforced. Spans batches if the
        nearest one doesn't cover the full amount, and returns the list of
        (batch, quantity_taken) pairs actually consumed — the caller needs
        that breakdown to create the matching batch(es) on the other side of
        a warehouse->floor release, so a lot's real expiry carries over
        instead of being merged away.

        No-expiry batches sort LAST (nulls_last): an unknown date must never
        be treated as "expires soonest" just because SQL sorts NULL first by
        default, which would consume untracked stock ahead of stock that IS
        known to be close to expiring.

        Batch tracking now backs the aggregate quantity_on_hand fully (see
        the class docstring) — every caller is expected to already have a
        batch for whatever it's deducting; a shortfall here (this returning
        less than `quantity`) means the aggregate row and its batches have
        drifted out of sync somewhere and is worth surfacing, not silently
        swallowing.

        Expired lots are excluded by default, so an availability check can
        never pass on fresh stock and then accidentally consume an older,
        expired lot. Explicit disposal/count-correction paths may opt into
        expired stock with ``include_expired=True``.

        Must be called from within an existing transaction — locks each
        batch row it touches.
        """
        remaining = quantity
        consumed = []
        batches = cls.objects.select_for_update().filter(
            product=product, branch=branch, location=location, quantity__gt=0
        )
        if not include_expired:
            as_of_date = as_of_date or timezone.localdate()
            batches = batches.filter(
                models.Q(expiry_date__isnull=True)
                | models.Q(expiry_date__gt=as_of_date)
            )
        batches = batches.order_by(F('expiry_date').asc(nulls_last=True))
        for batch in batches:
            if remaining <= 0:
                break
            take = min(batch.quantity, remaining)
            batch.quantity -= take
            batch.save(update_fields=['quantity'])
            consumed.append((batch, take))
            remaining -= take
        return consumed

    def __str__(self):
        return (
            f"{self.batch_number} | "
            f"{self.product.product_name} | "
            f"{self.branch.branch_name} ({self.location}) | "
            f"Exp: {self.expiry_date or '—'} | "
            f"Qty: {self.quantity}"
        )

    class Meta:
        # FEFO: nearest expiry first, no-expiry batches last (see
        # consume_fefo's own nulls_last for why NULL must not sort first).
        ordering = [F('expiry_date').asc(nulls_last=True)]
        verbose_name_plural = "Batches"


class StockTransfer(models.Model):
    """
    Legacy persistence model shared by two explicit proxy domains:
    WarehouseFloorTransfer and StockWriteOff. Keeping this table preserves
    every existing slip number, foreign key, URL, and API identifier while
    new workflow code uses the correctly scoped proxy.

    slip_number/WS- are left alone on purpose: they identify records
    that already exist.
    """
    class Status(models.TextChoices):
        # --- unchanged ---
        PENDING = 'Pending', 'Pending'
        APPROVED = 'Approved', 'Approved'
        REJECTED = 'Rejected', 'Rejected'
        RELEASED = 'Released', 'Released'
        ACKNOWLEDGED = 'Acknowledged', 'Acknowledged'
        DISCREPANCY = 'Discrepancy', 'Discrepancy'

    class Purpose(models.TextChoices):
        # --- unchanged ---
        BRANCH_RESTOCK = 'Branch Restock', 'Branch Restock'
        DAMAGED_SPOILED = 'Damaged/Spoiled', 'Damaged/Spoiled'
        SAMPLE_DISPLAY = 'Sample/Display', 'Sample/Display'
        OTHER = 'Other', 'Other'

    # --- existing fields (untouched) ----------------------------------------
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

    # --- new fields ---------------------------------------------------------
    acknowledged_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='acknowledged_transfers',
        limit_choices_to={'role__in': ['STAFF', 'BRANCH_MANAGER']},
        help_text="Staff who confirmed receipt of the transferred stock"
    )
    flag_reason = models.TextField(
        blank=True,
        help_text="Reason for Discrepancy status — required when status=Discrepancy"
    )
    received_qty = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Actual quantity received. May differ from approved qty if discrepancy."
    )

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.slip_number:
            year = self.created_at.year
            self.slip_number = f"WS-{year}-{str(self.pk).zfill(3)}"
            super().save(update_fields=['slip_number'])

    def __str__(self):
        return (
            f"{self.slip_number} | "
            f"{self.source_branch} -> "
            f"{self.destination_branch or 'N/A'} | "
            f"{self.status}"
        )

    @property
    def is_warehouse_floor_transfer(self):
        return self.purpose == self.Purpose.BRANCH_RESTOCK

    @property
    def is_write_off(self):
        return not self.is_warehouse_floor_transfer

    @property
    def workflow_name(self):
        return 'Warehouse to Floor Transfer' if self.is_warehouse_floor_transfer else 'Stock Write-off'

    class Meta:
        ordering = ['-created_at']


class WarehouseFloorTransferManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(
            purpose=StockTransfer.Purpose.BRANCH_RESTOCK,
        )


class WarehouseFloorTransfer(StockTransfer):
    """Warehouse stock requested and moved to a destination sales floor."""

    objects = WarehouseFloorTransferManager()

    class Meta:
        proxy = True
        verbose_name = 'Warehouse-to-floor transfer'
        verbose_name_plural = 'Warehouse-to-floor transfers'


class StockWriteOffManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().exclude(
            purpose=StockTransfer.Purpose.BRANCH_RESTOCK,
        )


class StockWriteOff(StockTransfer):
    """Floor stock permanently removed for a recorded write-off reason."""

    objects = StockWriteOffManager()

    class Meta:
        proxy = True
        verbose_name = 'Stock write-off'
        verbose_name_plural = 'Stock write-offs'


class StockTransferItem(models.Model):
    # --- unchanged ---
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

    # --- release-step fields ------------------------------------------------
    # StockTransfer.received_qty is one number for the whole slip, which is
    # enough for the acknowledge step but not for the release screen: picking
    # goods off a shelf is per-item, so scanning needs a per-item counter to
    # know when each row is done.
    class ReleaseMethod(models.TextChoices):
        SCAN = 'Scan', 'Scanned'
        MANUAL = 'Manual', 'Manual'

    quantity_released = models.PositiveIntegerField(
        default=0,
        help_text="Actually picked at release time. Drives per-item release status."
    )
    # Picking progress while the slip is still Approved. Kept apart from
    # quantity_released on purpose: that column means "left the warehouse"
    # (the detail page, receipt confirmation and discrepancy flow all read it
    # as such), whereas a pick is only a count on a screen. Writing scans into
    # quantity_released would make an unreleased slip look released.
    quantity_picked = models.PositiveIntegerField(
        default=0,
        help_text="Running pick count on the release screen. Staging only — it "
                  "never moves stock; it becomes quantity_released when the "
                  "release is confirmed."
    )
    release_method = models.CharField(
        max_length=10, choices=ReleaseMethod.choices, blank=True,
        help_text="How quantity_released was captured. Recorded for audit only — "
                  "a scanned count is not treated as more authoritative than a "
                  "typed one, since not every product carries a barcode."
    )

    def __str__(self):
        return (
            f"{self.product.product_name} | "
            f"Requested: {self.quantity_requested} | "
            f"Approved: {self.quantity_approved}"
        )


class StockTransferEvent(models.Model):
    """Append-style business history for a withdrawal/request transition."""

    transfer = models.ForeignKey(
        StockTransfer, on_delete=models.CASCADE, related_name='events',
    )
    from_status = models.CharField(max_length=15, blank=True)
    to_status = models.CharField(max_length=15, choices=StockTransfer.Status.choices)
    actor = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stock_transfer_events',
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']

    def __str__(self):
        return f'{self.transfer.slip_number}: {self.from_status or "Created"} -> {self.to_status}'


class StockMovement(models.Model):
    # --- unchanged ---
    class MovementType(models.TextChoices):
        SALE = 'Sale', 'Sale (Floor Out)'
        PROCUREMENT_RECEIVED = 'Procurement Received', 'Procurement Received (Warehouse In)'
        WAREHOUSE_TO_FLOOR = 'Warehouse to Floor', 'Warehouse to Floor (Transfer Out/In)'
        FLOOR_TO_WAREHOUSE = 'Floor to Warehouse', 'Floor to Warehouse (Return)'
        MANUAL_IN = 'Manual In', 'Manual Stock In'
        MANUAL_OUT = 'Manual Out', 'Manual Stock Out'
        ADJUSTMENT = 'Adjustment', 'Stock Adjustment'
        WRITE_OFF = 'Write-off', 'Stock Write-off'
        TRANSFER_DISCREPANCY = 'Transfer Discrepancy', 'Transfer Receipt Discrepancy'
        OPENING_BALANCE = 'Opening Balance', 'Opening Balance'
        OPENING_BALANCE_REVERSAL = 'Opening Reversal', 'Opening Balance Reversal'
        EXPIRY_WRITE_OFF = 'Expiry Write-off', 'Expiry Write-off'
        # A Sellable-condition line from sales.views.transaction_refund. Distinct
        # from ADJUSTMENT (the void-reversal type): a refund is a customer handing
        # goods back, not a correction to a mistaken count.
        CUSTOMER_RETURN = 'Customer Return', 'Customer Return (Refund)'
        # Goods sent back to the supplier (procurement.SupplierReturn shipped).
        # reference_id is the SupplierReturn's pk, not a Procurement's.
        SUPPLIER_RETURN = 'Supplier Return', 'Supplier Return (Warehouse Out)'

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
        help_text="ID of the Sale, Procurement, StockTransfer, or Batch that caused this"
    )
    batch = models.ForeignKey(
        Batch, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='stock_movements',
        help_text="Specific lot affected by this movement, when applicable."
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='stock_movements'
    )
    opening_import_row = models.ForeignKey(
        'OpeningStockImportRow', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='movements',
        help_text="Opening-stock row that caused this movement, when applicable."
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
        indexes = [
            # This is the audit trail: the fastest-growing table in the
            # schema (one row per sale line, per receipt line, per transfer
            # line, per adjustment) and the ordering above sorts every single
            # read of it by created_at — with no index that is a filesort
            # over the whole table on every stock-history page.
            models.Index(fields=['-created_at'], name='stockmove_created_idx'),
            # The per-product history panels filter by one of the two stock
            # FKs and then read newest-first. Django indexes each FK on its
            # own, but neither covers the ordering that follows.
            models.Index(
                fields=['inventory', '-created_at'],
                name='stockmove_inv_created_idx',
            ),
            models.Index(
                fields=['warehouse_stock', '-created_at'],
                name='stockmove_ws_created_idx',
            ),
        ]


class ProductImportMappingTemplate(models.Model):
    """A saved "which spreadsheet column feeds which Product field" mapping,
    so a repeat Excel import doesn't need re-mapping from scratch every time.

    `mapping` keys are the mappable-field keys inventory.views recognizes
    (see _MAPPABLE_FIELDS) and values are the exact column header text from
    the sheet the mapping was built against — a later import re-maps by
    matching header text, not position, since a supplier's export can
    reorder columns between deliveries.
    """
    name = models.CharField(max_length=100)
    mapping = models.JSONField(help_text="{field_key: uploaded column header}")
    created_by = models.ForeignKey(
        'accounts.User', on_delete=models.CASCADE,
        related_name='product_import_mapping_templates',
    )
    is_shared = models.BooleanField(
        default=False,
        help_text="Visible to every Owner/Branch Manager, not just its creator.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        # Scoped per-creator, not globally: two different people are each
        # free to call their own template "Supplier A" without colliding.
        unique_together = ('created_by', 'name')
        ordering = ['name']


class OpeningStockImport(models.Model):
    """One confirmed opening-inventory spreadsheet.

    The unique committed fingerprint is the database-level idempotency guard:
    a double-click, retry, or repeat upload of the same normalized contents
    cannot credit stock twice. It remains populated after reversal so history
    cannot be silently replayed.
    """

    class Status(models.TextChoices):
        COMMITTED = 'Committed', 'Committed'
        REVERSED = 'Reversed', 'Reversed'

    original_filename = models.CharField(max_length=255)
    raw_sha256 = models.CharField(max_length=64, db_index=True)
    canonical_sha256 = models.CharField(max_length=64)
    committed_fingerprint = models.CharField(
        max_length=64, unique=True,
        help_text="Canonical normalized-file hash; retained after reversal."
    )
    as_of_date = models.DateField()
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.COMMITTED,
    )
    row_count = models.PositiveIntegerField(default=0)
    product_count = models.PositiveIntegerField(default=0)
    branch_count = models.PositiveIntegerField(default=0)
    total_quantity = models.PositiveBigIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    warnings_acknowledged = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True,
        related_name='opening_stock_imports',
    )
    confirmed_at = models.DateTimeField(auto_now_add=True)
    reversed_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reversed_opening_stock_imports',
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['-confirmed_at']
        indexes = [
            models.Index(fields=['-confirmed_at'], name='openstock_confirmed_idx'),
        ]

    def __str__(self):
        return f"Opening stock #{self.pk} - {self.original_filename}"


class OpeningStockImportRow(models.Model):
    """Immutable row-level evidence for an opening-stock import."""

    import_batch = models.ForeignKey(
        OpeningStockImport, on_delete=models.PROTECT, related_name='rows',
    )
    row_number = models.PositiveIntegerField()
    normalized_hash = models.CharField(max_length=64)
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True,
        related_name='opening_stock_rows',
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True,
        related_name='opening_stock_rows',
    )
    product_name_snapshot = models.CharField(max_length=150)
    barcode_snapshot = models.CharField(max_length=64)
    branch_name_snapshot = models.CharField(max_length=100)
    quantity = models.PositiveIntegerField()
    external_lot_code = models.CharField(max_length=100, blank=True)
    received_date = models.DateField()
    expiry_date = models.DateField(null=True, blank=True)
    previous_quantity = models.PositiveIntegerField()
    resulting_quantity = models.PositiveIntegerField()
    warnings = models.JSONField(default=list, blank=True)
    reversed_quantity = models.PositiveIntegerField(default=0)
    consumed_quantity = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['row_number']
        constraints = [
            models.UniqueConstraint(
                fields=['import_batch', 'row_number'],
                name='unique_opening_import_row_number',
            ),
        ]

    def __str__(self):
        return f"{self.import_batch_id} row {self.row_number}: {self.barcode_snapshot}"
