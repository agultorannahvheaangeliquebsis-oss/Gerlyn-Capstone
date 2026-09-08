"""
inventory/alerts.py — the single source of truth for stock alerts.

Used by the Alerts Center, the Dashboard restock suggestions, and any
report that needs "what needs reordering / what's expiring". Keeping the
logic here (not copy-pasted into each view) guarantees every screen shows
the same numbers.

Severity model (Alerts Center ranking — low_stock_alerts/low_warehouse_stock_alerts)
--------------------------------------------------------------------------------
Only ever applied to rows already at or below reorder_level, to rank how
urgently each one needs restocking:
  Critical : quantity_on_hand <= 50% of reorder_level
  High     : quantity_on_hand <= 80% of reorder_level
  Moderate : quantity_on_hand <= reorder_level

Suggested reorder quantity = reorder_level * 2 - quantity_on_hand
(brings stock back to ~2x the reorder point — matches the UI mockups).

Stock status (Floor Stock / Warehouse Stock list pages — stock_status())
--------------------------------------------------------------------
A different, simpler tier a row is always in exactly one of — the label
shown in the Status column and the KPI counts above it. Not the same scale
as the severity model above: reorder_level is the dividing line between
Critical and Low Stock here, not a threshold Low Stock sits below.
  No Stock  : quantity_on_hand == 0
  Critical  : 0 < quantity_on_hand <= reorder_level
  Low Stock : reorder_level < quantity_on_hand <= 110% of reorder_level
  In Stock  : quantity_on_hand > 110% of reorder_level
A reorder_level of 0 collapses Low Stock to nothing (110% of 0 is still 0),
so anything above 0 reads straight as In Stock — reorder_level is expected
to always be set in practice, so this is treated as a non-issue rather than
special-cased.

Two independent low-stock alert types, never mixed:
  low_stock_alerts()           -> Inventory (branch FLOOR stock).
                                   Correct action: Request Stock to Floor,
                                   pulling from that branch's own warehouse.
  low_warehouse_stock_alerts() -> WarehouseStock (branch WAREHOUSE stock).
                                   Correct action: a Purchase Order to the
                                   supplier.
A floor alert must never suggest a PO, and a warehouse alert must never
suggest a stock request — each function/template only ever points to
its own correct next step.

Expiry alerts
-------------
Sourced from Batch — one row per lot, floor and warehouse both (see the
Batch model's own docstring for how a lot's real expiry now survives a
restock instead of being merged into one field). Three severity tiers, a
batch falls into exactly one (its most urgent applicable tier):
  Expired             : expiry_date <= today
  Expiring Soon       : today < expiry_date <= today + 30 days
  Approaching Expiry  : today + 30 days < expiry_date <= today + 90 days
Only batches with an expiry_date set are ever considered.
"""
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from .models import Batch, Inventory, WarehouseStock

EXPIRING_SOON_DAYS = 30
APPROACHING_EXPIRY_DAYS = 90


def suggested_reorder_qty(quantity_on_hand: int, reorder_level: int) -> int:
    """How many units to reorder to reach ~2x the reorder point."""
    return max(reorder_level * 2 - quantity_on_hand, 0)


def stock_status(quantity_on_hand: int, reorder_level: int) -> str:
    """Return 'No Stock' | 'Critical' | 'Low Stock' | 'In Stock' — the Status
    column tier for one Floor Stock / Warehouse Stock row. See the module
    docstring's "Stock status" section for the exact thresholds; this is the
    one place they're defined, shared by both list pages so they can't drift
    out of sync with each other."""
    if quantity_on_hand == 0:
        return "No Stock"
    if quantity_on_hand <= reorder_level:
        return "Critical"
    if quantity_on_hand <= reorder_level * 1.1:
        return "Low Stock"
    return "In Stock"


def stock_risk(quantity_on_hand: int, reorder_level: int) -> str:
    """Return 'Critical' | 'High' | 'Moderate' | 'OK'."""
    if reorder_level <= 0:
        return "OK"
    if quantity_on_hand <= reorder_level * 0.5:
        return "Critical"
    if quantity_on_hand <= reorder_level * 0.8:
        return "High"
    if quantity_on_hand <= reorder_level:
        return "Moderate"
    return "OK"


def low_stock_alerts(branch_ids=None):
    """
    Return low/critical-stock rows as dicts, ordered most-urgent first.

    `branch_ids=None` -> all branches (Owner). Pass a list to scope to a
    manager/staff's branch (use core.scoping.branch_ids_for_user).
    """
    # Computed live rather than filtered on the stored Inventory.restock flag
    # — that flag is only ever recomputed inside Inventory.save(), so any
    # write that bypasses save() (bulk update, a data import/fixture, a
    # raw query) leaves it stale or wrong with nothing to catch it. This
    # query can't drift out of sync with quantity_on_hand/reorder_level
    # because it reads them directly, every time.
    qs = (
        Inventory.objects.filter(quantity_on_hand__lte=F("reorder_level"))
        .select_related("product", "product__category", "branch")
    )
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)

    rows = []
    for inv in qs:
        rows.append(
            {
                "inventory": inv,
                "product": inv.product,
                "branch": inv.branch,
                "quantity_on_hand": inv.quantity_on_hand,
                "reorder_level": inv.reorder_level,
                "suggested_qty": suggested_reorder_qty(
                    inv.quantity_on_hand, inv.reorder_level
                ),
                "risk": stock_risk(inv.quantity_on_hand, inv.reorder_level),
            }
        )
    # Critical first, then by how far below reorder (relative).
    order = {"Critical": 0, "High": 1, "Moderate": 2, "OK": 3}
    rows.sort(
        key=lambda r: (
            order[r["risk"]],
            r["quantity_on_hand"] / r["reorder_level"] if r["reorder_level"] else 1,
        )
    )
    return rows


