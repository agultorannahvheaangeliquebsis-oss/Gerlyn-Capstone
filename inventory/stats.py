"""
Storefront counters read off real transaction and stock rows.

The marketplace layout the shop is modelled on prints a social-proof number
under every product ("Monthly Sales 308K+", "10K+ sold"). There is no
promotions or ratings model in this system to fake one from, so the number
shown is the plain sum of SalesItem.quantity_sold, and products with no
sales history simply show nothing rather than a zero or an invented figure.

Why the two counts are fetched in separate queries and merged in Python:
annotating Sum('sales_items__quantity_sold') and Sum('inventory__
quantity_on_hand') on ONE queryset joins both multi-valued relations at
once, and every sales row is then multiplied by every inventory row for
that product (and vice versa). Both figures come back inflated, silently
and plausibly — a product in 3 branches would report triple its true sales.
"""
from django.db.models import Sum

from inventory.models import Inventory
from sales.models import SalesItem


def units_sold_by_product(product_ids=None):
    """{product_id: units sold}. Products never sold are absent from the map."""
    rows = SalesItem.objects.values('product_id').annotate(n=Sum('quantity_sold'))
    if product_ids is not None:
        rows = rows.filter(product_id__in=product_ids)
    return {r['product_id']: r['n'] for r in rows if r['product_id'] and r['n']}


def on_hand_by_product(product_ids=None, branch_id=None):
    """
    {product_id: units on hand}, summed across branches unless one is given.

    Used where the page is not branch-scoped (the home page), so the stock
    line can still be truthful: "in stock somewhere in the store" is a real
    claim, and picking a branch narrows it on the catalog.

    sellable()'s available_qty nets out anything past its expiry_date — the
    storefront must never count or offer expired stock as available.
    """
    rows = Inventory.objects.sellable().values('product_id')
    if branch_id:
        rows = rows.filter(branch_id=branch_id)
    if product_ids is not None:
        rows = rows.filter(product_id__in=product_ids)
    rows = rows.annotate(n=Sum('available_qty'))
    return {r['product_id']: r['n'] or 0 for r in rows if r['product_id']}


def in_stock_product_ids(branch_id=None):
    """Ids of products with at least one unit on a shelf.

    The storefront hides anything at zero, so this is the gate every listing
    passes through — catalog, home grid, best sellers, related products and
    the header's search suggestions — and having it in one place is what
    stops those five from drifting into five different definitions of
    "available".

    Scoped to a branch when one is chosen, summed across all branches when
    not, matching on_hand_by_product. A product with no Inventory row at all
    is absent from the map and therefore excluded, which is correct: nothing
    is on a shelf for it anywhere.

    Returns a set for O(1) `__in` membership on the caller side; the query
    itself is one grouped scan of Inventory, not one per product.
    """
    return {
        pid for pid, qty in on_hand_by_product(branch_id=branch_id).items() if qty > 0
    }


def attach_storefront_counts(products, branch_id=None):
    """
    Hang `units_sold` and `on_hand` on each product in `products`.

    Takes a list (not a queryset) because the attributes are set on the
    instances — re-evaluating a queryset afterwards would throw them away.
    """
    ids = [p.id for p in products]
    if not ids:
        return products

    sold = units_sold_by_product(ids)
    on_hand = on_hand_by_product(ids, branch_id=branch_id)
    for product in products:
        product.units_sold = sold.get(product.id, 0)
        product.on_hand = on_hand.get(product.id, 0)
    return products


def best_sellers(limit=8):
    """
    Products ranked by units actually sold, best first.

    Only products with a real sales history qualify — `filter(units_sold__gt
    =0)` is what keeps the rail from padding itself out with never-sold stock
    when the store is new. The caller must be ready for an empty list.
    """
    from inventory.models import Product

    # Sold AND still on a shelf. A best-seller rail is a buy-this rail, so a
    # product that sold well and then ran out belongs off it rather than at
    # the top of it — the storefront hides out-of-stock everywhere else too.
    ranked = list(
        Product.objects.select_related('category')
        .prefetch_related('images')
        .annotate(units_sold=Sum('sales_items__quantity_sold'))
        .filter(units_sold__gt=0, id__in=in_stock_product_ids())
        .order_by('-units_sold')[:limit]
    )
    # on_hand only — units_sold is already annotated above, and letting
    # attach_storefront_counts overwrite it would cost a query for nothing.
    on_hand = on_hand_by_product([p.id for p in ranked])
    for product in ranked:
        product.on_hand = on_hand.get(product.id, 0)
    return ranked
