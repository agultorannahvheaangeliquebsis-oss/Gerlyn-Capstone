"""Operations on supplier<->product mappings that are more than one write.

Kept out of views.py because the transfer below has a collision case that is
easy to get wrong, and both the single-product and bulk paths have to handle
it identically.
"""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import F
from django.core.exceptions import ValidationError

from django.utils import timezone

from core.date_validation import validate_today_or_future

from inventory.models import Batch, Product, StockMovement, Warehouse, WarehouseStock

from .models import (
    DeliveryReceipt, DeliveryReceiptItem, DeliveryReceiptLine, Procurement, SupplierProduct, SupplierProductRequest,
    SupplierReturn, SupplierReturnItem,
)


class SupplierTransferError(Exception):
    """Raised inside transfer_supplier_products' atomic block so a partially
    applied bulk transfer rolls back rather than leaving half the selection
    moved."""


class SupplierCostError(Exception):
    """A purchase order cannot be confirmed without supplier-owned costs."""


class ExternalOrderError(Exception):
    """Externally communicated terms are incomplete or no longer applicable."""


class DeliveryError(Exception):
    """A delivery event failed validation and must not mutate stock."""


def _notify_received_delivery(procurement):
    """Keep web and API receiving side effects identical."""
    from core.notifications import (
        CATEGORY_PROCUREMENT, PRIORITY_HIGH, PRIORITY_LOW, notify,
        notify_supplier,
    )

    if procurement.status == Procurement.Status.PARTIALLY_RECEIVED:
        from core.module_permissions import DELIVERIES
        from django.urls import reverse

        notify(
            CATEGORY_PROCUREMENT, PRIORITY_HIGH,
            f'{procurement.po_number} received with a discrepancy',
            f'{procurement.po_number} from {procurement.supplier} '
            f'({procurement.branch.branch_name}) was received with delivered '
            f'quantities that differ from what was ordered.',
            branch=procurement.branch,
            action_url=reverse('procurement_detail', kwargs={'pk': procurement.pk}),
            action_label='View PO', required_module=DELIVERIES,
        )
        notify_supplier(
            procurement.supplier, PRIORITY_HIGH,
            f'{procurement.po_number} — delivery discrepancy',
            f'{procurement.branch.branch_name} recorded different quantities '
            f'from what was ordered on {procurement.po_number}. Please check '
            f'this delivery with them.',
            action_url=reverse('supplier_portal'), action_label='View order',
        )
    else:
        from django.urls import reverse

        notify_supplier(
            procurement.supplier, PRIORITY_LOW,
            f'{procurement.po_number} delivered & received',
            f'{procurement.branch.branch_name} confirmed receipt of '
            f'{procurement.po_number} in full.',
            action_url=reverse('supplier_portal'), action_label='View order',
        )


def validate_expected_delivery_date(value, *, today=None):
    """Apply the shared future-action rule to procurement delivery dates."""
    return validate_today_or_future(value, today=today)


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
    # Terms can sit agreed until the supplier confirms them. Revalidate at
    # this mutation boundary so yesterday's proposal cannot become a newly
    # active Incoming commitment today.
    validate_expected_delivery_date(procurement.proposed_delivery_date)
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


def record_external_order(
    procurement, *, actor, delivery_date, unit_costs, method, note=''
):
    """Lock terms agreed by phone/message and mark the approved PO ordered.

    This is the non-portal counterpart to supplier negotiation plus final
    confirmation. It records the same immutable cost/date snapshots, so the
    receiving and reporting paths do not need separate business rules.
    ``procurement`` must already be row-locked by the caller.
    """
    if not procurement.can_transition_to(Procurement.Status.COMMUNICATED):
        raise ExternalOrderError(
            f'{procurement.po_number} cannot be communicated while it is '
            f'{procurement.status}.'
        )
    if method not in Procurement.CommunicationMethod.values:
        raise ExternalOrderError('Choose how the purchase order was communicated.')

    try:
        validate_expected_delivery_date(delivery_date)
    except ValidationError as exc:
        raise ExternalOrderError(f'Delivery date: {exc.messages[0]}') from exc

    items = list(procurement.items.filter(is_unexpected=False))
    if not items:
        raise ExternalOrderError('The purchase order has no orderable items.')

    total = Decimal('0.00')
    for item in items:
        cost = unit_costs.get(item.pk)
        if cost is None or cost <= 0:
            product_name = item.product.product_name if item.product else 'a removed product'
            raise ExternalOrderError(f'Enter a valid unit cost for {product_name}.')
        item.proposed_unit_cost = cost
        item.unit_cost_snapshot = cost
        item.save(update_fields=['proposed_unit_cost', 'unit_cost_snapshot'])
        total += item.quantity_ordered * cost

    now = timezone.now()
    procurement.proposed_delivery_date = delivery_date
    procurement.delivery_date = delivery_date
    procurement.proposed_by = actor
    procurement.total_cost = total
    procurement.communication_method = method
    procurement.communication_note = (note or '').strip()[:300]
    procurement.communicated_by = actor
    procurement.communicated_at = now
    procurement.status = Procurement.Status.COMMUNICATED
    procurement.save(update_fields=[
        'proposed_delivery_date', 'delivery_date', 'proposed_by', 'total_cost',
        'communication_method', 'communication_note', 'communicated_by',
        'communicated_at', 'status',
    ])
    return procurement


