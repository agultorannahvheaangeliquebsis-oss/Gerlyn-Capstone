"""inventory/alert_actions.py - what each Alerts Center row lets the viewer DO.

The alert engine (inventory/alerts.py) says what is wrong; this says what to do
about it, using only flows that already exist, and only the ones the viewer's
own grants let them use - a button that bounces the user back to their
dashboard with a flash message is worse than no button.

Per alert type:
  Floor low        Warehouse has stock  -> Request Floor Refill
                   Warehouse is empty   -> Create PO (never both: a transfer
                                           from an empty warehouse cannot work)
  Warehouse low    -> Create PO
  Expired          -> Write Off (existing, gated by the Alerts delete grant)
  Expiring soon /  Warehouse lot        -> Request to Floor (the transfer
  Approaching      consumes nearest-expiry batches first, so it sells the lot
                   before its date); floor lot has no flow to offer
  Every stock row  -> History (this product's movement journal), when the
                      viewer may open the Product Master List

"Create PO" is Owner/Branch Manager only: procurement_add refuses everyone
else, and the Procurement module's Add grant decides the rest.
"""
from urllib.parse import urlencode

from django.urls import NoReverseMatch, reverse

from accounts.models import User
from core.module_permissions import (
    ADD, DELIVERIES, PRODUCTS, PROCUREMENT, VIEW, WAREHOUSE_STOCK,
    user_can_perform_module_action,
)

from .models import WarehouseStock

# location.hash -> the panel it opens on the Alerts Center. Every link that
# points at the page (notifications, dashboard "Needs attention" rows) must use
# a key here; the page's script reads this same map (via json_script), so the
# two cannot drift. 'low' and 'expiring' are the dashboard's older spellings.
ALERT_TAB_HASHES = {
    'floor': 'floor',
    'low': 'floor',
    'warehouse': 'warehouse',
    'expired': 'expired',
    'expiring': 'expired',
    'soon': 'soon',
    'approaching': 'approaching',
}
ALERT_PANELS = ('floor', 'warehouse', 'expired', 'soon', 'approaching')


class AlertPermissions:
    """What one user may do from an alert row, decided once per page."""

    def __init__(self, user):
        self.user = user
        self.is_owner = user.role == User.Role.OWNER
        self.can_request = user_can_perform_module_action(user, WAREHOUSE_STOCK, ADD)
        self.can_create_po = (
            user.role in (User.Role.OWNER, User.Role.BRANCH_MANAGER)
            and user_can_perform_module_action(user, PROCUREMENT, ADD)
        )
        self.can_history = user_can_perform_module_action(user, PRODUCTS, VIEW)


def _link(kind, label, url, primary=False):
    return {'kind': kind, 'label': label, 'url': url, 'primary': primary}


def request_stock_url(branch):
    return f"{reverse('warehouse_stock_list')}?{urlencode({'open_request': 1, 'branch': branch.pk})}"


def create_po_url(branch, product, quantity):
    """procurement_add prefill. Only ever a convenience: the page re-validates
    branch, product and quantity itself."""
    params = {'branch': branch.pk, 'product': product.pk}
    if quantity and quantity > 0:
        params['quantity'] = quantity
    return f"{reverse('procurement_add')}?{urlencode(params)}"


def history_url(perms, product, branch):
    """This product's movement journal, all time. The branch parameter narrows
    it for an Owner and is ignored for everyone else (their branch is fixed)."""
    params = {'product': product.pk, 'range': 'all'}
    if perms.is_owner:
        params['branch'] = branch.pk
    return f"{reverse('stock_movement_history')}?{urlencode(params)}"


def _warehouse_available(rows):
    """{(product_id, branch_id): units the warehouse could actually release}
    for every (product, branch) pair among `rows`, in one query. Sellable, so a
    warehouse holding only expired stock counts as empty."""
    product_ids = {r['product'].pk for r in rows}
    branch_ids = {r['branch'].pk for r in rows}
    if not rows:
        return {}
    stock = WarehouseStock.objects.sellable().filter(
        product_id__in=product_ids, warehouse__branch_id__in=branch_ids,
    ).values_list('product_id', 'warehouse__branch_id', 'available_qty')
    return {(p, b): qty for p, b, qty in stock}


def decorate_alerts(user, summary):
    """Attach `actions` (a list of link dicts) and `note` to every alert row in
    `summary`, in place, and return the AlertPermissions used."""
    perms = AlertPermissions(user)

    available = _warehouse_available(summary['low_stock'])
    for row in summary['low_stock']:
        actions, note = [], ''
        if available.get((row['product'].pk, row['branch'].pk), 0) > 0:
            if perms.can_request:
                actions.append(_link(
                    'request', 'Request Floor Refill',
                    request_stock_url(row['branch']), primary=True))
        else:
            if perms.can_create_po:
                actions.append(_link(
                    'po', 'Create PO',
                    create_po_url(row['branch'], row['product'], row['suggested_qty']),
                    primary=True))
            note = 'No warehouse stock — needs a purchase order'
        if perms.can_history:
            actions.append(_link('history', 'History', history_url(perms, row['product'], row['branch'])))
        row['actions'], row['note'] = actions, note

    for row in summary['low_warehouse_stock']:
        actions = []
        if perms.can_create_po:
            actions.append(_link(
                'po', 'Create PO',
                create_po_url(row['branch'], row['product'], row['suggested_qty']),
                primary=True))
        if perms.can_history:
            actions.append(_link('history', 'History', history_url(perms, row['product'], row['branch'])))
        row['actions'], row['note'] = actions, ''

    # Expired lots keep their own Write Off button (a POST with a confirm
    # modal, drawn by the template); only History is a plain link here.
    for key in ('expiry_expired', 'expiry_soon', 'expiry_approaching'):
        for row in summary[key]:
            actions = []
            if key != 'expiry_expired' and row['location'] == 'Warehouse' and perms.can_request:
                actions.append(_link(
                    'request', 'Request to Floor',
                    request_stock_url(row['branch']), primary=True))
            if perms.can_history:
                actions.append(_link('history', 'History', history_url(perms, row['product'], row['branch'])))
            row['actions'], row['note'] = actions, ''
    return perms


def overdue_order_url(user, procurement):
    """Where an overdue-delivery row should lead this user, or '' if nowhere.

    procurement_detail is the full purchase-order page but sits behind the
    Procurement module; Staff work deliveries, so they get delivery_detail
    (same order, opened through the Deliveries module and record-level Staff
    policy). Falls back to the Deliveries list when a page cannot be opened
    or does not exist."""
    def resolve(name, *args):
        try:
            return reverse(name, args=args)
        except NoReverseMatch:
            return ''

    if (
        user.role in (User.Role.OWNER, User.Role.BRANCH_MANAGER)
        and user_can_perform_module_action(user, PROCUREMENT, VIEW)
    ):
        return resolve('procurement_detail', procurement.pk)
    if user_can_perform_module_action(user, DELIVERIES, VIEW):
        return resolve('delivery_detail', procurement.pk) or resolve('delivery_list')
    return ''
