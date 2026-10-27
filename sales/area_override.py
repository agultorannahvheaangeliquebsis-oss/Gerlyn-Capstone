"""A delivery's serviceable area, as the Order Console sees it, and the one way
to let an out-of-area delivery stand.

The customer cannot place a delivery the branch does not serve (checkout and
the sales API refuse it), so an order only meets this when the branch's list
changed AFTER the order was placed: an Owner deactivated an area, or excluded
a barangay, while orders for it were still open. Those orders are flagged on
the order, and Approving one is refused until an Owner or the order's Branch
Manager overrides, with a reason that is recorded on the order and in the Audit
Log. Staff can see the flag and the override but cannot make one.

A delivery whose area was never recorded (placed before service areas existed,
or at a branch that had none) reads as "area not recorded" - unknown, never
outside - and is not held up.
"""
from django.db import transaction
from django.utils import timezone

from audit import context as audit_context
from branches.coverage import check_delivery
from core.permissions import BRANCH_MANAGER, OWNER
from core.scoping import user_can_access_branch

from .models import SalesTransaction

OVERRIDE_ROLES = (OWNER, BRANCH_MANAGER)
MIN_REASON_LENGTH = 8
MAX_REASON_LENGTH = 255

# Orders still on their way; a completed or cancelled one has nothing to hold.
_OPEN = (
    SalesTransaction.OrderStatus.PENDING,
    SalesTransaction.OrderStatus.APPROVED,
    SalesTransaction.OrderStatus.PREPARING,
)


class AreaOverrideError(Exception):
    """A refusal with a message that is fine to show the person."""


def is_online_delivery(txn):
    return (
        txn.transaction_type == SalesTransaction.TransactionType.ONLINE
        and txn.fulfillment_type == SalesTransaction.FulfillmentType.DELIVERY
    )


def area_state(txn):
    """What the order page says about the delivery's area.

    ``applies`` is False for pickup and walk-in sales. ``outside`` is True only
    when the order recorded a place AND the branch's current list does not
    cover it. ``can_be_overridden`` adds that the order is still open.
    """
    if not is_online_delivery(txn):
        return {'applies': False}
    place = txn.delivery_place
    state = {
        'applies': True,
        'recorded': not place.is_empty,
        'label': place.label(),
        'coverage': None,
        'outside': False,
        'overridden': txn.has_area_override,
        'can_be_overridden': False,
    }
    if state['recorded']:
        coverage = check_delivery(txn.branch, place)
        state['coverage'] = coverage
        state['outside'] = not coverage.covered
    state['can_be_overridden'] = (
        state['outside'] and not state['overridden'] and txn.order_status in _OPEN
    )
    return state


def approval_block_message(txn):
    """Why Approve must wait, or '' when it need not."""
    state = area_state(txn)
    if state.get('outside') and not state['overridden']:
        return (
            f'{txn.order_number} is for {state["label"]}, which {txn.branch.branch_name} '
            f'does not deliver to. An Owner or Branch Manager has to override the '
            f'delivery area (with a reason) before it can be approved, or the order '
            f'can be cancelled.'
        )
    return ''


def can_override(user, txn):
    return user.role in OVERRIDE_ROLES and user_can_access_branch(user, txn.branch_id)


def override_area(txn_pk, actor, reason):
    """Record an Owner / Branch Manager's override on one delivery order.

    Re-checked against the locked row, so a stale page or a hand-built POST
    gets the same answers as the button. Raises AreaOverrideError.
    """
    reason = ' '.join((reason or '').split())
    if len(reason) < MIN_REASON_LENGTH:
        raise AreaOverrideError(
            f'Give a reason for overriding the delivery area '
            f'(at least {MIN_REASON_LENGTH} characters).'
        )
    if len(reason) > MAX_REASON_LENGTH:
        raise AreaOverrideError(
            f'The reason cannot be longer than {MAX_REASON_LENGTH} characters.'
        )
    with transaction.atomic():
        txn = SalesTransaction.objects.select_for_update().select_related('branch').get(pk=txn_pk)
        if not can_override(actor, txn):
            raise AreaOverrideError(
                'Only the Owner or this branch’s Manager can override the delivery area.'
            )
        state = area_state(txn)
        if not state['applies']:
            raise AreaOverrideError('Only an online delivery order has a delivery area.')
        if state['overridden']:
            raise AreaOverrideError(f'{txn.order_number} already has a delivery-area override.')
        if txn.order_status not in _OPEN:
            raise AreaOverrideError(f'{txn.order_number} is {txn.order_status}; it can no longer be overridden.')
        if not state['outside']:
            raise AreaOverrideError(
                f'{txn.order_number} is not outside the branch’s delivery areas, so '
                f'there is nothing to override.'
            )
        audit_context.set_action_label('Overrode a delivery area restriction')
        txn.area_override_reason = reason
        txn.area_override_by = actor
        txn.area_override_at = timezone.now()
        txn.save(update_fields=['area_override_reason', 'area_override_by', 'area_override_at'])
    return txn