def low_warehouse_stock_alerts(branch_ids=None):
    """
    Return low/critical WAREHOUSE-stock rows as dicts, ordered most-urgent
    first — same shape as low_stock_alerts(), but sourced from
    WarehouseStock instead of Inventory. The correct action for these is a
    Purchase Order (never a stock request, which pulls FROM the
    warehouse — it can't fix the warehouse itself running low).

    `branch_ids=None` -> all branches (Owner). Pass a list to scope to a
    manager/staff's branch (use core.scoping.branch_ids_for_user).
    """
    qs = (
        WarehouseStock.objects.filter(quantity_on_hand__lte=F("reorder_level"))
        .select_related("product", "product__category", "warehouse", "warehouse__branch")
    )
    if branch_ids is not None:
        qs = qs.filter(warehouse__branch_id__in=branch_ids)

    rows = []
    for ws in qs:
        rows.append(
            {
                "warehouse_stock": ws,
                "product": ws.product,
                "branch": ws.warehouse.branch,
                "quantity_on_hand": ws.quantity_on_hand,
                "reorder_level": ws.reorder_level,
                "suggested_qty": suggested_reorder_qty(
                    ws.quantity_on_hand, ws.reorder_level
                ),
                "risk": stock_risk(ws.quantity_on_hand, ws.reorder_level),
            }
        )
    order = {"Critical": 0, "High": 1, "Moderate": 2, "OK": 3}
    rows.sort(
        key=lambda r: (
            order[r["risk"]],
            r["quantity_on_hand"] / r["reorder_level"] if r["reorder_level"] else 1,
        )
    )
    return rows


def _expiry_tier(days_until_expiry):
    """Which of the three tiers a days-until-expiry number falls into, or
    None if it's fresh enough not to need one yet."""
    if days_until_expiry <= 0:
        return "Expired"
    if days_until_expiry <= EXPIRING_SOON_DAYS:
        return "Expiring Soon"
    if days_until_expiry <= APPROACHING_EXPIRY_DAYS:
        return "Approaching Expiry"
    return None


def expiry_alerts(branch_ids=None):
    """
    Return {"Expired": [...], "Expiring Soon": [...], "Approaching Expiry": [...]},
    each a list of rows sourced from Batch — one row per lot, floor and
    warehouse both, nearest expiry first. Each row is labeled with its
    `location` ("Floor" or "Warehouse") since the same product can have lots
    expiring in both places at once, independently.

    Batch-sourced rather than Inventory/WarehouseStock's own expiry_date:
    those aggregate fields no longer carry real per-lot expiry (see the
    Batch model's docstring) — a row here already IS one lot, so no
    per-product/branch grouping is needed the way the old aggregate-field
    version needed.
    """
    # localdate(), not now().date(): under USE_TZ, now() is UTC — from
    # midnight to 8am Asia/Manila time that's still "yesterday" in UTC, which
    # would misclassify a row expiring today as one more day fresh than it is.
    today = timezone.localdate()
    cutoff = today + timedelta(days=APPROACHING_EXPIRY_DAYS)
    tiers = {"Expired": [], "Expiring Soon": [], "Approaching Expiry": []}

    qs = (
        Batch.objects.filter(
            expiry_date__isnull=False, expiry_date__lte=cutoff, quantity__gt=0
        )
        .select_related("product", "branch")
    )
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)

    for batch in qs:
        days = (batch.expiry_date - today).days
        tier = _expiry_tier(days)
        if tier is None:
            continue
        tiers[tier].append({
            "product": batch.product,
            "branch": batch.branch,
            "location": "Floor" if batch.location == Batch.Location.FLOOR else "Warehouse",
            "quantity_on_hand": batch.quantity,
            "expiry_date": batch.expiry_date,
            "days_until_expiry": days,
            # What the Expired tab's "Confirm Write-Off" action
            # (inventory.views.expiry_write_off) targets — one specific lot,
            # not the whole product/branch row.
            "batch_id": batch.pk,
        })

    for rows in tiers.values():
        rows.sort(key=lambda r: r["expiry_date"])
    return tiers


def alert_summary(branch_ids=None):
    """Counts for the Alerts Center header + Dashboard KPI tiles."""
    low = low_stock_alerts(branch_ids)
    low_warehouse = low_warehouse_stock_alerts(branch_ids)
    expiry = expiry_alerts(branch_ids)
    return {
        "critical_count": sum(1 for r in low if r["risk"] == "Critical"),
        "low_count": len(low),
        "warehouse_critical_count": sum(1 for r in low_warehouse if r["risk"] == "Critical"),
        "warehouse_low_count": len(low_warehouse),
        "expired_count": len(expiry["Expired"]),
        "expiring_soon_count": len(expiry["Expiring Soon"]),
        "approaching_expiry_count": len(expiry["Approaching Expiry"]),
        "low_stock": low,
        "low_warehouse_stock": low_warehouse,
        "expiry_expired": expiry["Expired"],
        "expiry_soon": expiry["Expiring Soon"],
        "expiry_approaching": expiry["Approaching Expiry"],
    }
