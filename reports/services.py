"""
reports/services.py — the shared reporting query layer.

reports/views.py (server-rendered templates) and reports/api_views.py (the
same-origin DRF API) used to each compute their own version of "revenue",
"low stock" and "procurement cost" — which is how they drifted apart (the
API summed every order_status into revenue, read the potentially-stale
Inventory.restock flag for low stock, and included Cancelled purchase
orders in procurement cost, while the template views had already fixed
some but not all of the same bugs). Both now read the definitions below
instead of recomputing them, so the two surfaces cannot disagree with each
other again. See docs/METRICS.md for the prose definitions of each figure.

Five figures, defined once:

  Gross Sales      Sum of total_amount over Completed orders only — a
                    Pending/Approved/Preparing order has not been paid for
                    or fulfilled yet, and a Cancelled order is void.
  Refund Value      Sum of Refund.total_amount for refunds issued against
                    orders visible in the caller's scope. A refund never
                    changes SalesTransaction.order_status (see
                    sales.models.Refund), so a refunded order is still
                    "Completed" and still counted in Gross Sales — Refund
                    Value is what must be subtracted back out.
  Net Sales         Gross Sales minus Refund Value — realised revenue after
                    money actually handed back to customers.
  Pipeline Value    Sum of total_amount over committed-but-unfulfilled
                    orders (Pending/Approved/Preparing). Never added into
                    Gross/Net Sales — it is money not yet earned.
  Procurement Cost  Sum of total_cost over every purchase order except
                    Cancelled (which never happened). Matches the
                    "committed value" convention already used elsewhere
                    (Procurement.OPEN_STATUSES, the Pending POs count,
                    procurement_report's active_value) rather than
                    inventing a stricter definition.

All money aggregates run as a single Decimal SQL SUM() (via
django.db.models.functions.Coalesce) rather than a Python-side loop, so an
empty queryset returns Decimal('0.00') from the database and the work stays
server-side.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce

from accounts.models import User
from branches.models import Branch
from core.scoping import branch_for_user, scope_queryset
from core.timebounds import local_range_bounds
from inventory.alerts import low_stock_filter
from inventory.models import Inventory, WarehouseStock
from procurement.models import Procurement, SupplierReturn
from sales.models import Refund, SalesTransaction

MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)


def _decimal_sum(qs, field):
    """Decimal-safe SQL SUM(): Coalesce so an empty/no-match queryset
    returns Decimal('0.00') from the database instead of None, and the
    addition happens in SQL rather than in a Python loop."""
    return qs.aggregate(
        total=Coalesce(Sum(field), Decimal('0.00'), output_field=MONEY_FIELD)
    )['total']


# --- Sales: what counts as revenue, one definition, every surface ---------

REVENUE_ORDER_STATUSES = [SalesTransaction.OrderStatus.COMPLETED]

# Committed but unfulfilled — an order in the pipeline for exactly as long
# as it stays in one of these. Also the Dashboard's Open Orders panel's
# definition of "open" (see reports/views.py::OPEN_ORDER_STATUSES, which
# aliases this list so the two never drift apart).
PIPELINE_ORDER_STATUSES = [
    SalesTransaction.OrderStatus.PENDING,
    SalesTransaction.OrderStatus.APPROVED,
    SalesTransaction.OrderStatus.PREPARING,
]

# Every status except Cancelled — a cancelled PO never happened, but a Draft
# one is still a real commitment for reporting purposes (matches
# Procurement.OPEN_STATUSES, already used for the Dashboard's Pending POs
# count and procurement_report's active_value).
PROCUREMENT_COST_STATUSES = frozenset(Procurement.OPEN_STATUSES) | {Procurement.Status.RECEIVED}


def revenue_sales(sales_qs):
    """Narrow a SalesTransaction queryset to realised revenue — Completed
    orders only. The single definition of "counts as a sale" every range
    total, daily series and branch breakdown reads through, so they cannot
    drift apart from each other or from Billing."""
    return sales_qs.filter(order_status__in=REVENUE_ORDER_STATUSES)


def pipeline_sales(sales_qs):
    """Narrow to committed-but-unfulfilled orders (Pending / Approved /
    Preparing) — the pipeline figure shown alongside Gross/Net Sales."""
    return sales_qs.filter(order_status__in=PIPELINE_ORDER_STATUSES)


def gross_sales_total(sales_qs):
    """Gross Sales — Decimal SQL sum of total_amount over Completed orders
    in `sales_qs`. Whatever branch/date scope `sales_qs` already carries is
    preserved; this only narrows the order_status."""
    return _decimal_sum(revenue_sales(sales_qs), 'total_amount')


def pipeline_value_total(sales_qs):
    """Pipeline Value — Decimal SQL sum of total_amount over committed-but-
    unfulfilled orders in `sales_qs`."""
    return _decimal_sum(pipeline_sales(sales_qs), 'total_amount')


def refund_value_total(sales_qs, start=None, end=None):
    """Refund Value — Decimal SQL sum of Refund.total_amount for refunds
    issued against orders visible in `sales_qs`.

    Membership is judged against `sales_qs` as given (any order_status
    filter the caller already applied is ignored on purpose): a refund only
    ever targets what was, at issue time, a Completed order, and
    order_status is never changed by a refund (see sales.models.Refund), so
    filtering the base queryset to Completed first would make no
    difference — passing the branch/date-scoped queryset before its own
    revenue_sales() narrowing is fine and is what every caller here does.

    `start`/`end` optionally narrow to refunds actually RECORDED
    (Refund.refunded_at) in [start, end) — the reporting call sites below
    all leave these unset, so "Refund Value" for a scope means "everything
    ever refunded against these orders", matching how Gross Sales for that
    same scope already ignores when a sale was refunded.
    """
    # Approved only — a Pending or Rejected request (P14) has moved no money.
    refunds = Refund.objects.filter(
        transaction__in=sales_qs, status=Refund.Status.APPROVED
    )
    if start is not None:
        refunds = refunds.filter(refunded_at__gte=start)
    if end is not None:
        refunds = refunds.filter(refunded_at__lt=end)
    return _decimal_sum(refunds, 'total_amount')


def net_sales_total(sales_qs, start=None, end=None):
    """Net Sales — Gross Sales minus Refund Value for the same scope."""
    return gross_sales_total(sales_qs) - refund_value_total(sales_qs, start, end)


def sales_in_range(sales_qs, start, end):
    """Narrow `sales_qs` to transactions dated within the local calendar days
    [start, end], both inclusive: 00:00:00 on `start` through 23:59:59.999999
    on `end` (a half-open [start 00:00, end+1 00:00) comparison, so no row at
    the day's last instant is dropped and none at the next midnight is
    picked up). `start=None` is 'All time' — no lower bound at all.

    Range comparison on aware datetimes, never `__date` — see core/timebounds
    for why that lookup silently matches nothing on this deployment."""
    period_start, period_end = local_range_bounds(start or end, end)
    sales_qs = sales_qs.filter(transaction_date__lt=period_end)
    if start is not None:
        sales_qs = sales_qs.filter(transaction_date__gte=period_start)
    return sales_qs


def sales_range_totals(sales_qs):
    """The figures a daily-sales view prints, for one already-scoped and
    already-ranged queryset — the single source both the Sales Report page
    and its JSON mirror read, so their totals cannot disagree.

    `orders` and every money figure are Completed-only (see the module
    docstring); `transactions` is every row in scope, Cancelled and pipeline
    included, which is what the listing beneath the totals shows.
    `avg_order_value` is Gross Sales per Completed order, rounded half-up to
    centavos so the JSON and the page's floatformat:2 print the same digits.
    """
    orders = revenue_sales(sales_qs).count()
    gross = gross_sales_total(sales_qs)
    refunds = refund_value_total(sales_qs)
    average = (
        (gross / orders).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if orders else Decimal('0.00')
    )
    return {
        'transactions': sales_qs.count(),
        'orders': orders,
        'gross_sales': gross,
        'refund_value': refunds,
        'net_sales': gross - refunds,
        'avg_order_value': average,
        'pipeline_value': pipeline_value_total(sales_qs),
        'pipeline_count': pipeline_sales(sales_qs).count(),
    }


# --- Procurement: what counts as committed cost ----------------------------

def procurement_cost_total(procurement_qs):
    """Procurement Cost — Decimal SQL sum of total_cost, excluding
    Cancelled purchase orders only. See PROCUREMENT_COST_STATUSES."""
    return _decimal_sum(
        procurement_qs.filter(status__in=PROCUREMENT_COST_STATUSES), 'total_cost'
    )


def procurement_return_totals(procurement_qs):
    """Gross return evidence, credit adjustments, and the net-cost deduction.

    Goods shipped back but not yet resolved reduce net procurement at their
    original PO snapshot value. A settled credit uses the actual credit-note
    amount. Replacement settlements remain visible as returned goods but do
    not reduce net procurement because replacement stock was received.
    """
    returns = SupplierReturn.objects.filter(
        procurement__in=procurement_qs,
        status__in=[SupplierReturn.Status.SHIPPED, SupplierReturn.Status.SETTLED],
    ).prefetch_related('items__procurement_item')
    returned_goods = Decimal('0.00')
    open_return_adjustment = Decimal('0.00')
    credits = Decimal('0.00')
    for supplier_return in returns:
        value = sum(
            (
                Decimal(item.quantity_returned)
                * (item.procurement_item.unit_cost_snapshot or Decimal('0.00'))
            )
            for item in supplier_return.items.all()
        )
        returned_goods += value
        if supplier_return.status == SupplierReturn.Status.SHIPPED:
            open_return_adjustment += value
        elif supplier_return.resolution == SupplierReturn.Resolution.CREDIT:
            credits += supplier_return.credit_amount or Decimal('0.00')
    return {
        'returned_goods': returned_goods,
        'open_return_adjustment': open_return_adjustment,
        'credits': credits,
        'net_adjustment': open_return_adjustment + credits,
    }


# --- Inventory: canonical live low-stock predicate --------------------------

def low_stock_count(inventory_qs):
    """Low Stock count — the same live `quantity_on_hand <= reorder_level`
    predicate inventory.alerts.low_stock_alerts() ranks, never the stored
    `restock` flag (see that module's docstring: the flag is only ever
    recomputed inside Inventory.save(), so any write that bypasses save()
    — a bulk update, a fixture, a raw query — leaves it stale with nothing
    to catch it)."""
    return low_stock_filter(inventory_qs).count()


# --- Shared scope resolution ------------------------------------------------

def scoped_report_querysets(user, override_branch_id=None):
    """Sales/procurement/inventory/warehouse querysets scoped to the
    EFFECTIVE branch: a Branch Manager or Staff account's own branch always
    wins regardless of `override_branch_id` (they have no selector to send
    one from), an Owner's explicit branch choice is honoured when given,
    and an Owner with no choice made sees every branch.

    The one scope-resolution function reports/views.py and
    reports/api_views.py both call, so a template view and its API mirror
    can never apply different branch rules to the same user.
    """
    sales_qs = SalesTransaction.objects.all()
    procurement_qs = Procurement.objects.all()
    inventory_qs = Inventory.objects.select_related('product', 'branch').prefetch_related('product__price_tiers').all()
    warehouse_qs = WarehouseStock.objects.select_related('product', 'warehouse__branch').all()

    effective_branch = branch_for_user(user)
    if effective_branch is None and user.role == User.Role.OWNER and override_branch_id:
        effective_branch = Branch.objects.filter(pk=override_branch_id).first()

    if effective_branch:
        sales_qs = sales_qs.filter(branch=effective_branch)
        procurement_qs = procurement_qs.filter(branch=effective_branch)
        inventory_qs = inventory_qs.filter(branch=effective_branch)
        warehouse_qs = warehouse_qs.filter(warehouse__branch=effective_branch)

    # Backstop — see branch_for_user's own docstring: None means "Owner, see
    # everything" AND "unassigned Manager/Staff, see nothing", and the block
    # above cannot tell them apart. scope_queryset goes via
    # branch_ids_for_user, which returns None only for the Owner and [] for
    # an unassigned account, so it fails CLOSED where the block above would
    # otherwise fail open.
    sales_qs = scope_queryset(sales_qs, user, 'branch')
    procurement_qs = scope_queryset(procurement_qs, user, 'branch')
    inventory_qs = scope_queryset(inventory_qs, user, 'branch')
    warehouse_qs = scope_queryset(warehouse_qs, user, 'warehouse__branch')

    return sales_qs, procurement_qs, inventory_qs, warehouse_qs, effective_branch


def scoped_sales_in_range(user, start, end, branch_id=None):
    """The daily-sales row set: `user`'s branch-scoped sales (an Owner may
    narrow to `branch_id`; anyone else's own branch wins, and an unassigned
    account gets nothing — see scoped_report_querysets) within the local
    calendar days [start, end], newest first.

    The one path the Sales Report page, its JSON mirror and its CSV export
    all read, so the three cannot list or total different rows."""
    sales_qs = scoped_report_querysets(user, override_branch_id=branch_id)[0]
    sales_qs = sales_qs.select_related('branch', 'staff', 'customer')
    return sales_in_range(sales_qs, start, end).order_by('-transaction_date')
