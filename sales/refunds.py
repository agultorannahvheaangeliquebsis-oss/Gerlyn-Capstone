"""The refund workflow's rules — request, approve, reject — in one place.

Both the Order Console views (sales.views) and the DRF endpoints
(sales.api_views) call these functions, so permissions, branch scoping,
validation, quantity reservation and the stock / money side effects cannot
drift between the two surfaces.

Lifecycle (see sales.models.Refund):

    request_refund   -> Pending   (reserves quantities, moves nothing)
    approve_refund   -> Approved  (restocks Sellable lines, adds to
                                   SalesTransaction.refunded_amount, steps the
                                   billing record — one transaction)
    reject_refund    -> Rejected  (releases the reservation, moves nothing)

An Owner/Branch Manager raising a refund themselves is approved in the same
transaction (SELF_APPROVE_ROLES); both events are still written.

Locking order is always: order row, billing row, refund row, then stock rows.
Every writer here takes the order lock first, so two racing requests or
decisions on the same order serialise instead of double-processing.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.db import models as dj_models
from django.db import transaction
from django.utils import timezone

from billing.models import BillingRecord
from core.permissions import BRANCH_MANAGER, OWNER, STAFF
from core.scoping import user_can_access_branch
from inventory.models import Batch, Inventory, StockMovement

from .models import Refund, RefundEvent, RefundItem, SalesItem, SalesTransaction

REQUESTER_ROLES = (OWNER, BRANCH_MANAGER, STAFF)
DECIDER_ROLES = (OWNER, BRANCH_MANAGER)
# Roles whose own request is approved as it is submitted — the trust A-1 gives
# Owner/Branch Manager over sales they rang up themselves. Staff never are.
SELF_APPROVE_ROLES = (OWNER, BRANCH_MANAGER)

# The billing states a refund may be raised or approved against.
REFUNDABLE_BILLING_STATUSES = (
    BillingRecord.PaymentStatus.PAID,
    BillingRecord.PaymentStatus.PARTIALLY_REFUNDED,
)

MAX_TEXT = 255


class RefundError(Exception):
    """A refused refund action. The message is written for the person who
    attempted it and is safe to show as-is."""


class RefundForbidden(RefundError):
    """The actor's role or branch does not allow this action at all."""


@dataclass
class RefundOutcome:
    refund: Refund
    # Sellable units that could not go back onto the shelf because their
    # product has since been deleted from the catalog.
    unrestockable: int = 0

    @property
    def approved(self):
        return self.refund.status == Refund.Status.APPROVED


def item_label(item):
    """A SalesItem's product name, safe against a product that was hard-
    deleted from the catalog since the sale (SalesItem.product is SET_NULL
    precisely so the sale survives that)."""
    return item.product.product_name if item.product_id else 'a product since removed from the catalog'


def not_eligible_message(txn):
    return (
        f'{txn.order_number} is not eligible for a refund right now — it must be '
        f'Completed, Paid, and within {txn.REFUND_WINDOW.days} days of completion.'
    )


# ---------------------------------------------------------------------------
# What may be refunded, and by whom
# ---------------------------------------------------------------------------

def can_request_refund(txn, billing, user):
    """Whether `user` may raise a refund on `txn` right now. The single
    definition the order page asks to decide what to draw and the request
    path re-asks against locked rows to decide what to allow."""
    return (
        user.role in REQUESTER_ROLES
        and user_can_access_branch(user, txn.branch_id)
        and txn.order_status == SalesTransaction.OrderStatus.COMPLETED
        and billing is not None
        # Partially Refunded still has refundable quantity left on it — an
        # order already refunded once must stay refundable for what remains.
        and billing.status in REFUNDABLE_BILLING_STATUSES
        and not txn.refund_window_expired()
    )


def can_decide_refund(user, txn):
    return user.role in DECIDER_ROLES and user_can_access_branch(user, txn.branch_id)


def refundable_lines(txn):
    """This order's items that still have quantity nobody has claimed, each
    with how much remains. "Claimed" is Approved AND Pending — a request that
    is waiting for a decision holds its units, so the same ones can't be
    requested twice; a Rejected request holds nothing."""
    items = txn.items.select_related('product').annotate(
        reserved=dj_models.Sum(
            'refund_items__quantity',
            filter=dj_models.Q(refund_items__refund__status__in=Refund.RESERVING_STATUSES),
        )
    )
    rows = []
    for item in items:
        remaining = item.quantity_sold - (item.reserved or 0)
        if remaining > 0:
            rows.append({'item': item, 'remaining': remaining, 'product_label': item_label(item)})
    return rows


