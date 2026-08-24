"""Operations on supplier<->product mappings that are more than one write.

Kept out of views.py because the transfer below has a collision case that is
easy to get wrong, and both the single-product and bulk paths have to handle
it identically.
"""

from django.db import transaction

from .models import SupplierProduct


class SupplierTransferError(Exception):
    """Raised inside transfer_supplier_products' atomic block so a partially
    applied bulk transfer rolls back rather than leaving half the selection
    moved."""


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
            # supplier_sku and default_cost_price belong to the OLD supplier's
            # relationship and are meaningless under the new one — carrying
            # them over would silently pre-fill future POs with another
            # company's price.
            link.supplier_sku = ''
            link.default_cost_price = None
            link.save(update_fields=['supplier', 'supplier_sku', 'default_cost_price'])
            moved += 1

    return moved, merged
