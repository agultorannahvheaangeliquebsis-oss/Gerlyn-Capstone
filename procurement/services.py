"""Operations on supplier<->product mappings that are more than one write.

Kept out of views.py because the transfer below has a collision case that is
easy to get wrong, and both the single-product and bulk paths have to handle
it identically.
"""

import datetime as dt
from decimal import Decimal

from django.db import transaction

from django.utils import timezone

from inventory.models import Product

from .models import Procurement, SupplierProduct, SupplierProductRequest


class SupplierTransferError(Exception):
    """Raised inside transfer_supplier_products' atomic block so a partially
    applied bulk transfer rolls back rather than leaving half the selection
    moved."""


class SupplierCostError(Exception):
    """A purchase order cannot be confirmed without supplier-owned costs."""


def snapshot_supplier_costs(procurement):
    """Freeze this supplier's current prices onto a PO and return its total."""
    items = list(
        procurement.items.select_related('product').filter(is_unexpected=False)
    )
    product_ids = [item.product_id for item in items if item.product_id]
    links = {
        link.product_id: link
        for link in SupplierProduct.objects.approved().filter(
            supplier=procurement.supplier,
            product_id__in=product_ids,
        )
    }

    unavailable = []
    for item in items:
        link = links.get(item.product_id)
        if item.product is None or link is None or link.current_cost_price is None:
            unavailable.append(
                item.product.product_name if item.product else 'Deleted product'
            )

    if unavailable:
        names = ', '.join(sorted(set(unavailable)))
        raise SupplierCostError(
            f'The supplier must set a current cost for: {names}.'
        )

    total = Decimal('0.00')
    for item in items:
        item.unit_cost_snapshot = links[item.product_id].current_cost_price
        item.save(update_fields=['unit_cost_snapshot'])
        total += item.quantity_ordered * item.unit_cost_snapshot

    procurement.total_cost = total
    procurement.save(update_fields=['total_cost'])
    return total


def apply_agreed_terms(procurement):
    """Lock in the negotiated price/date once the supplier gives their final
    confirmation (Terms Agreed -> Incoming) — copies the currently-proposed
    date and per-item costs onto the fields the rest of the app (receiving,
    the printable PO, reports) actually reads, and recomputes total_cost
    from them. Unlike snapshot_supplier_costs (the old, now-unused-at-
    creation path pulling from the supplier's standing catalog price), this
    pulls from what was actually negotiated on THIS order.
    """
    procurement.delivery_date = procurement.proposed_delivery_date

    total = Decimal('0.00')
    items = list(procurement.items.filter(is_unexpected=False))
    for item in items:
        item.unit_cost_snapshot = item.proposed_unit_cost
        item.save(update_fields=['unit_cost_snapshot'])
        total += item.quantity_ordered * (item.unit_cost_snapshot or 0)

    procurement.total_cost = total
    procurement.save(update_fields=['delivery_date', 'total_cost'])
    return total


@transaction.atomic
def transfer_supplier_products(product_ids, from_supplier, to_supplier):
    """Move products from one supplier's catalog to another's.

    This is a metadata change and nothing more. Historical Procurement and
    ProcurementItem rows are deliberately left alone: they record what was
    actually ordered from whom at the time, and rewriting them to match a
    later supplier change would falsify the order history. What moves is only
    which supplier is the current source for future orders.

    Returns (moved, merged) counts. `merged` covers products the destination
    supplier already carried — those links are folded together rather than
    raising on the (supplier, product) unique constraint, since "both
    suppliers carry it and you asked to consolidate on one" is a normal thing
    to ask for, not an error.
    """
    if from_supplier.pk == to_supplier.pk:
        raise SupplierTransferError(
            'The source and destination supplier are the same.'
        )

    links = list(
        SupplierProduct.objects.approved().select_for_update()
        .filter(supplier=from_supplier, product_id__in=product_ids)
        .select_related('product')
    )
    if not links:
        raise SupplierTransferError('No matching products to transfer.')

    # One query for the collisions rather than a get_or_create per row.
    # Any status counts as "already there": the (supplier, product) pair is
    # unique, so a destination row that is only Proposed/Rejected would
    # otherwise collide with the move.
    already_there = {
        link.product_id: link
        for link in SupplierProduct.objects.filter(
            supplier=to_supplier,
            product_id__in=[l.product_id for l in links],
        )
    }

    moved = merged = 0
    for link in links:
        if link.product_id in already_there:
            # Destination already carries it. Keep the destination's own
            # cost/SKU — those are what it actually quotes — and just drop
            # the source link so the product stops showing under the old
            # supplier, which is what "transfer" means to the user.
            link.delete()
            merged += 1
            dest = already_there[link.product_id]
            if dest.status != SupplierProduct.Status.APPROVED:
                # The Owner is moving an approved product here on purpose —
                # that is the approval the destination's own offer lacked.
                dest.status = SupplierProduct.Status.APPROVED
                dest.save(update_fields=['status'])
        else:
            link.supplier = to_supplier
            # supplier_sku and current_cost_price belong to the OLD supplier's
            # relationship and are meaningless under the new one — carrying
            # them over would silently pre-fill future POs with another
            # company's price.
            link.supplier_sku = ''
            link.current_cost_price = None
            link.save(update_fields=['supplier', 'supplier_sku', 'current_cost_price'])
            moved += 1

    return moved, merged


