"""
inventory/alerts.py — the single source of truth for stock alerts.

Used by the Alerts Center, the Dashboard restock suggestions, and any
report that needs "what needs reordering / what's expiring". Keeping the
logic here (not copy-pasted into each view) guarantees every screen shows
the same numbers.

Severity model
--------------
low-stock  : quantity_on_hand <= reorder_level          (Inventory.restock=True)
  Critical : quantity_on_hand <= 50% of reorder_level
  High     : quantity_on_hand <= 80% of reorder_level
  Moderate : quantity_on_hand <= reorder_level

Suggested reorder quantity = reorder_level * 2 - quantity_on_hand
(brings stock back to ~2x the reorder point — matches the UI mockups).
"""
from datetime import timedelta

from django.utils import timezone

from .models import Batch, Inventory

# Batches expiring within this many days raise an expiry alert.
EXPIRY_WINDOW_DAYS = 14


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
    qs = (
        Inventory.objects.filter(restock=True)
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


def expiry_alerts(branch_ids=None, window_days=EXPIRY_WINDOW_DAYS):
    """
    Return batches that are expired or expiring within `window_days`,
    nearest expiry first. Only batches with stock remaining.
    """
    today = timezone.now().date()
    cutoff = today + timedelta(days=window_days)

    qs = (
        Batch.objects.filter(quantity__gt=0, expiry_date__lte=cutoff)
        .select_related("product", "branch")
        .order_by("expiry_date")
    )
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)

    rows = []
    for batch in qs:
        rows.append(
            {
                "batch": batch,
                "product": batch.product,
                "branch": batch.branch,
                "quantity": batch.quantity,
                "expiry_date": batch.expiry_date,
                "days_until_expiry": batch.days_until_expiry,
                "status": batch.expiry_status,  # Fresh / Expiring Soon / Expired
            }
        )
    return rows


def alert_summary(branch_ids=None):
    """Counts for the Alerts Center header + Dashboard KPI tiles."""
    low = low_stock_alerts(branch_ids)
    expiring = expiry_alerts(branch_ids)
    return {
        "critical_count": sum(1 for r in low if r["risk"] == "Critical"),
        "low_count": len(low),
        "expiring_count": len(expiring),
        "low_stock": low,
        "expiry": expiring,
    }
