"""Damaged Goods figures for the Inventory Report.

Counts the reports a manager has confirmed (Approved, then Resolved) - never
Pending, Under Review or Rejected, which have not been accepted as damage.
Quantity is the units on the report; estimated loss is units x the unit cost
frozen on the report when it was filed (the same cost basis the Inventory
Report values stock with, Product.bulk_floor_price), so a price change today
does not rewrite what last month's damage cost.

A customer-reported item counts here too: the goods were sold and have left
the building, but the store still bears the loss when it refunds or replaces
them. They are shown as their own column so the two sources stay readable.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import DamageReport

# (query value, label, days or None for all time)
PERIODS = (
    ('30', 'Last 30 days', 30),
    ('90', 'Last 90 days', 90),
    ('365', 'Last 12 months', 365),
    ('all', 'All time', None),
)
DEFAULT_PERIOD = 'all'


def normalise_period(value):
    return value if value in {code for code, _label, _days in PERIODS} else DEFAULT_PERIOD


def damaged_goods(branch_ids=None, period=DEFAULT_PERIOD):
    """(rows, totals) of confirmed damage, one row per branch and product.

    `branch_ids=None` means every branch (Owner); a list scopes to those
    branches, and an empty list returns nothing - the same fail-closed
    contract as core.scoping.branch_ids_for_user. `period` is one of PERIODS,
    measured from the day the manager confirmed the report.
    """
    reports = DamageReport.objects.filter(
        status__in=DamageReport.DECIDED_STATUSES,
    ).select_related('branch', 'product')
    if branch_ids is not None:
        reports = reports.filter(branch_id__in=branch_ids)
    days = {code: d for code, _label, d in PERIODS}[normalise_period(period)]
    if days is not None:
        reports = reports.filter(reviewed_at__gte=timezone.now() - timedelta(days=days))

    grouped = {}
    for report in reports:
        key = (report.branch_id, report.product_id or report.product_name)
        row = grouped.setdefault(key, {
            'branch': report.branch.branch_name if report.branch_id else '—',
            'product': report.product_label,
            'sku': report.product.sku_code if report.product_id else '',
            'reports': 0, 'units': 0, 'customer_units': 0,
            'loss': Decimal('0.00'),
        })
        row['reports'] += 1
        row['units'] += report.quantity
        if report.is_customer_report:
            row['customer_units'] += report.quantity
        row['loss'] += report.loss_value

    rows = sorted(grouped.values(), key=lambda r: (-r['loss'], r['branch'], r['product']))
    totals = {
        'reports': sum(r['reports'] for r in rows),
        'units': sum(r['units'] for r in rows),
        'loss': sum((r['loss'] for r in rows), Decimal('0.00')),
    }
    return rows, totals