class SupplierRemovalBlocked(Exception):
    """A product can't leave a supplier's catalog while a live order needs it."""

    def __init__(self, po_numbers):
        self.po_numbers = po_numbers
        super().__init__(
            'It is on ' + ', '.join(po_numbers) + '. Finish or cancel '
            f'{"that order" if len(po_numbers) == 1 else "those orders"} first.'
        )


def open_po_numbers(supplier, product):
    """PO numbers of this supplier's committed, still-open orders (Approved
    through Partially Received) that list the product. Drafts are excluded —
    they commit nothing — and so are Received/Cancelled ones, whose price
    snapshots are already frozen on the items."""
    return list(
        Procurement.objects.filter(
            supplier=supplier,
            status__in=Procurement.COMMITTED_OPEN_STATUSES,
            items__product=product,
            items__is_unexpected=False,
        ).order_by('pk').values_list('po_number', flat=True).distinct()
    )


def remove_supplier_product(link):
    """Delete one SupplierProduct link, refusing while an open PO needs it.

    Approved links are the ones POs are raised against, so only they can be
    blocked; withdrawing a Proposed/Rejected offer never affects an order.
    Cost snapshots are read from the link when terms are confirmed, so
    deleting it mid-negotiation would break that order.
    """
    with transaction.atomic():
        link = SupplierProduct.objects.select_for_update().get(pk=link.pk)
        if link.status == SupplierProduct.Status.APPROVED:
            blocking = open_po_numbers(link.supplier, link.product)
            if blocking:
                raise SupplierRemovalBlocked(blocking)
        link.delete()


class OfferReviewError(Exception):
    """The offer/request was already decided, or the input is unusable."""


def review_offer(link, reviewer, *, approve, note=''):
    """Approve or reject a supplier's Proposed offer on a master-list product."""
    with transaction.atomic():
        link = SupplierProduct.objects.select_for_update().get(pk=link.pk)
        if link.status != SupplierProduct.Status.PROPOSED:
            raise OfferReviewError('This offer was already decided.')
        link.status = (
            SupplierProduct.Status.APPROVED if approve
            else SupplierProduct.Status.REJECTED
        )
        link.reviewed_by = reviewer
        link.reviewed_at = timezone.now()
        link.review_note = '' if approve else note[:200]
        link.save(update_fields=[
            'status', 'reviewed_by', 'reviewed_at', 'review_note',
        ])
    return link


def review_product_request(req, reviewer, *, approve, note='',
                           unit_price=None, category=None):
    """Decide a supplier's request for a product not in the master list.

    Approving creates the Product (Owner-chosen retail price and category,
    supplier-proposed name and unit) and an Approved SupplierProduct at the
    supplier's proposed cost, in one transaction. Returns the request.
    """
    with transaction.atomic():
        req = SupplierProductRequest.objects.select_for_update().get(pk=req.pk)
        if req.status != SupplierProductRequest.Status.PENDING:
            raise OfferReviewError('This request was already decided.')

        if approve:
            if unit_price is None or unit_price <= 0:
                raise OfferReviewError('Enter a retail price greater than zero.')
            product = Product.objects.create(
                category=category,
                sku_code=Product.next_sku_code(),
                product_name=req.product_name,
                unit_of_measure=req.unit_of_measure,
                unit_price=unit_price,
                description=req.note,
            )
            SupplierProduct.objects.create(
                supplier=req.supplier, product=product,
                current_cost_price=req.proposed_cost,
                lead_time_days=req.lead_time_days,
                status=SupplierProduct.Status.APPROVED,
                reviewed_by=reviewer, reviewed_at=timezone.now(),
            )
            req.created_product = product
            req.status = SupplierProductRequest.Status.APPROVED
        else:
            req.status = SupplierProductRequest.Status.REJECTED
            req.review_note = note[:200]
        req.reviewed_by = reviewer
        req.reviewed_at = timezone.now()
        req.save()
    return req


class FundingError(Exception):
    """The funding source is missing, unknown, or locked."""


