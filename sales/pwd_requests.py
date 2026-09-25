"""Customer PWD discount requests for online Pickup / Pay at Store orders.

One module, because four entry points have to agree exactly: the storefront
checkout, the DRF write serializer that mirrors it, the Order Console's review
form, and the API review endpoint next to it.

Request, not claim
------------------
F-04 removed self-service discounts from customer checkout: a typed ID number
proves nothing, and 'MADE-UP-123' used to buy a permanent 20%. That still
holds. Submitting a request records what the customer says and changes NOTHING
about the money — total_amount, the line prices and the billing record's
amount_due stay at the full price. The discount only exists once a named staff
member has seen the card at pick-up and approved it, and approval re-prices the
order through the same code path the Order Console's existing apply-discount
form uses (`apply_discount` below — that view now calls it too), so the two
cannot disagree on the arithmetic or the audit trail.

Until a Pending request is resolved, billing refuses to mark the order Paid
(BillingRecord.pwd_review_block_message). An order can only be Completed once
it is Paid, so "verify the ID before final billing" is enforced by the payment
gate rather than by hoping the cashier remembers.

Refunds (P14) are unaffected: approval is only possible while the bill is not
yet Paid, so by the time a refund can exist every SalesItem already carries its
final, discounted unit_price, which is what the refund arithmetic reads.
"""
import re
from decimal import Decimal

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from billing.models import BillingRecord

from .models import SalesTransaction
from .pricing import discount_applies_to, price_cart


class DiscountError(Exception):
    """A discount / request action that cannot go ahead. str(e) is the
    sentence to show the user, so the template views (flash message) and the
    API (400 body) report exactly the same wording."""


# Once money has actually changed hands (Paid, or refunded from having been
# Paid) a NEW discount has nothing left to apply against — the price was
# already settled. Shared by the Order Console's apply form, the request
# review, and the Order Console's decision about which forms to draw.
DISCOUNT_BLOCKED_BILLING_STATUSES = {
    BillingRecord.PaymentStatus.PAID,
    BillingRecord.PaymentStatus.PARTIALLY_REFUNDED,
    BillingRecord.PaymentStatus.REFUNDED,
}

PWD = SalesTransaction.DiscountType.PWD
Status = SalesTransaction.PwdRequestStatus

# What a PWD ID number may look like. Issuing LGUs format them differently
# (region-province-barangay-serial, or a plain serial), so this does not try to
# match one scheme — it rejects what is plainly not an ID: too short/long,
# characters no ID carries, or no digit at all ("MADE-UP" style filler).
_ID_NUMBER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 \-/]{3,48}[A-Za-z0-9]$')
# Letters (any script — "Peña") in words separated by spaces, dots, commas,
# apostrophes or hyphens: "Juan dela Cruz", "Ma. Cristina O'Neil", "Reyes Jr."
_NAME_RE = re.compile(r"^[^\W\d_]+(?:[ .,'\-]+[^\W\d_]+)*\.?$")


def validate_pwd_details(id_number, holder_name):
    """Normalise and validate the ID details typed at checkout or at review.

    Returns ``(id_number, holder_name, errors)``. `errors` is a list of
    sentences; empty means both fields are usable.
    """
    id_number = ' '.join((id_number or '').split()).upper()
    holder_name = ' '.join((holder_name or '').split())
    errors = []

    if not id_number:
        errors.append('Enter the PWD ID number.')
    elif not _ID_NUMBER_RE.match(id_number) or not any(c.isdigit() for c in id_number):
        errors.append(
            'That PWD ID number does not look valid — use the number printed '
            'on the card (letters, digits and dashes, 5 to 50 characters).'
        )

    if not holder_name:
        errors.append('Enter the name printed on the PWD ID.')
    elif len(holder_name) < 3 or len(holder_name) > 150 or not _NAME_RE.match(holder_name):
        errors.append('Enter the name on the PWD ID as printed (letters only).')

    return id_number, holder_name, errors


def request_can_reach_cart(products):
    """True when at least one of `products` is PWD-eligible. A request that
    could not move the total by a centavo is refused up front instead of
    leaving a pointless Pending request for staff to chase."""
    return any(discount_applies_to(p, PWD) for p in products)


