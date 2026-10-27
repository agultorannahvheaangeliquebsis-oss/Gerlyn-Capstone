"""Storefront helpers for serviceable areas: what checkout asks, remembers and says.

The rule itself is ``branches.coverage``; this is the session and message side
of it, shared by checkout, the live check endpoint and the customer sales API.
"""
from __future__ import annotations

import time

from django.urls import reverse

from accounts.geo import resolve_place
from branches import coverage as cov
from core.notifications import notify_customer

from .delivery import _place_input

# Set when a shopper is stopped (or stops themselves) at a delivery address the
# branch does not serve. Read when the order is then placed as Pickup, so the
# switch can be confirmed with a notification; cleared whenever an order is
# placed. Holds only a branch id and a place label.
AREA_SWITCH_KEY = 'area_switch'
AREA_SWITCH_TTL = 60 * 60

SWITCH_HELP = 'Switch to Pickup or choose another branch.'


SAVED_ADDRESS_UNCHECKABLE = (
    'Your saved address does not say which province, municipality and barangay '
    'it is in, so we cannot check it against our delivery areas. Choose “Use a '
    'different address for this order” and pick the place from the lists, '
    'update the address in Settings, or choose Pickup.'
)

# Statuses that mean "the place could not be pinned down" (as opposed to "it is
# a place we do not serve"): for a saved address the fix is different.
_UNPINNED = (cov.INCOMPLETE, cov.UNRECOGNISED, cov.NEEDS_BARANGAY)


def uses_saved_address(post, saved_address):
    """True when the checkout form is using the account's saved address (the
    default) rather than one typed for this order."""
    return saved_address is not None and post.get('address_source') != 'typed'


def area_message(coverage, *, saved):
    """What to tell the shopper. The generic "choose your place from the lists"
    is wrong advice for a saved address, whose lists are hidden until they pick
    "Use a different address", so that case gets its own wording."""
    if saved and coverage.status in _UNPINNED:
        return SAVED_ADDRESS_UNCHECKABLE
    return coverage.message


def place_from_form(post, saved_address):
    """The place the checkout form is pointing at, as ``geo.Place``.

    Mirrors ``orders.delivery.clean_delivery_form``: the saved address unless
    the shopper chose to type one for this order.
    """
    if uses_saved_address(post, saved_address):
        return resolve_place(saved_address.province, saved_address.city, saved_address.barangay)
    return resolve_place(
        _place_input(post, 'addr_province'),
        _place_input(post, 'addr_city'),
        _place_input(post, 'addr_barangay'),
    )


def remember_area_block(session, branch, coverage):
    session[AREA_SWITCH_KEY] = {
        'branch_id': branch.pk,
        'place': coverage.place_label,
        'at': int(time.time()),
    }


def pop_area_switch(session, branch_id):
    """The remembered block for this branch if it is recent, else None. Always
    clears it."""
    switch = session.pop(AREA_SWITCH_KEY, None)
    if not switch or switch.get('branch_id') != branch_id:
        return None
    if time.time() - switch.get('at', 0) > AREA_SWITCH_TTL:
        return None
    return switch


def notify_pickup_switch(customer, sale, switch):
    """Tell the customer, in the app, that their order became a pickup order
    because the address was outside the delivery areas."""
    place = switch.get('place') or 'that address'
    notify_customer(
        customer,
        'Your order was switched to pickup',
        f'{place} is outside the areas {sale.branch.branch_name} delivers to, so '
        f'order {sale.order_number} is for pickup instead. Collect it at '
        f'{sale.branch.branch_name}; we will let you know when it is ready.',
        action_url=reverse('orders:track_order', args=[sale.pk]),
        action_label='View order',
    )


def send_registration_notice(user):
    """The one-time "where we deliver" notice for a brand-new customer.

    A bell notification (with a link to the full list) created at sign-up, so
    it can only ever exist once per account; the text is returned for a toast.
    Returns '' and sends nothing when no branch has serviceable areas set up,
    since there is then nothing true to say.
    """
    from branches.models import Branch

    configured, summary = cov.banner_summary(Branch.objects.filter(is_active=True))
    if not configured:
        return ''
    if summary:
        text = (
            f'We deliver to: {summary}. Outside these areas? Choose Pickup at '
            f'checkout and collect your order at the branch.'
        )
    else:
        text = 'Delivery isn’t available right now. You can choose Pickup at checkout.'
    notify_customer(
        user, 'Where we deliver', text,
        action_url=reverse('orders:service_areas'),
        action_label='View all serviceable areas',
    )
    return text


def check_payload(branch, coverage, place, *, order_total=None, saved=False):
    """The JSON the checkout page's live check reads."""
    others = []
    if not coverage.covered and not place.is_empty:
        others = [b.branch_name for b in cov.covering_branches(
            place, exclude_id=branch.pk, order_total=order_total)]
    return {
        'status': coverage.status,
        'ok': coverage.ok,
        'covered': coverage.covered,
        'configured': coverage.status != cov.OPEN,
        'message': area_message(coverage, saved=saved),
        'place': coverage.place_label or place.label(),
        'branch': branch.branch_name,
        'delivery_fee': None if coverage.delivery_fee is None else f'{coverage.delivery_fee:.2f}',
        'minimum_order': None if coverage.minimum_order is None else f'{coverage.minimum_order:.2f}',
        'other_branches': others,
        'resolved': {
            'province': place.province_code,
            'city': place.city_code,
            'barangay': place.barangay_code,
        },
    }