def clean_funding(source, note):
    """Validate a (funding_source, funding_note) pair; returns the cleaned
    pair. Other needs a note saying what it is; a note on any other source is
    kept as-is (e.g. which bank account)."""
    source = (source or '').strip()
    note = (note or '').strip()
    if source not in Procurement.FundingSource.values:
        raise FundingError('Choose where the money for this order comes from.')
    if len(note) > 200:
        raise FundingError('The funding note is too long (200 characters max).')
    if source == Procurement.FundingSource.OTHER and not note:
        raise FundingError('Describe the funding source when you choose Other.')
    return source, note


def set_funding(procurement, source, note):
    """Change a purchase order's funding source while its terms are still open.
    Locks the row so a concurrent status change can't slip past the guard."""
    source, note = clean_funding(source, note)
    with transaction.atomic():
        locked = Procurement.objects.select_for_update().get(pk=procurement.pk)
        if locked.funding_locked:
            raise FundingError(
                f'The funding source can no longer be changed — this order is '
                f'{locked.status}.'
            )
        locked.funding_source = source
        locked.funding_note = note
        locked.save(update_fields=['funding_source', 'funding_note'])
    return locked


# ---------------------------------------------------------------------------
# Overdue deliveries (P18)
# ---------------------------------------------------------------------------

class OverdueResult:
    def __init__(self, flagged=0, escalated=0):
        self.flagged = flagged
        self.escalated = escalated


def overdue_orders(branch_ids=None, today=None):
    """Incoming / Partially Received orders past their delivery date, worst
    first. branch_ids=None means every branch (Owner); a list scopes it."""
    today = today or timezone.localdate()
    qs = Procurement.objects.filter(
        status__in=Procurement.AWAITING_DELIVERY_STATUSES,
        delivery_date__lt=today,
    ).select_related('branch', 'supplier', 'supplier__supplier_profile')
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)
    return qs.order_by('delivery_date', 'pk')


def process_overdue_deliveries(today=None):
    """The daily sweep: stamp and notify each stage exactly once.

    Idempotent — running it twice in a day (or every hour) never repeats a
    notification, because each stage is recorded on the order under a row lock
    before it is sent. Never changes an order's status.
    """
    from django.conf import settings

    from .notifications import notify_po_overdue, notify_po_overdue_escalated

    today = today or timezone.localdate()
    grace = settings.PO_OVERDUE_GRACE_DAYS
    escalate = max(settings.PO_OVERDUE_ESCALATE_DAYS, grace)
    result = OverdueResult()

    candidate_ids = list(
        overdue_orders(today=today)
        .filter(delivery_date__lte=today - dt.timedelta(days=grace))
        .values_list('pk', flat=True)
    )
    for pk in candidate_ids:
        with transaction.atomic():
            proc = (
                Procurement.objects.select_for_update()
                .select_related('branch', 'supplier').get(pk=pk)
            )
            days_late = proc.days_overdue(today)
            # Re-check under the lock: it may have been received, cancelled or
            # rescheduled since the id list was read.
            if days_late < grace:
                continue
            now = timezone.now()
            send_first = proc.overdue_flagged_at is None
            send_second = (
                days_late >= escalate and proc.overdue_escalated_at is None
            )
            if not send_first and not send_second:
                continue
            fields = []
            if send_first:
                proc.overdue_flagged_at = now
                fields.append('overdue_flagged_at')
            if send_second:
                proc.overdue_escalated_at = now
                fields.append('overdue_escalated_at')
            proc.save(update_fields=fields)
        if send_first:
            notify_po_overdue(proc, days_late)
            result.flagged += 1
        if send_second:
            notify_po_overdue_escalated(proc, days_late)
            result.escalated += 1
    return result


class RescheduleError(Exception):
    """The delivery date can't be changed (wrong status, or a past date)."""


def reschedule_delivery(procurement, new_date, today=None):
    """Move an awaiting-delivery order's delivery date and restart its overdue
    tracking. Returns the locked, updated order."""
    today = today or timezone.localdate()
    if new_date is None or new_date < today:
        raise RescheduleError('Choose a delivery date that is today or later.')
    with transaction.atomic():
        locked = Procurement.objects.select_for_update().get(pk=procurement.pk)
        if locked.status not in Procurement.AWAITING_DELIVERY_STATUSES:
            raise RescheduleError(
                f'A {locked.status} order has no pending delivery to reschedule.'
            )
        locked.delivery_date = new_date
        locked.overdue_flagged_at = None
        locked.overdue_escalated_at = None
        locked.save(update_fields=[
            'delivery_date', 'overdue_flagged_at', 'overdue_escalated_at',
        ])
    return locked