def new_request_fields(id_number, holder_name):
    """Keyword arguments that record a fresh Pending request on a
    SalesTransaction being created. Deliberately touches no discount_* or
    total_* field — the order is priced in full."""
    return {
        'pwd_request_status': Status.PENDING,
        'pwd_request_id_number': id_number,
        'pwd_request_holder_name': holder_name,
        'pwd_requested_at': timezone.now(),
    }


def notify_pwd_request_submitted(txn):
    """Tell the branch there is an ID to check when this order is collected."""
    from core.module_permissions import SALES_POS
    from core.notifications import CATEGORY_SALES, PRIORITY_MEDIUM, notify

    notify(
        CATEGORY_SALES, PRIORITY_MEDIUM,
        f'PWD discount request — {txn.order_number}',
        f'{txn.order_number} ({txn.branch.branch_name}) asks for the PWD '
        f'discount. Verify the ID when the customer collects the order, '
        f'before confirming payment.',
        branch=txn.branch,
        action_url=reverse('transaction_detail', kwargs={'pk': txn.pk}),
        action_label='Review Request',
        required_module=SALES_POS,
        reference_type='sales_transaction', reference_id=txn.pk,
    )


def _notify_customer_of_decision(txn, title, message):
    from core.notifications import notify_customer

    notify_customer(
        txn.customer, title, message,
        action_url=reverse('orders:track_order', kwargs={'transaction_id': txn.pk}),
        action_label='Track Order',
    )


def _lock_for_discount(pk):
    """Lock the order and its bill and check that a NEW discount can still be
    applied to them. Must run inside an atomic block.

    Re-checked against the LOCKED rows, not the caller's earlier read, so two
    staff acting on one order at once cannot both pass the "no discount yet"
    check and compound the reduction, and a payment confirmed in the gap
    cannot be silently overwritten (billing_confirm takes the same lock, D-1).
    """
    locked = SalesTransaction.objects.select_for_update().get(pk=pk)
    billing = BillingRecord.objects.select_for_update().filter(
        transaction=locked
    ).first()

    if not locked.can_apply_pwd_senior_discount():
        raise DiscountError(
            f'{locked.order_number} is for delivery — a PWD / Senior Citizen '
            f'discount can only be applied in person, at the counter for a '
            f'walk-in or when a pickup order is collected.'
        )
    if locked.order_status == SalesTransaction.OrderStatus.CANCELLED:
        raise DiscountError(
            f'{locked.order_number} is cancelled — there is nothing to discount.'
        )
    if locked.discount_type != SalesTransaction.DiscountType.NONE:
        # Re-pricing already-discounted lines would take 20% off the
        # discounted price, compounding it. One claim per sale.
        raise DiscountError(
            f'{locked.order_number} already carries a '
            f'{locked.get_discount_type_display()} discount.'
        )
    if billing and billing.status in DISCOUNT_BLOCKED_BILLING_STATUSES:
        raise DiscountError(
            f'{locked.order_number} is already paid — the amount due cannot '
            f'be changed. Reverse the payment first if this discount is owed.'
        )
    return locked, billing


def _reprice_locked(locked, billing, discount_type, id_number, holder_name, actor):
    """Re-price a locked, undiscounted order and write the audit trail.

    Every line on an undiscounted order was stored at its original tier
    price, so SalesItem.unit_price is exactly the listed, VAT-inclusive
    figure price_cart expects. Passing it back in re-prices the frozen lines
    without re-deriving the tier, so a wholesale order stays at wholesale.
    VAT is stripped first, then 20% comes off the exclusive base (RA 10754 /
    RA 9994) — that arithmetic lives in sales.pricing and is not repeated here.
    """
    items = list(locked.items.select_related('product').all())
    priced = price_cart(
        [(i.product, i.quantity_sold, i.unit_price) for i in items],
        discount_type=discount_type,
    )

    for item, line in zip(items, priced['lines']):
        item.unit_price = line['unit_price']
        item.is_vat_exempt_line = line['is_vat_exempt_line']
        item.vat_amount = line['vat_amount']
        item.discount_amount = line['discount_amount']
        item.save()

    locked.discount_type = discount_type
    locked.discount_id_number = id_number
    locked.discount_holder_name = holder_name
    locked.total_amount = priced['total_amount']
    locked.total_vat_amount = priced['total_vat_amount']
    locked.total_discount_amount = priced['total_discount_amount']
    # Who granted this, and when — same shape as BillingRecord.confirmed_by /
    # date_confirmed (A-1).
    locked.discount_applied_by = actor
    locked.discount_applied_at = timezone.now()

    if billing:
        # The bill has to follow the total, or the customer is asked for the
        # pre-discount amount.
        billing.amount_due = priced['total_amount']
        billing.save()
    return priced