def _units_not_refunded(txn):
    """Units on the order not covered by an APPROVED refund — what decides
    Partially Refunded vs Refunded on the billing record. Pending requests do
    not count: no money has moved for them."""
    sold = txn.items.aggregate(n=dj_models.Sum('quantity_sold'))['n'] or 0
    refunded = RefundItem.objects.filter(
        refund__transaction=txn, refund__status=Refund.Status.APPROVED
    ).aggregate(n=dj_models.Sum('quantity'))['n'] or 0
    return sold - refunded


def has_approved_refunds(txn):
    return txn.refunds.filter(status=Refund.Status.APPROVED).exists()


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

def _clean_text(value):
    return (value or '').strip()


def request_refund(txn_id, user, *, reason_code, reason, refund_method,
                   lines=None, refund_all=False, conditions=None,
                   default_condition=None):
    """Raise a refund on an order.

    `lines` is a list of dicts {'item_id', 'quantity', 'condition'}. With
    `refund_all` it is ignored and every line's whole remaining quantity is
    refunded instead, each in the condition `conditions` (item id -> value)
    names, falling back to `default_condition`. Every value here is untrusted
    client input — amounts, types and statuses are all derived, never taken.

    Returns a RefundOutcome; raises RefundError / RefundForbidden.
    """
    if user.role not in REQUESTER_ROLES:
        raise RefundForbidden('Your account cannot request refunds.')

    reason_code = _clean_text(reason_code)
    reason = _clean_text(reason)
    refund_method = _clean_text(refund_method)
    if reason_code not in Refund.ReasonCode.values:
        raise RefundError('Choose a reason for this refund.')
    if reason_code == Refund.ReasonCode.OTHER and not reason:
        raise RefundError('Explain the reason — "Other" needs a note.')
    if len(reason) > MAX_TEXT:
        raise RefundError(f'Keep the note to {MAX_TEXT} characters or fewer.')
    if refund_method not in Refund.RefundMethod.values:
        raise RefundError('Choose how the money was given back.')

    with transaction.atomic():
        # Re-fetch and lock so two requests racing the same order can't both
        # pass the remaining-quantity check against the same stale read.
        locked_txn = SalesTransaction.objects.select_for_update().get(pk=txn_id)
        if not user_can_access_branch(user, locked_txn.branch_id):
            raise RefundForbidden('That order belongs to another branch.')
        billing = BillingRecord.objects.select_for_update().filter(
            transaction=locked_txn
        ).first()
        if not can_request_refund(locked_txn, billing, user):
            raise RefundError(not_eligible_message(locked_txn))

        sales_items = {
            i.pk: i for i in
            SalesItem.objects.select_for_update().select_related('product')
            .filter(transaction=locked_txn)
        }
        reserved = dict(
            RefundItem.objects.filter(
                refund__transaction=locked_txn,
                refund__status__in=Refund.RESERVING_STATUSES,
            ).values('sales_item_id').annotate(n=dj_models.Sum('quantity'))
            .values_list('sales_item_id', 'n')
        )
        remaining = {pk: i.quantity_sold - reserved.get(pk, 0) for pk, i in sales_items.items()}
        if not any(r > 0 for r in remaining.values()):
            raise RefundError(
                f'Every unit on {locked_txn.order_number} is already refunded or '
                f'waiting for a refund decision.'
            )

        resolved = []  # (SalesItem, quantity, condition)
        if refund_all:
            conditions = conditions or {}
            for pk, item in sales_items.items():
                if remaining[pk] <= 0:
                    continue
                condition = conditions.get(str(pk)) or default_condition or ''
                if condition not in RefundItem.Condition.values:
                    raise RefundError(f'Choose a condition for {item_label(item)}.')
                resolved.append((item, remaining[pk], condition))
        else:
            if not lines:
                raise RefundError('Select at least one item to refund.')
            seen = set()
            for line in lines:
                item_id = line.get('item_id')
                if item_id in seen:
                    # Each duplicate would re-read the same pre-loop "remaining"
                    # figure, letting the same units be claimed more than once
                    # in a single request.
                    raise RefundError(
                        'That item was selected more than once — refund it in a single line.'
                    )
                seen.add(item_id)
                item = sales_items.get(item_id)
                if item is None:
                    raise RefundError('That item does not belong to this order.')
                quantity = line.get('quantity')
                if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                    raise RefundError(f'Enter a valid quantity for {item_label(item)}.')
                if line.get('condition') not in RefundItem.Condition.values:
                    raise RefundError(f'Choose a condition for {item_label(item)}.')
                if quantity > remaining[item_id]:
                    raise RefundError(
                        f'Only {remaining[item_id]} unit(s) of {item_label(item)} '
                        f'can still be refunded on this order.'
                    )
                resolved.append((item, quantity, line['condition']))

        total = sum((q * i.unit_price for i, q, _ in resolved), Decimal('0'))
        claimed = RefundItem.objects.filter(
            refund__transaction=locked_txn, refund__status__in=Refund.RESERVING_STATUSES,
        ).aggregate(n=dj_models.Sum('amount'))['n'] or Decimal('0')
        if claimed + total > locked_txn.total_amount:
            raise RefundError(
                f'This would refund more than {locked_txn.order_number} was charged '
                f'(₱{locked_txn.total_amount}).'
            )

        units_sold = sum(i.quantity_sold for i in sales_items.values())
        refund_type = (
            Refund.RefundType.FULL
            if sum(q for _, q, _ in resolved) == units_sold
            else Refund.RefundType.PARTIAL
        )
        refund = Refund.objects.create(
            transaction=locked_txn, status=Refund.Status.PENDING,
            reason_code=reason_code, reason=reason, refund_type=refund_type,
            refund_method=refund_method, total_amount=total, requested_by=user,
        )
        for item, quantity, condition in resolved:
            RefundItem.objects.create(
                refund=refund, sales_item=item, quantity=quantity,
                condition=condition, amount=quantity * item.unit_price,
            )
        RefundEvent.objects.create(
            refund=refund, from_status='', to_status=Refund.Status.PENDING, actor=user,
            note=f'{refund.get_refund_type_display()} requested — {refund.get_reason_code_display()}'
                 + (f': {reason}' if reason else ''),
        )

        unrestockable = 0
        if user.role in SELF_APPROVE_ROLES:
            unrestockable = _apply_approval(
                refund, locked_txn, billing, user, 'Approved on submission by the requester.'
            )

    outcome = RefundOutcome(refund, unrestockable)
    from .notifications import notify_refund_decided, notify_refund_requested

    if outcome.approved:
        notify_refund_decided(refund, user)
    else:
        notify_refund_requested(refund, user)
    return outcome


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------