def record_delivery(
    procurement, user, *, lines=None, receipt_date=None, delivered_by='',
    idempotency_key=None, rejection_reason='',
):
    """Record one accepted or rejected physical delivery exactly once.

    ``lines`` maps ProcurementItem ids to ``(quantity, expiry_date)``. An empty
    map is a rejected delivery and therefore requires ``rejection_reason``.
    The client token is scoped by a globally unique field so browser retries
    and API retries return the already-created event without touching stock.
    """
    lines = lines or {}
    receipt_date = receipt_date or timezone.localdate()
    delivered_by = (delivered_by or '').strip()[:100]
    rejection_reason = (rejection_reason or '').strip()
    idempotency_key = (idempotency_key or '').strip()[:64] or None

    with transaction.atomic():
        if idempotency_key:
            existing = DeliveryReceipt.objects.select_for_update().filter(
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                if existing.procurement_id != procurement.pk:
                    raise DeliveryError('That delivery submission token belongs to another purchase order.')
                return existing, existing.procurement, True

        locked = Procurement.objects.select_for_update().select_related('branch').get(
            pk=procurement.pk,
        )
        # A second request may have passed the optimistic lookup above while
        # waiting for this PO lock. Recheck with a locking/current read after
        # serialization so concurrent retries cannot collide on the unique
        # token or return a server error.
        if idempotency_key:
            existing = DeliveryReceipt.objects.select_for_update().filter(
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                if existing.procurement_id != locked.pk:
                    raise DeliveryError('That delivery submission token belongs to another purchase order.')
                return existing, locked, True
        if locked.status not in Procurement.AWAITING_DELIVERY_STATUSES:
            raise DeliveryError(f'{locked.po_number} cannot be received while it is {locked.status}.')

        if not lines:
            if not rejection_reason:
                raise DeliveryError('Enter a reason for the failed or rejected delivery.')
            receipt = DeliveryReceipt.objects.create(
                procurement=locked, received_by=user, receipt_date=receipt_date,
                delivered_by=delivered_by,
                outcome=DeliveryReceipt.Outcome.REJECTED,
                rejection_reason=rejection_reason[:300],
                idempotency_key=idempotency_key,
            )
            return receipt, locked, False

        warehouse = Warehouse.objects.filter(branch=locked.branch).first()
        if warehouse is None:
            raise DeliveryError(
                f'No warehouse is configured for {locked.branch.branch_name} — cannot receive stock.'
            )

        items = {
            item.pk: item for item in locked.items.select_related('product').all()
        }
        unknown = set(lines) - set(items)
        if unknown:
            raise DeliveryError('One or more submitted delivery lines are not on this purchase order.')

        normalized = []
        for item_id, (quantity, expiry) in lines.items():
            item = items[item_id]
            if not isinstance(quantity, int) or quantity < 0:
                raise DeliveryError(f'Enter a valid received quantity for {item.product.product_name}.')
            if quantity == 0:
                continue
            remaining = max(item.quantity_ordered - item.quantity_received, 0)
            if not item.is_unexpected and quantity > remaining:
                raise DeliveryError(
                    f'Received quantity for {item.product.product_name} ({quantity}) '
                    f'cannot exceed the remaining quantity ({remaining}).'
                )
            normalized.append((item, quantity, expiry))
        if not normalized:
            raise DeliveryError('Enter at least one received quantity greater than zero, or reject the delivery with a reason.')

        receipt = DeliveryReceipt.objects.create(
            procurement=locked, received_by=user, receipt_date=receipt_date,
            delivered_by=delivered_by, outcome=DeliveryReceipt.Outcome.ACCEPTED,
            idempotency_key=idempotency_key,
        )
        for item, quantity, expiry in normalized:
            item.quantity_received += quantity
            item.save(update_fields=['quantity_received'])
            stock, _ = WarehouseStock.objects.select_for_update().get_or_create(
                product=item.product, warehouse=warehouse,
                defaults={'quantity_on_hand': 0, 'reorder_level': 0},
            )
            before = stock.quantity_on_hand
            stock.quantity_on_hand = before + quantity
            stock.save()
            batch = Batch.objects.create(
                product=item.product, branch=locked.branch,
                location=Batch.Location.WAREHOUSE, procurement=locked,
                quantity=quantity, received_date=receipt_date, expiry_date=expiry,
            )
            DeliveryReceiptItem.objects.create(
                receipt=receipt, procurement_item=item, product=item.product,
                quantity_received=quantity, batch=batch,
            )
            DeliveryReceiptLine.objects.create(
                receipt=receipt, item=item, quantity_received=quantity,
                expiry_date=expiry,
            )
            StockMovement.objects.create(
                warehouse_stock=stock, batch=batch,
                location_type=StockMovement.LocationType.WAREHOUSE,
                movement_type=StockMovement.MovementType.PROCUREMENT_RECEIVED,
                quantity_change=quantity, quantity_before=before,
                quantity_after=stock.quantity_on_hand,
                reference_id=locked.pk, notes=f'Delivery for {locked.po_number}',
                created_by=user,
            )

        has_remaining = any(
            not item.is_unexpected and item.quantity_received < item.quantity_ordered
            for item in items.values()
        )
        has_unexpected = any(
            item.is_unexpected and item.quantity_received > 0 for item in items.values()
        )
        locked.status = (
            Procurement.Status.PARTIALLY_RECEIVED if has_remaining
            else Procurement.Status.RECEIVED
        )
        locked.receipt_flag = (
            Procurement.ReceiptFlag.PARTIAL if has_remaining
            else Procurement.ReceiptFlag.DISCREPANCY if has_unexpected
            else Procurement.ReceiptFlag.NONE
        )
        locked.actual_receipt_date = receipt_date
        locked.save(update_fields=['status', 'receipt_flag', 'actual_receipt_date'])
        _notify_received_delivery(locked)
        return receipt, locked, False


def active_supplier_product_links(supplier):
    """Return the current, orderable catalog for one active supplier.

    ``SupplierProduct`` rows are the lifecycle boundary for a catalog
    assignment: adding a row activates the product for that supplier and
    removing it takes the product out of future purchase orders without
    rewriting historical ``ProcurementItem`` rows.  A deactivated supplier
    must not remain orderable merely because its catalog rows still exist.
    """
    supplier_id = getattr(supplier, 'pk', supplier)
    return SupplierProduct.objects.approved().filter(
        supplier_id=supplier_id,
        supplier__is_active=True,
    )


def unassigned_product_ids(supplier, product_ids, *, lock=False):
    """Return requested product IDs outside ``supplier``'s active catalog.

    This is an informational catalog comparison, not a purchase-order
    eligibility rule: PO creation may include products without a supplier
    link. ``lock`` remains available to catalog-management callers that need
    a stable comparison during a write.
    """
    requested = {int(product_id) for product_id in product_ids}
    links = active_supplier_product_links(supplier)
    if lock:
        links = links.select_for_update()
    assigned = set(
        links.filter(product_id__in=requested).values_list(
            'product_id', flat=True
        )
    )
    return requested - assigned


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
            product = Product.create_with_sku(
                category=category,
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
    if new_date is None:
        raise RescheduleError('Choose a delivery date that is today or later.')
    try:
        validate_expected_delivery_date(new_date, today=today)
    except ValidationError as exc:
        raise RescheduleError(exc.messages[0]) from exc
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


# ---------------------------------------------------------------------------
# Supplier returns (P17)
# ---------------------------------------------------------------------------

class ReturnError(Exception):
    """A supplier-return step was refused; the message is user-facing."""


def _warehouse_for(procurement):
    warehouse = Warehouse.objects.filter(branch=procurement.branch).first()
    if warehouse is None:
        raise ReturnError(
            f'No warehouse is configured for {procurement.branch.branch_name}.'
        )
    return warehouse


def _po_warehouse_batches(procurement, product):
    """This order's own warehouse lots of a product, soonest expiry first.
    Expired lots are included on purpose: expired goods are a legitimate
    reason to send a delivery back."""
    return Batch.objects.select_for_update().filter(
        procurement=procurement, product=product,
        branch=procurement.branch, location=Batch.Location.WAREHOUSE,
        quantity__gt=0,
    ).order_by(F('expiry_date').asc(nulls_last=True), 'pk')


def returnable_lines(procurement):
    """One row per item on the order that could still be returned, each with
    the most units that may go back right now:

      * no more than was actually received, less what earlier returns already
        claim (Requested, Acknowledged, Shipped or Settled), and
      * no more than this order's own lots still sitting in the warehouse,
        less what pending (not yet shipped) returns already earmark.

    Goods that reached the floor are deliberately not returnable here.
    """
    active = SupplierReturn.ACTIVE_STATUSES
    pending = {SupplierReturn.Status.REQUESTED, SupplierReturn.Status.ACKNOWLEDGED}
    rows = []
    for item in procurement.items.select_related('product').order_by('pk'):
        if item.is_unexpected and item.quantity_received == 0:
            continue
        claimed = sum(
            ri.quantity_returned for ri in item.return_items.all()
            if ri.supplier_return.status in active
        )
        pending_units = sum(
            ri.quantity_returned for ri in item.return_items.all()
            if ri.supplier_return.status in pending
        )
        in_warehouse = sum(
            Batch.objects.filter(
                procurement=procurement, product=item.product,
                branch=procurement.branch, location=Batch.Location.WAREHOUSE,
                quantity__gt=0,
            ).values_list('quantity', flat=True)
        )
        rows.append({
            'item': item,
            'received': item.quantity_received,
            'claimed': claimed,
            'in_warehouse': in_warehouse,
            'max': max(min(item.quantity_received - claimed,
                           in_warehouse - pending_units), 0),
        })
    return rows


def create_supplier_return(procurement, user, *, reason, notes, quantities):
    """Raise a return request against a received order.

    quantities maps ProcurementItem pk -> units (zero/blank lines ignored).
    Locks the order row so two people can't both claim the same units.
    """
    if reason not in SupplierReturn.Reason.values:
        raise ReturnError('Choose a reason for the return.')
    if reason == SupplierReturn.Reason.OTHER and not (notes or '').strip():
        raise ReturnError('Describe the reason when you choose Other.')

    with transaction.atomic():
        locked = Procurement.objects.select_for_update().select_related(
            'branch', 'supplier'
        ).get(pk=procurement.pk)
        if locked.status not in (
            Procurement.Status.RECEIVED, Procurement.Status.PARTIALLY_RECEIVED,
        ):
            raise ReturnError('Only an order that has been received can be returned.')
        if locked.supplier is None:
            raise ReturnError('This order has no supplier to return goods to.')

        allowed = {row['item'].pk: row for row in returnable_lines(locked)}
        chosen = []
        for item_id, qty in quantities.items():
            if not qty:
                continue
            row = allowed.get(item_id)
            if row is None:
                raise ReturnError('One of the selected lines is not on this order.')
            if qty > row['max']:
                raise ReturnError(
                    f"You can return at most {row['max']} of "
                    f"{row['item'].product.product_name} right now."
                )
            chosen.append((row['item'], qty))
        if not chosen:
            raise ReturnError('Enter a quantity to return for at least one product.')

        ret = SupplierReturn.objects.create(
            procurement=locked, supplier=locked.supplier, branch=locked.branch,
            reason=reason, notes=(notes or '').strip()[:300], requested_by=user,
        )
        for item, qty in chosen:
            # Allocate returned units to the concrete receipt lines that
            # introduced them. This keeps PO/product compatibility while
            # making every new return traceable to an original delivery.
            remaining = qty
            receipt_lines = item.receipt_items.select_related('receipt').filter(
                receipt__outcome=DeliveryReceipt.Outcome.ACCEPTED,
            ).order_by('receipt__received_at', 'pk')
            for source in receipt_lines:
                already_claimed = sum(
                    row.quantity_returned
                    for row in source.supplier_return_items.select_related('supplier_return')
                    if row.supplier_return.status in SupplierReturn.ACTIVE_STATUSES
                )
                available = max(source.quantity_received - already_claimed, 0)
                take = min(available, remaining)
                if take:
                    SupplierReturnItem.objects.create(
                        supplier_return=ret, procurement_item=item,
                        product=item.product, quantity_returned=take,
                        source_receipt_item=source,
                    )
                    remaining -= take
                if remaining == 0:
                    break
            if remaining:
                # Legacy receipts predate item-level receipt rows. Preserve
                # their returnability, while new receipts always take the
                # traceable path above.
                SupplierReturnItem.objects.create(
                    supplier_return=ret, procurement_item=item,
                    product=item.product, quantity_returned=remaining,
                )
    return ret


def _locked_return(ret, *allowed_statuses, action):
    locked = SupplierReturn.objects.select_for_update().select_related(
        'procurement', 'procurement__branch', 'supplier', 'branch',
    ).get(pk=ret.pk)
    if locked.status not in allowed_statuses:
        raise ReturnError(
            f'{locked.return_number} is {locked.status}, so it can no longer be {action}.'
        )
    return locked


def respond_to_return(ret, *, acknowledge, note=''):
    """The supplier's answer to a Requested return."""
    note = (note or '').strip()
    if not acknowledge and not note:
        raise ReturnError('Tell the store why you are rejecting this return.')
    with transaction.atomic():
        locked = _locked_return(
            ret, SupplierReturn.Status.REQUESTED, action='answered',
        )
        locked.status = (
            SupplierReturn.Status.ACKNOWLEDGED if acknowledge
            else SupplierReturn.Status.REJECTED
        )
        locked.supplier_note = '' if acknowledge else note[:200]
        locked.responded_at = timezone.now()
        locked.save(update_fields=['status', 'supplier_note', 'responded_at'])
    return locked


def cancel_return(ret):
    with transaction.atomic():
        locked = _locked_return(
            ret, *SupplierReturn.CANCELLABLE_STATUSES, action='cancelled',
        )
        locked.status = SupplierReturn.Status.CANCELLED
        locked.save(update_fields=['status'])
    return locked


def ship_return(ret, user, *, note=''):
    """The goods leave: deduct them from warehouse stock, exactly once.

    Everything happens in one transaction under row locks (aggregate row
    first, then the lots — the order sales and transfers use), so a return
    can't ship twice or race a withdrawal. If any line can't be covered by
    this order's own warehouse lots the whole shipment is refused.
    """
    with transaction.atomic():
        locked = _locked_return(
            ret, SupplierReturn.Status.ACKNOWLEDGED, action='shipped',
        )
        proc = locked.procurement
        warehouse = _warehouse_for(proc)

        for line in locked.items.select_related('product'):
            stock = WarehouseStock.objects.select_for_update().filter(
                product=line.product, warehouse=warehouse,
            ).first()
            remaining = line.quantity_returned
            if stock is None or stock.quantity_on_hand < remaining:
                raise ReturnError(
                    f'Not enough {line.product.product_name} in the warehouse '
                    f'to send back {remaining}.'
                )
            for batch in _po_warehouse_batches(proc, line.product):
                if remaining <= 0:
                    break
                take = min(batch.quantity, remaining)
                before = stock.quantity_on_hand
                stock.quantity_on_hand = before - take
                stock.save()
                batch.quantity -= take
                batch.save(update_fields=['quantity'])
                StockMovement.objects.create(
                    warehouse_stock=stock, batch=batch,
                    location_type=StockMovement.LocationType.WAREHOUSE,
                    movement_type=StockMovement.MovementType.SUPPLIER_RETURN,
                    quantity_change=-take,
                    quantity_before=before, quantity_after=stock.quantity_on_hand,
                    reference_id=locked.pk,
                    notes=(
                        f'{locked.return_number}: {line.product.product_name} '
                        f'returned to supplier ({locked.reason}) from {proc.po_number}.'
                    ),
                    created_by=user,
                )
                remaining -= take
            if remaining > 0:
                raise ReturnError(
                    f"This order's lots hold fewer {line.product.product_name} "
                    f'than the {line.quantity_returned} being returned.'
                )

        locked.status = SupplierReturn.Status.SHIPPED
        locked.shipped_at = timezone.now()
        locked.shipped_by = user
        locked.shipping_note = (note or '').strip()[:200]
        locked.save(update_fields=[
            'status', 'shipped_at', 'shipped_by', 'shipping_note',
        ])
    return locked


def settle_with_credit(ret, user, *, reference, amount):
    """Close a shipped return with a supplier credit note."""
    reference = (reference or '').strip()
    if not reference:
        raise ReturnError('Enter the credit note reference.')
    try:
        amount = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        raise ReturnError('Enter the credit amount as a number.')
    if not amount.is_finite() or amount <= 0:
        raise ReturnError('The credit amount must be greater than zero.')
    with transaction.atomic():
        locked = _locked_return(
            ret, SupplierReturn.Status.SHIPPED, action='settled',
        )
        locked.status = SupplierReturn.Status.SETTLED
        locked.resolution = SupplierReturn.Resolution.CREDIT
        locked.credit_reference = reference[:60]
        locked.credit_amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        locked.settled_at = timezone.now()
        locked.settled_by = user
        locked.save(update_fields=[
            'status', 'resolution', 'credit_reference', 'credit_amount',
            'settled_at', 'settled_by',
        ])
    return locked


def receive_replacement(ret, user, *, lines, delivered_by=''):
    """Receive replacement goods for a shipped return.

    lines maps SupplierReturnItem pk -> (units, expiry_date or None). Stock
    is added only here, after the store has actually validated what arrived;
    the return settles itself once every returned unit has been replaced.
    """
    with transaction.atomic():
        locked = _locked_return(
            ret, SupplierReturn.Status.SHIPPED, action='received against',
        )
        proc = locked.procurement
        warehouse = _warehouse_for(proc)
        items = {i.pk: i for i in locked.items.select_related('product')}

        any_received = False
        for item_id, (qty, expiry) in lines.items():
            if not qty:
                continue
            item = items.get(item_id)
            if item is None:
                raise ReturnError('One of the lines is not part of this return.')
            if qty > item.replacement_outstanding:
                raise ReturnError(
                    f'Only {item.replacement_outstanding} more '
                    f'{item.product.product_name} are still owed as replacements.'
                )
            stock, _ = WarehouseStock.objects.select_for_update().get_or_create(
                product=item.product, warehouse=warehouse,
                defaults={'quantity_on_hand': 0, 'reorder_level': 0},
            )
            before = stock.quantity_on_hand
            stock.quantity_on_hand = before + qty
            stock.save()
            batch = Batch.objects.create(
                product=item.product, branch=proc.branch,
                location=Batch.Location.WAREHOUSE, procurement=proc,
                quantity=qty, received_date=timezone.localdate(),
                expiry_date=expiry,
                notes=f'Replacement for {locked.return_number}',
            )
            StockMovement.objects.create(
                warehouse_stock=stock, batch=batch,
                location_type=StockMovement.LocationType.WAREHOUSE,
                movement_type=StockMovement.MovementType.PROCUREMENT_RECEIVED,
                quantity_change=qty, quantity_before=before,
                quantity_after=stock.quantity_on_hand,
                reference_id=proc.pk,
                notes=f'Replacement received for {locked.return_number} ({proc.po_number}).',
                created_by=user,
            )
            item.quantity_replaced += qty
            item.save(update_fields=['quantity_replaced'])
            any_received = True

        if not any_received:
            raise ReturnError('Enter a received quantity for at least one product.')

        receipt = DeliveryReceipt.objects.create(
            procurement=proc, received_by=user,
            receipt_date=timezone.localdate(),
            delivered_by=(delivered_by or '').strip()[:100],
            supplier_return=locked,
        )
        # Replacement quantities are already represented by one return line;
        # create matching receipt-line evidence for the replacement event.
        for item_id, (qty, _expiry) in lines.items():
            if not qty or item_id not in items:
                continue
            return_item = items[item_id]
            batch = Batch.objects.filter(
                procurement=proc, product=return_item.product,
                notes=f'Replacement for {locked.return_number}',
            ).order_by('-pk').first()
            DeliveryReceiptItem.objects.create(
                receipt=receipt, procurement_item=return_item.procurement_item,
                product=return_item.product, quantity_received=qty, batch=batch,
            )
            DeliveryReceiptLine.objects.create(
                receipt=receipt, item=return_item.procurement_item,
                quantity_received=qty, expiry_date=_expiry,
            )
        if all(i.replacement_outstanding == 0 for i in items.values()):
            locked.status = SupplierReturn.Status.SETTLED
            locked.resolution = SupplierReturn.Resolution.REPLACEMENT
            locked.settled_at = timezone.now()
            locked.settled_by = user
            locked.save(update_fields=[
                'status', 'resolution', 'settled_at', 'settled_by',
            ])
    return locked
