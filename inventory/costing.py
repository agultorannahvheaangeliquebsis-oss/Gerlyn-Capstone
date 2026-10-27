"""What a unit of stock cost the store -- the one place that rule lives.

The Profitability report subtracts cost of goods sold (COGS) from sales. COGS
needs a cost for every unit sold, and the system has two honest sources:

  1. The BATCH the unit came out of. A batch received on a purchase order (or
     moved from such a batch to the shelf, or created to replace goods sent
     back to the supplier) carries ``procurement``, and the purchase order's
     line for that product carries the supplier price actually agreed
     (``ProcurementItem.unit_cost_snapshot``). A floor batch made by a
     warehouse-to-floor transfer copies ``procurement`` from its source, and
     also records ``source_batch``, so the chain is walked if one is missing.
  2. Otherwise the product's LATEST cost price (``Product.cost_price``): opening
     stock, a customer return put back on the shelf and a count correction
     have no purchase order behind them.

If neither is known the cost is None -- unknown, never zero. A line sold for
PHP 100 whose cost is unknown has no knowable profit, and calling it PHP 100 of
profit is the error the report's "incomplete data" note exists to prevent.

The answer is written onto ``SalesItem.unit_cost`` the moment the stock leaves
(sales.views._deduct_stock), so a supplier raising a price next month cannot
rewrite last month's margin.
"""
from decimal import ROUND_HALF_UP, Decimal

COST_PLACES = Decimal('0.0001')


class CostResolver:
    """Resolves unit costs, remembering purchase-order lookups so pricing a
    whole cart (or backfilling thousands of lines) asks the database about each
    purchase order line once."""

    def __init__(self):
        self._purchase_costs = {}

    def purchase_cost(self, procurement_id, product_id):
        """Agreed unit cost of `product_id` on purchase order `procurement_id`,
        or None when the PO has no priced line for it."""
        key = (procurement_id, product_id)
        if key not in self._purchase_costs:
            from procurement.models import ProcurementItem

            self._purchase_costs[key] = (
                ProcurementItem.objects
                .filter(
                    procurement_id=procurement_id, product_id=product_id,
                    unit_cost_snapshot__isnull=False,
                )
                .order_by('-pk')
                .values_list('unit_cost_snapshot', flat=True)
                .first()
            )
        return self._purchase_costs[key]

    def batch_cost(self, batch):
        """Cost of one unit in `batch`, or None if it cannot be traced to a
        priced purchase order line."""
        seen = set()
        while batch is not None and batch.pk not in seen:
            seen.add(batch.pk)
            if batch.procurement_id:
                cost = self.purchase_cost(batch.procurement_id, batch.product_id)
                if cost is not None:
                    return cost
            batch = batch.source_batch
        return None

    def line_cost(self, product, consumed, quantity):
        """Quantity-weighted unit cost for a sale line.

        `consumed` is the ``[(batch, quantity_taken), ...]`` that
        Batch.consume_fefo returned. Each batch is priced from its own purchase
        order, falling back to the product's latest cost; any quantity the
        batches did not cover (a drifted aggregate) is priced at the latest
        cost as well. Returns None when any unit cannot be priced at all.
        """
        fallback = product.cost_price if product is not None else None
        total, units = Decimal('0'), 0
        for batch, taken in consumed:
            cost = self.batch_cost(batch)
            if cost is None:
                cost = fallback
            if cost is None:
                return None
            total += cost * taken
            units += taken
        uncovered = quantity - units
        if uncovered > 0:
            if fallback is None:
                return None
            total += fallback * uncovered
            units += uncovered
        if units <= 0:
            return fallback
        return (total / units).quantize(COST_PLACES, rounding=ROUND_HALF_UP)


def sale_unit_cost(product, consumed, quantity):
    """One-off convenience over CostResolver.line_cost."""
    return CostResolver().line_cost(product, consumed, quantity)


def stamp_sale_item_cost(item, consumed, resolver=None):
    """Freeze `item.unit_cost` from the batches the sale just drew on.

    Called inside the same transaction that deducts the stock. Only the cost
    column is written, so nothing else on the line (its subtotal included) is
    touched, and the audit log -- which skips this column on purpose -- does not
    grow a row per sold line.
    """
    resolver = resolver or CostResolver()
    item.unit_cost = resolver.line_cost(item.product, consumed, item.quantity_sold)
    item.save(update_fields=['unit_cost'])
    return item.unit_cost


def note_purchase_cost(product, unit_cost):
    """Record `unit_cost` as the product's latest cost, when it is a usable
    price and different from what is stored. Called as a purchase order line
    is received, so ``Product.cost_price`` follows the supplier's real price
    without anyone retyping it."""
    if product is None or unit_cost is None or unit_cost <= 0:
        return False
    if product.cost_price == unit_cost:
        return False
    product.cost_price = unit_cost
    product.save(update_fields=['cost_price'])
    return True


def backfill_sale_costs(queryset=None, *, dry_run=False, resolver=None):
    """Fill ``unit_cost`` on sale lines that were recorded before costs were
    tracked (or whose cost was unknown at the time), from the batches each line
    actually drew on and then the product's cost price.

    Only ever writes NULLs -- a cost already frozen on a line is never touched.
    Completed orders only: an order still in the pipeline has not taken its
    stock yet (``_deduct_stock`` stamps the cost then) and a cancelled one has
    no revenue to match a cost against. Returns ``(filled, still_unknown)``.
    """
    from sales.models import SalesItem, SalesTransaction

    resolver = resolver or CostResolver()
    items = queryset if queryset is not None else SalesItem.objects.all()
    items = (
        items.filter(
            unit_cost__isnull=True, product__isnull=False,
            transaction__order_status=SalesTransaction.OrderStatus.COMPLETED,
        )
        .select_related('product')
        .prefetch_related('batch_consumptions__batch')
        .order_by('pk')
    )
    filled = unknown = 0
    for item in items.iterator(chunk_size=500):
        consumed = [
            (c.batch, c.quantity_taken) for c in item.batch_consumptions.all()
            if c.batch is not None
        ]
        cost = resolver.line_cost(item.product, consumed, item.quantity_sold)
        if cost is None:
            unknown += 1
            continue
        filled += 1
        if not dry_run:
            item.unit_cost = cost
            item.save(update_fields=['unit_cost'])
    return filled, unknown