def _restock_refunded_item(sales_item, txn, quantity, user, refund):
    """The stock side of a Sellable-condition refund line: back onto the
    branch's floor. A fresh, no-expiry batch — the honestly-unknown-provenance
    fallback the void reversal also uses — since a unit that has left the
    store and come back has no traceable original lot to credit.

    Callers must not call this for a line whose product has since been
    deleted (sales_item.product_id is None); there is no Inventory row to
    identify.
    """
    inv, _ = Inventory.objects.select_for_update().get_or_create(
        product=sales_item.product, branch=txn.branch,
        defaults={'quantity_on_hand': 0, 'reorder_level': 0},
    )
    before = inv.quantity_on_hand
    inv.quantity_on_hand += quantity
    inv.save()
    batch = Batch.objects.create(
        product=sales_item.product, branch=txn.branch, location=Batch.Location.FLOOR,
        quantity=quantity, received_date=timezone.localdate(), expiry_date=None,
        notes=f'Customer return for {txn.order_number} — refund #{refund.pk}, marked sellable.',
    )
    StockMovement.objects.create(
        inventory=inv,
        location_type=StockMovement.LocationType.FLOOR,
        movement_type=StockMovement.MovementType.CUSTOMER_RETURN,
        quantity_change=inv.quantity_on_hand - before,
        quantity_before=before,
        quantity_after=inv.quantity_on_hand,
        reference_id=txn.pk,
        batch=batch,
        notes=f'Refund #{refund.pk} on {txn.order_number} — '
              f'{item_label(sales_item)} returned sellable.',
        created_by=user,
    )


