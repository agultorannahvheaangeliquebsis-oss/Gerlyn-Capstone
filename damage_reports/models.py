"""Damaged Product Reports: a report of damaged goods, with photo evidence,
that a manager reviews before any stock or money moves.

Two kinds of report share one table:

  * STAFF   - found in the branch's own stock (on the floor or in the
              warehouse): damaged on arrival, damaged in storage, expired or
              spoiled. The units are still counted as on hand, so approving the
              report is what takes them out of stock.
  * CUSTOMER - raised by a customer from a received order, inside the same
              window refunds use. The goods are already out of the building and
              already left stock when they were sold, so approving never touches
              stock: it hands the customer's money back through the existing
              refund workflow (or issues a replacement).

Nothing here moves stock or money by itself. `damage_reports.services` is the
only writer and takes the rows it changes under a lock, so a report can be
decided once and its units can leave stock once.

Photos are evidence. They live on private storage (never /media/) and are
streamed through authorization-checked views, and nothing in the app deletes
one - a Rejected report keeps its photos for audit.
"""
import uuid
from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.limits import MAX_QUANTITY, quantity_validators
from core.storage import PrivateMediaStorage

damage_photo_storage = PrivateMediaStorage()


def photo_upload_path(instance, filename):
    """damage_reports/<yyyy>/<mm>/<random>.<jpg|png>.

    The uploader's own file name is never kept: it is arbitrary text from the
    request. services.validate_photos has already renamed the upload to what
    Pillow found; anything else (an admin upload) is stored as .bin, which the
    photo view will not serve as an image.
    """
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if extension not in ('jpg', 'png'):
        extension = 'bin'
    return f'damage_reports/{timezone.localdate():%Y/%m}/{uuid.uuid4().hex}.{extension}'


class DamageReportQuerySet(models.QuerySet):
    def holding_stock(self):
        """Reports that already lay claim to units still sitting in stock.

        An open report holds its quantity so the same units cannot be reported
        twice. An Approved "return to supplier" report holds it too: nothing is
        deducted at approval for that resolution (the supplier return takes
        the units out when it ships), so the units are still on the shelf and
        already spoken for. Approved write-offs hold nothing - the units are
        gone from the on-hand figure they would be subtracted from.
        """
        return self.filter(
            Q(status__in=DamageReport.OPEN_STATUSES)
            | Q(
                status=DamageReport.Status.APPROVED,
                resolution=DamageReport.Resolution.RETURN_TO_SUPPLIER,
            )
        )


