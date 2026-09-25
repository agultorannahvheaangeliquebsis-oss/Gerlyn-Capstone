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
from decimal import Decimal

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce

from accounts.models import User
from branches.models import Branch
from core.scoping import branch_for_user, scope_queryset
from inventory.alerts import low_stock_filter
from inventory.models import Inventory, WarehouseStock
from procurement.models import Procurement
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


# --- Procurement: what counts as committed cost ----------------------------

def procurement_cost_total(procurement_qs):
    """Procurement Cost — Decimal SQL sum of total_cost, excluding
    Cancelled purchase orders only. See PROCUREMENT_COST_STATUSES."""
    return _decimal_sum(
        procurement_qs.filter(status__in=PROCUREMENT_COST_STATUSES), 'total_cost'
    )


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