def apply_discount(pk, actor, discount_type, id_number, holder_name):
    """Staff-initiated PWD/Senior discount on an existing order (F-04's
    Order Console flow). Returns the price_cart result.

    Refused while the customer's own PWD request is Pending: that request has
    its own review, and applying around it would leave it Pending forever and
    the payment gate closed on an order that already has its discount.
    """
    with transaction.atomic():
        locked, billing = _lock_for_discount(pk)
        if locked.has_pending_pwd_request:
            raise DiscountError(
                f'{locked.order_number} has a PWD discount request waiting '
                f'for review — approve or reject it instead.'
            )
        priced = _reprice_locked(
            locked, billing, discount_type, id_number, holder_name, actor
        )
        locked.save()
    return priced


def approve_pwd_request(pk, actor, id_number, holder_name):
    """Approve the customer's Pending PWD request: the reviewer has seen the
    card. `id_number` / `holder_name` are what the reviewer confirms — they
    start from what the customer typed but may be corrected against the card,
    and are what discount_id_number / discount_holder_name record. The
    customer's own submission stays untouched on the pwd_request_* fields.

    Returns the price_cart result. Raises DiscountError.
    """
    id_number, holder_name, errors = validate_pwd_details(id_number, holder_name)
    if errors:
        raise DiscountError(' '.join(errors))

    with transaction.atomic():
        locked, billing = _lock_for_discount(pk)
        if not locked.has_pending_pwd_request:
            raise DiscountError(
                f'{locked.order_number} has no PWD discount request waiting '
                f'for review.'
            )
        priced = _reprice_locked(locked, billing, PWD, id_number, holder_name, actor)
        locked.pwd_request_status = Status.APPROVED
        locked.pwd_request_reviewed_by = actor
        locked.pwd_request_reviewed_at = timezone.now()
        locked.pwd_request_reject_reason = ''
        locked.save()

    if priced['total_discount_amount'] > Decimal('0'):
        _notify_customer_of_decision(
            locked,
            f'PWD discount approved — {locked.order_number}',
            f'Your PWD discount was verified. ₱{priced["total_discount_amount"]} '
            f'off — your order is now ₱{priced["total_amount"]}.',
        )
    return priced


def reject_pwd_request(pk, actor, reason):
    """Reject the Pending request (ID missing, invalid, or not the holder).
    The order stays exactly as priced — nothing was ever discounted — and
    payment can now be confirmed at the full amount. Raises DiscountError."""
    reason = ' '.join((reason or '').split())
    if not reason:
        raise DiscountError('Enter a reason for not approving the PWD discount.')
    if len(reason) > 255:
        raise DiscountError('Keep the reason under 255 characters.')

    with transaction.atomic():
        locked = SalesTransaction.objects.select_for_update().get(pk=pk)
        # Locked too, so the rejection cannot interleave with a payment
        # being confirmed (billing_confirm locks the same row, D-1).
        BillingRecord.objects.select_for_update().filter(transaction=locked).first()

        if not locked.has_pending_pwd_request:
            raise DiscountError(
                f'{locked.order_number} has no PWD discount request waiting '
                f'for review.'
            )
        locked.pwd_request_status = Status.REJECTED
        locked.pwd_request_reviewed_by = actor
        locked.pwd_request_reviewed_at = timezone.now()
        locked.pwd_request_reject_reason = reason
        locked.save()

    _notify_customer_of_decision(
        locked,
        f'PWD discount not approved — {locked.order_number}',
        f'The store could not verify your PWD discount: {reason} '
        f'Your order stays at the full price.',
    )
