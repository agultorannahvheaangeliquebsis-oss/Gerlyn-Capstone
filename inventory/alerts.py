"""
inventory/alerts.py — the single source of truth for stock alerts.

Used by the Alerts Center, the Dashboard restock suggestions, and any
report that needs "what needs reordering / what's expiring". Keeping the
logic here (not copy-pasted into each view) guarantees every screen shows
the same numbers.

Severity model
--------------
low-stock  : quantity_on_hand <= reorder_level
  Critical : quantity_on_hand <= 50% of reorder_level
  High     : quantity_on_hand <= 80% of reorder_level
  Moderate : quantity_on_hand <= reorder_level

Suggested reorder quantity = reorder_level * 2 - quantity_on_hand
(brings stock back to ~2x the reorder point — matches the UI mockups).

Two independent low-stock alert types, never mixed:
  low_stock_alerts()           -> Inventory (branch FLOOR stock).
                                   Correct action: a Stock Adjustment,
                                   pulling from that branch's own warehouse.
  low_warehouse_stock_alerts() -> WarehouseStock (branch WAREHOUSE stock).
                                   Correct action: a Purchase Order to the
                                   supplier.
A floor alert must never suggest a PO, and a warehouse alert must never
suggest a Stock Adjustment — each function/template only ever points to
its own correct next step.

Expiry alerts
-------------
Sourced from Inventory.expiry_date (floor) and WarehouseStock.expiry_date
(warehouse) — the fields managers actually set from the Floor Stock /
Warehouse Stock edit modals. Three severity tiers, a product falls into
exactly one (its most urgent applicable tier):
  Expired             : expiry_date <= today
  Expiring Soon       : today < expiry_date <= today + 30 days
  Approaching Expiry  : today + 30 days < expiry_date <= today + 90 days
Only rows with an expiry_date set are ever considered.
"""
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from .models import Inventory, WarehouseStock

EXPIRING_SOON_DAYS = 30
APPROACHING_EXPIRY_DAYS = 90


def suggested_reorder_qty(quantity_on_hand: int, reorder_level: int) -> int:
    """How many units to reorder to reach ~2x the reorder point."""
    return max(reorder_level * 2 - quantity_on_hand, 0)


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
    Purchase Order (never a Stock Adjustment, which pulls FROM the
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
    each a list of rows sourced from both floor stock (Inventory) and
    warehouse stock (WarehouseStock), nearest expiry first. Each row is
    labeled with its `location` ("Floor" or "Warehouse") since the same
    product can be expiring in both places at once, independently.
    """
    today = timezone.now().date()
    cutoff = today + timedelta(days=APPROACHING_EXPIRY_DAYS)
    tiers = {"Expired": [], "Expiring Soon": [], "Approaching Expiry": []}

    floor_qs = (
        Inventory.objects.filter(expiry_date__isnull=False, expiry_date__lte=cutoff)
        .select_related("product", "branch")
    )
    if branch_ids is not None:
        floor_qs = floor_qs.filter(branch_id__in=branch_ids)

    for inv in floor_qs:
        days = (inv.expiry_date - today).days
        tier = _expiry_tier(days)
        if tier is None:
            continue
        tiers[tier].append({
            "product": inv.product,
            "branch": inv.branch,
            "location": "Floor",
            "quantity_on_hand": inv.quantity_on_hand,
            "expiry_date": inv.expiry_date,
            "days_until_expiry": days,
        })

    warehouse_qs = (
        WarehouseStock.objects.filter(expiry_date__isnull=False, expiry_date__lte=cutoff)
        .select_related("product", "warehouse", "warehouse__branch")
    )
    if branch_ids is not None:
        warehouse_qs = warehouse_qs.filter(warehouse__branch_id__in=branch_ids)

    for ws in warehouse_qs:
        days = (ws.expiry_date - today).days
        tier = _expiry_tier(days)
        if tier is None:
            continue
        tiers[tier].append({
            "product": ws.product,
            "branch": ws.warehouse.branch,
            "location": "Warehouse",
            "quantity_on_hand": ws.quantity_on_hand,
            "expiry_date": ws.expiry_date,
            "days_until_expiry": days,
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