def _apply_approval(refund, txn, billing, actor, note=''):
    """Everything approval does, against rows the caller already holds locked
    (order, billing, and the refund itself). Returns the number of Sellable
    units that could not be restocked. Raises RefundError if the order is no
    longer in a state that can be refunded — the caller's atomic block then
    rolls the whole approval back."""
    if refund.status != Refund.Status.PENDING:
        raise RefundError(f'Refund #{refund.pk} was already {refund.status.lower()}.')
    if (
        txn.order_status != SalesTransaction.OrderStatus.COMPLETED
        or billing is None
        or billing.status not in REFUNDABLE_BILLING_STATUSES
    ):
        raise RefundError(
            f'{txn.order_number} can no longer be refunded — it is not Completed and Paid. '
            f'Reject this request instead.'
        )

    unrestockable = 0
    for line in refund.items.select_related('sales_item__product'):
        if line.condition != RefundItem.Condition.SELLABLE:
            continue  # Damaged / unsellable: written off, stock untouched.
        if line.sales_item.product_id is None:
            # Nowhere to restock to (Inventory requires a product) — the
            # refund is still recorded, the money was genuinely paid.
            unrestockable += line.quantity
        else:
            _restock_refunded_item(line.sales_item, txn, line.quantity, actor, refund)

    refund.status = Refund.Status.APPROVED
    refund.refunded_by = actor
    refund.refunded_at = timezone.now()
    refund.save(update_fields=['status', 'refunded_by', 'refunded_at'])

    txn.refunded_amount = txn.refunded_amount + refund.total_amount
    txn.save(update_fields=['refunded_amount'])

    payment_before = billing.status
    new_billing_status = (
        BillingRecord.PaymentStatus.REFUNDED
        if _units_not_refunded(txn) == 0
        else BillingRecord.PaymentStatus.PARTIALLY_REFUNDED
    )
    if billing.can_transition_to(new_billing_status):
        billing.status = new_billing_status
        billing.save(update_fields=['status'])

    RefundEvent.objects.create(
        refund=refund, from_status=Refund.Status.PENDING, to_status=Refund.Status.APPROVED,
        actor=actor,
        note=(f'{note} ' if note else '') + f'Payment: {payment_before} -> {billing.status}.',
    )
    return unrestockable


def _lock_for_decision(refund_id, actor):
    """Lock order, billing and refund (in that order) and check the actor may
    decide it. Must be called inside transaction.atomic()."""
    txn_id = Refund.objects.values_list('transaction_id', flat=True).get(pk=refund_id)
    txn = SalesTransaction.objects.select_for_update().select_related('branch').get(pk=txn_id)
    if not can_decide_refund(actor, txn):
        raise RefundForbidden('Only the Owner or a Branch Manager of this branch can decide a refund.')
    billing = BillingRecord.objects.select_for_update().filter(transaction=txn).first()
    refund = Refund.objects.select_for_update().get(pk=refund_id)
    return txn, billing, refund


def approve_refund(refund_id, actor, note=''):
    note = _clean_text(note)
    if len(note) > MAX_TEXT:
        raise RefundError(f'Keep the note to {MAX_TEXT} characters or fewer.')
    with transaction.atomic():
        txn, billing, refund = _lock_for_decision(refund_id, actor)
        unrestockable = _apply_approval(refund, txn, billing, actor, note)
    from .notifications import notify_refund_decided

    notify_refund_decided(refund, actor)
    return RefundOutcome(refund, unrestockable)


def _mark_rejected(refund, actor, note):
    refund.status = Refund.Status.REJECTED
    refund.decision_note = note
    refund.save(update_fields=['status', 'decision_note'])
    RefundEvent.objects.create(
        refund=refund, from_status=Refund.Status.PENDING, to_status=Refund.Status.REJECTED,
        actor=actor, note=note,
    )


def reject_refund(refund_id, actor, note):
    note = _clean_text(note)
    if not note:
        raise RefundError('Enter a reason for rejecting this refund.')
    if len(note) > MAX_TEXT:
        raise RefundError(f'Keep the note to {MAX_TEXT} characters or fewer.')
    with transaction.atomic():
        txn, billing, refund = _lock_for_decision(refund_id, actor)
        if refund.status != Refund.Status.PENDING:
            raise RefundError(f'Refund #{refund.pk} was already {refund.status.lower()}.')
        _mark_rejected(refund, actor, note)
    from .notifications import notify_refund_decided

    notify_refund_decided(refund, actor)
    return RefundOutcome(refund)


def reject_pending_refunds(locked_txn, actor, note):
    """Close every Pending request on an order that is being voided — their
    goods are being reversed wholesale, so there is nothing left to approve.
    The caller holds the order lock. Returns how many were closed."""
    pending = list(
        Refund.objects.select_for_update().filter(
            transaction=locked_txn, status=Refund.Status.PENDING
        )
    )
    for refund in pending:
        _mark_rejected(refund, actor, note)
    return len(pending)