class DamageReport(models.Model):
    class ReporterType(models.TextChoices):
        STAFF = 'staff', 'Staff'
        CUSTOMER = 'customer', 'Customer'

    class DamageType(models.TextChoices):
        ARRIVAL = 'arrival', 'Damaged on arrival'
        STORAGE = 'storage', 'Damaged in storage'
        EXPIRED = 'expired', 'Expired'
        SPOILED = 'spoiled', 'Spoiled'
        CUSTOMER = 'customer', 'Customer-reported'

    class Status(models.TextChoices):
        PENDING = 'Pending', 'Pending'
        UNDER_REVIEW = 'Under Review', 'Under Review'
        APPROVED = 'Approved', 'Approved'
        REJECTED = 'Rejected', 'Rejected'
        RESOLVED = 'Resolved', 'Resolved'

    class Resolution(models.TextChoices):
        NONE = 'none', 'No further action'
        REFUND = 'refund', 'Refund'
        REPLACEMENT = 'replacement', 'Replacement'
        WRITE_OFF = 'write_off', 'Write-off'
        RETURN_TO_SUPPLIER = 'return_to_supplier', 'Return to supplier'

    class StockLocation(models.TextChoices):
        # Same values as inventory.Batch.Location, so a report's location can
        # be handed straight to the batch helpers.
        FLOOR = 'Floor', 'Floor'
        WAREHOUSE = 'Warehouse', 'Warehouse'

    # Waiting for a decision. Both statuses hold the reported units.
    OPEN_STATUSES = (Status.PENDING, Status.UNDER_REVIEW)
    # A decision (and any stock/refund effect) has been made.
    DECIDED_STATUSES = (Status.APPROVED, Status.RESOLVED)
    # The damage types a staff member can report against their own stock.
    STAFF_DAMAGE_TYPES = (
        DamageType.ARRIVAL, DamageType.STORAGE, DamageType.EXPIRED, DamageType.SPOILED,
    )

    objects = DamageReportQuerySet.as_manager()

    report_number = models.CharField(
        max_length=20, unique=True, null=True, blank=True, editable=False,
        help_text='DR-YYYY-NNN, issued when the report is created. NULL until then '
                  '(NULLs do not collide under UNIQUE).',
    )
    branch = models.ForeignKey(
        'branches.Branch', on_delete=models.SET_NULL, null=True,
        related_name='damage_reports',
    )
    reported_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='damage_reports_filed',
    )
    reporter_type = models.CharField(max_length=10, choices=ReporterType.choices)

    # SET_NULL + a name snapshot: deleting a product from the catalog must not
    # delete the evidence, and the report still has to say what it was about.
    product = models.ForeignKey(
        'inventory.Product', on_delete=models.SET_NULL, null=True,
        related_name='damage_reports',
    )
    product_name = models.CharField(max_length=150, blank=True)
    inventory_batch = models.ForeignKey(
        'inventory.Batch', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='damage_reports',
        help_text='The lot the damaged units came from, when the reporter knew. '
                  'Blank means "oldest expiry first" at approval.',
    )
    stock_location = models.CharField(
        max_length=10, choices=StockLocation.choices, blank=True,
        help_text='Which stock the units are in. Blank for a customer report: '
                  'those goods are no longer in the store.',
    )
    sales_transaction = models.ForeignKey(
        'sales.SalesTransaction', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='damage_reports',
    )
    sales_item = models.ForeignKey(
        'sales.SalesItem', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='damage_reports',
        help_text='The order line a customer report is about; a refund is raised '
                  'against this line.',
    )
    procurement = models.ForeignKey(
        'procurement.Procurement', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='damage_reports',
        help_text='The purchase order the goods arrived on. Needed to flag them '
                  'for return to the supplier.',
    )

    quantity = models.PositiveIntegerField(
        validators=quantity_validators(minimum=1, maximum=MAX_QUANTITY),
    )
    damage_type = models.CharField(max_length=10, choices=DamageType.choices)
    description = models.TextField()
    # What one unit is worth for the Inventory Report's "estimated loss":
    # frozen when the report is filed, on the same cost basis the report values
    # stock with (Product.bulk_floor_price), so a later price change cannot
    # rewrite what a past report cost.
    estimated_unit_cost = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
    )

    status = models.CharField(
        max_length=14, choices=Status.choices, default=Status.PENDING,
    )
    reviewed_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='damage_reports_reviewed',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(
        max_length=20, choices=Resolution.choices, default=Resolution.NONE,
    )
    resolution_notes = models.TextField(blank=True)

    # --- what approval did ---------------------------------------------------
    # Each of these is written once, inside the approval transaction, and is
    # the second line of defence behind the status check against a double
    # deduction: a unique link means a report cannot have two slips or two
    # refunds, and stock_deducted_at says the units already left.
    withdrawal_slip = models.OneToOneField(
        'inventory.StockTransfer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='damage_report',
        help_text='The withdrawal slip generated when stock was deducted.',
    )
    stock_deducted_at = models.DateTimeField(null=True, blank=True)
    refund = models.OneToOneField(
        'sales.Refund', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='damage_report',
        help_text='The refund raised through the refund workflow.',
    )
    flagged_for_supplier_return = models.BooleanField(
        default=False,
        help_text='The goods are waiting to go back to the supplier on the linked '
                  'purchase order. The supplier return (not this report) takes '
                  'them out of warehouse stock when it ships.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name='damagereport_quantity_positive',
            ),
        ]
        indexes = [
            models.Index(fields=['branch', 'status'], name='damagerep_branch_status_idx'),
            models.Index(fields=['status', '-created_at'], name='damagerep_status_created_idx'),
        ]

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if self.product_id and not self.product_name:
            self.product_name = self.product.product_name
        super().save(*args, **kwargs)
        if is_new and not self.report_number:
            self.report_number = f'DR-{timezone.localtime(self.created_at).year}-{str(self.pk).zfill(3)}'
            super().save(update_fields=['report_number'])

    def __str__(self):
        return f'{self.report_number or f"Report #{self.pk}"} - {self.status}'

    @property
    def product_label(self):
        return self.product.product_name if self.product_id else (
            self.product_name or 'a product since removed from the catalog'
        )

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_customer_report(self):
        return self.reporter_type == self.ReporterType.CUSTOMER

    @property
    def loss_value(self):
        return (self.estimated_unit_cost or Decimal('0')) * self.quantity


class DamageReportPhoto(models.Model):
    """One piece of photo evidence. Never deleted by the application."""

    report = models.ForeignKey(
        DamageReport, on_delete=models.CASCADE, related_name='photos',
    )
    image = models.ImageField(
        upload_to=photo_upload_path, storage=damage_photo_storage, max_length=255,
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at', 'pk']

    def __str__(self):
        return f'Photo {self.pk} for {self.report}'

    @property
    def content_type(self):
        name = self.image.name.lower()
        if name.endswith('.png'):
            return 'image/png'
        if name.endswith(('.jpg', '.jpeg')):
            return 'image/jpeg'
        return 'application/octet-stream'


class DamageReportEvent(models.Model):
    """Append-only history of a report: filed, put under review, decided,
    resolved. What the detail page's timeline reads. Written by
    damage_reports.services only; the field-level changes are in the Audit Log.
    """

    report = models.ForeignKey(
        DamageReport, on_delete=models.CASCADE, related_name='events',
    )
    from_status = models.CharField(max_length=14, blank=True)
    to_status = models.CharField(max_length=14, choices=DamageReport.Status.choices)
    actor = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='damage_report_events',
    )
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']

    def __str__(self):
        return f'{self.report_id}: {self.from_status or "-"} -> {self.to_status}'
