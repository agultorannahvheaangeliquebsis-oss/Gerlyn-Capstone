"""Operations on supplier<->product mappings that are more than one write.

Kept out of views.py because the transfer below has a collision case that is
easy to get wrong, and both the single-product and bulk paths have to handle
it identically.
"""

from decimal import Decimal

from django.db import transaction

from .models import SupplierProduct


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
        for link in SupplierProduct.objects.filter(
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
        SupplierProduct.objects.select_for_update()
        .filter(supplier=from_supplier, product_id__in=product_ids)
        .select_related('product')
    )
    if not links:
        raise SupplierTransferError('No matching products to transfer.')

    # One query for the collisions rather than a get_or_create per row.
    already_there = set(
        SupplierProduct.objects.filter(
            supplier=to_supplier,
            product_id__in=[l.product_id for l in links],
        ).values_list('product_id', flat=True)
    )

    moved = merged = 0
    for link in links:
        if link.product_id in already_there:
            # Destination already carries it. Keep the destination's own
            # cost/SKU — those are what it actually quotes — and just drop
            # the source link so the product stops showing under the old
            # supplier, which is what "transfer" means to the user.
            link.delete()
            merged += 1
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
