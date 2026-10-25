"""Delivery / pickup receipt confirmation for online orders.

When an online order changes hands the system keeps proof of it: who took it,
when, which staff member handed it over, and optionally a photo and a drawn
signature. The customer can add their own confirmation, and either side can
open a receipt (HTML or PDF) afterwards.

Stage names
-----------
The order lifecycle has no separate "Out for delivery" or "Ready for pickup"
status: Pending -> Approved -> Preparing -> Completed / Cancelled. Preparing is
the stage where an order is packed and waiting to be handed over (My Orders
already calls it "To receive"), so that is what these two names stand for here,
told apart by the order's fulfilment type. Nothing about the lifecycle changes.

Who writes what
---------------
This module is the only writer of OrderReceiptConfirmation. Staff and customer
each confirm at most once, and both confirmations are written under the
order's row lock (the same lock transaction_set_status takes), so two taps, or
a staff confirmation racing a status change, cannot both go through.

Staff confirmation also moves the order Preparing -> Completed, through the
very routine the Order Detail status control uses
(sales.views._apply_status_transition), so the payment gate, the exactly-once
stock deduction and the history rows are the same ones, not a copy. If any of
that refuses, nothing is recorded: no receipt number is burned and no
half-confirmed order is left behind.

This is the ONLY way an online order is completed: sales.views._next_actions
no longer offers Completed for one, so the status control cannot finish an
online order without recording who received it.

Orders completed before that rule (or by anyone who got there first) have no
hand-over on record. Staff may record it afterwards: for an order that is
already Completed the confirmation changes nothing about the order — no status
step, no stock movement, no payment check (all of that already happened) — and
just issues the receipt.
"""
import base64
import binascii
import io
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models as dj_models
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from billing.models import BillingRecord
from core.limits import MAX_NAME_LENGTH, MAX_NOTE_LENGTH, parse_text
from core.people import person_display_name
from core.permissions import INTERNAL_ROLES
from core.scoping import user_can_access_branch

from .models import (
    OrderReceiptConfirmation, ReceiptNumberSequence, SalesTransaction,
)
from .pricing import price_line

Status = SalesTransaction.OrderStatus
ReceiptStatus = OrderReceiptConfirmation.Status

# received_at may be a little behind the moment the form is submitted (a rider
# logging a hand-over once they are back at the branch) but never ahead of it.
CLOCK_SKEW = timedelta(minutes=5)

# A drawn signature is a small PNG; anything bigger is not one.
SIGNATURE_MAX_BYTES = 300 * 1024
SIGNATURE_MAX_PIXELS = 2000

_DATA_URL_PREFIX = 'data:image/png;base64,'

# The formats validate_proof_photo lets through, and the extension each is
# stored under (which is also what evidence_response serves it as).
_PHOTO_EXTENSIONS = {'jpeg': 'jpg', 'png': 'png', 'webp': 'webp'}
_EVIDENCE_CONTENT_TYPES = {
    '.jpg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
}
EVIDENCE_KINDS = {'photo': 'proof_photo', 'signature': 'signature'}


class ReceiptError(Exception):
    """A confirmation that cannot go ahead. str(e) is the sentence to show the
    user, so every caller reports the same wording."""


# ---------------------------------------------------------------------------
# Rules: what may be confirmed, by whom, and when
# ---------------------------------------------------------------------------

def hand_over_label(txn):
    """What the Preparing stage is called for this order's fulfilment type."""
    if txn.fulfillment_type == SalesTransaction.FulfillmentType.DELIVERY:
        return 'Out for delivery'
    return 'Ready for pickup'


def confirm_action_label(txn):
    return (
        'Confirm Delivery'
        if txn.fulfillment_type == SalesTransaction.FulfillmentType.DELIVERY
        else 'Confirm Receipt'
    )


def confirmation_of(txn):
    """The order's OrderReceiptConfirmation, or None."""
    try:
        return txn.receipt_confirmation
    except OrderReceiptConfirmation.DoesNotExist:
        return None


def receipt_applies(txn):
    """Whether an order is the kind that carries a hand-over confirmation at
    all. Walk-in sales are handed over across the counter and already print a
    receipt there; a cancelled order never reaches anybody. Drives the
    Confirmation column, which reads "—" for the rest."""
    return (
        txn.transaction_type == SalesTransaction.TransactionType.ONLINE
        and txn.order_status != Status.CANCELLED
    )


def is_refunded(txn, billing):
    """Any refund against the order, approved or reflected on its bill."""
    if txn.refunded_amount and txn.refunded_amount > 0:
        return True
    return bool(billing and billing.status in (
        BillingRecord.PaymentStatus.REFUNDED,
        BillingRecord.PaymentStatus.PARTIALLY_REFUNDED,
    ))


def _common_block_reason(txn, billing):
    if txn.transaction_type != SalesTransaction.TransactionType.ONLINE:
        return 'Only online orders have a delivery or pickup confirmation.'
    if txn.order_status == Status.CANCELLED:
        return f'{txn.order_number} was cancelled, so it cannot be confirmed.'
    if is_refunded(txn, billing):
        return f'{txn.order_number} has been refunded, so it cannot be confirmed.'
    return None


def staff_block_reason(txn, billing, conf):
    """Why staff cannot record this order's hand-over right now (None = they
    can). Payment is judged separately — see payment_block_reason.

    Open while the order is out for delivery / ready for pickup (Preparing),
    and for an order that is already Completed but has no hand-over on record."""
    reason = _common_block_reason(txn, billing)
    if reason:
        return reason
    if conf is not None and conf.staff_confirmed:
        return f'{txn.order_number} has already been confirmed as received.'
    if txn.order_status not in (Status.PREPARING, Status.COMPLETED):
        return (
            f'{txn.order_number} is {txn.order_status}. It can be confirmed once '
            f'it is {hand_over_label(txn).lower()} (Preparing).'
        )
    return None


def payment_block_reason(txn, billing):
    """The payment rule for completing the order, as the page-level message.
    The same gate _apply_status_transition enforces when the form is posted.
    None for an order that is already Completed: there is no step left to
    gate, and its payment was judged when it was completed."""
    if txn.order_status == Status.COMPLETED:
        return None
    if billing is None or billing.status != BillingRecord.PaymentStatus.PAID:
        return (
            'Payment must be verified as Paid before this order can be marked '
            'received — record and verify the payment first.'
        )
    return None


def customer_block_reason(txn, billing, conf):
    """Why the customer cannot confirm this order right now (None = they can).

    Open while the order is out for delivery / ready for pickup, and also
    after staff have recorded the hand-over — that is what lets the two
    confirmations meet when staff go first.
    """
    reason = _common_block_reason(txn, billing)
    if reason:
        return reason
    if conf is not None and conf.customer_confirmed:
        return 'You have already confirmed receiving this order.'
    handed_over_by_staff = (
        txn.order_status == Status.COMPLETED and conf is not None and conf.staff_confirmed
    )
    if txn.order_status != Status.PREPARING and not handed_over_by_staff:
        return (
            f'You can confirm once your order is {hand_over_label(txn).lower()}.'
        )
    return None


def can_view_receipt(conf):
    """A receipt exists only once staff have recorded the hand-over — that is
    when its number is issued and the order is completed."""
    return conf is not None and conf.staff_confirmed


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------

def next_receipt_number(when=None):
    """GP-RCPT-YYYYMMDD-NNNNN for the local day of `when`.

    Must be called inside the transaction that writes the confirmation: the
    day's counter row is locked until it commits, so concurrent hand-overs
    cannot share a number and a rolled-back one returns its number.
    """
    day = timezone.localdate(when or timezone.now())
    seq, _ = ReceiptNumberSequence.objects.select_for_update().get_or_create(day=day)
    seq.last_value += 1
    seq.save(update_fields=['last_value'])
    return f'GP-RCPT-{day:%Y%m%d}-{seq.last_value:05d}'


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def parse_received_at(raw, *, order_placed_at, now=None):
    """The hand-over time the staff member entered, as an aware datetime.

    Blank means "now". Anything in the future, or from before the order was
    placed, is refused rather than quietly clamped.
    """
    now = now or timezone.now()
    raw = (raw or '').strip()
    if not raw:
        return now
    parsed = parse_datetime(raw)
    if parsed is None:
        raise ReceiptError('Enter the date and time received in a valid format.')
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    if parsed > now + CLOCK_SKEW:
        raise ReceiptError('The time received cannot be in the future.')
    if parsed < order_placed_at:
        raise ReceiptError('The time received cannot be before the order was placed.')
    return parsed


def decode_signature(data_url):
    """A drawn signature (the canvas's PNG data URL) as a ContentFile, or None
    when nothing was drawn.

    The value comes from the browser, so it is treated as untrusted input:
    only a base64 PNG data URL is accepted, it is size-capped, and Pillow must
    agree it is a PNG of sane dimensions with ink on it.
    """
    data_url = (data_url or '').strip()
    if not data_url:
        return None
    if not data_url.startswith(_DATA_URL_PREFIX):
        raise ReceiptError('The signature could not be read. Clear it and sign again.')
    try:
        raw = base64.b64decode(data_url[len(_DATA_URL_PREFIX):], validate=True)
    except (binascii.Error, ValueError):
        raise ReceiptError('The signature could not be read. Clear it and sign again.')
    if len(raw) > SIGNATURE_MAX_BYTES:
        raise ReceiptError('The signature image is too large. Clear it and sign again.')

    from PIL import Image
    try:
        image = Image.open(io.BytesIO(raw))
        image.verify()
        image = Image.open(io.BytesIO(raw))
        if image.format != 'PNG':
            raise ValueError('not a PNG')
        width, height = image.size
        if not (0 < width <= SIGNATURE_MAX_PIXELS and 0 < height <= SIGNATURE_MAX_PIXELS):
            raise ValueError('bad dimensions')
        rgba = image.convert('RGBA')
    except Exception:
        raise ReceiptError('The signature could not be read. Clear it and sign again.')
    # An untouched canvas exports a fully transparent PNG; that is "no
    # signature", not a signature.
    if rgba.getchannel('A').getbbox() is None:
        return None
    return ContentFile(raw, name='signature.png')


def validate_proof_photo(upload):
    """Same real-image check as a payment proof: a renamed .html or .svg must
    not be stored and served back under its own content type."""
    if not upload:
        return None
    from PIL import Image

    from billing.views import _validate_proof_of_payment
    try:
        _validate_proof_of_payment(upload)
        upload.seek(0)
        verified_format = Image.open(upload).format.lower()
        upload.seek(0)
    except ValidationError as exc:
        raise ReceiptError('Proof photo: ' + ' '.join(exc.messages))
    # The file is later served back under a content type taken from its name,
    # so the name must say what Pillow found, not what the uploader typed — a
    # JPEG called photo.html must not be served as a web page.
    upload.name = 'proof-photo.' + _PHOTO_EXTENSIONS[verified_format]
    return upload


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def record_staff_confirmation(
    txn_id, actor, *, received_by_name, received_at_raw='', notes='',
    proof_photo=None, signature_data='',
):
    """Staff record that the order was handed over. Returns the confirmation.

    Issues the receipt number, moves the order Preparing -> Completed (payment
    rules permitting), writes the audit entries and notifies the customer. For
    an order that is already Completed (a late recording) the order itself is
    left alone and only the receipt is issued.
    Raises ReceiptError, leaving no trace, when any of that cannot happen.
    """
    # Deferred: the lifecycle machinery (and the stock routines it calls) lives
    # in sales.views, which imports this module for the list filter.
    from . import views as sales_views
    from .notifications import notify_receipt_confirmed
    from audit.capture import record_detail

    name, error = parse_text(
        received_by_name, 'Received by', max_length=MAX_NAME_LENGTH, required=True,
    )
    if error:
        raise ReceiptError(error)
    notes, error = parse_text(notes, 'Notes', max_length=MAX_NOTE_LENGTH)
    if error:
        raise ReceiptError(error)
    photo = validate_proof_photo(proof_photo)
    signature = decode_signature(signature_data)

    if actor.role not in INTERNAL_ROLES:
        raise ReceiptError('Only staff can record a hand-over.')

    with transaction.atomic():
        # Plain lock on the order row alone: a select_related() here would make
        # MySQL lock the branch and customer rows too.
        txn = SalesTransaction.objects.select_for_update().get(pk=txn_id)
        if not user_can_access_branch(actor, txn.branch_id):
            raise ReceiptError('That order belongs to another branch.')
        billing = BillingRecord.objects.filter(transaction=txn).first()
        conf = (
            OrderReceiptConfirmation.objects.select_for_update()
            .filter(transaction=txn).first()
        )
        reason = staff_block_reason(txn, billing, conf)
        if reason:
            raise ReceiptError(reason)

        now = timezone.now()
        received_at = parse_received_at(
            received_at_raw, order_placed_at=txn.transaction_date, now=now,
        )

        receipt_number = next_receipt_number(now)

        # Complete the order first: it is the step that can refuse (payment not
        # Paid, stock short), and it must do so before anything is recorded.
        # An order that is already Completed has nothing left to do here.
        completing = txn.order_status == Status.PREPARING
        if completing:
            try:
                sales_views._apply_status_transition(
                    txn, Status.COMPLETED, actor,
                    f'Hand-over confirmed — receipt {receipt_number}.',
                    notify_customer_of_status=False,
                )
            except sales_views._OrderActionError as exc:
                raise ReceiptError(str(exc))

        conf = conf or OrderReceiptConfirmation(transaction=txn)
        conf.receipt_number = receipt_number
        conf.received_by_name = name
        conf.received_at = received_at
        conf.confirmed_by = actor
        conf.staff_confirmed_at = now
        conf.notes = notes
        if photo:
            conf.proof_photo = photo
        if signature:
            conf.signature = signature
        conf.save()

        # The create/update rows the audit signals write only carry field
        # diffs; this is the sentence an auditor reads on the order itself.
        who = actor.role.replace('_', ' ').title()
        record_detail(
            txn,
            f'{txn.order_number} hand-over confirmed by {who} '
            f'(receipt {conf.receipt_number}); order moved to Completed.'
            if completing else
            f'{txn.order_number} hand-over recorded by {who} after the order was '
            f'completed (receipt {conf.receipt_number}); order status unchanged.',
        )
        notify_receipt_confirmed(txn, conf, actor, by_customer=False)
    return conf


def record_customer_confirmation(txn_id, user):
    """The customer confirms they received their order. Returns the
    confirmation. Raises ReceiptError when they cannot."""
    from .notifications import notify_receipt_confirmed
    from audit.capture import record_detail

    with transaction.atomic():
        txn = SalesTransaction.objects.select_for_update().get(pk=txn_id)
        # Ownership is re-checked under the lock; the view has already turned
        # anyone else's order into a 404.
        if user.role != 'CUSTOMER' or txn.customer_id != user.pk:
            raise ReceiptError('That order does not belong to your account.')
        billing = BillingRecord.objects.filter(transaction=txn).first()
        conf = (
            OrderReceiptConfirmation.objects.select_for_update()
            .filter(transaction=txn).first()
        )
        reason = customer_block_reason(txn, billing, conf)
        if reason:
            raise ReceiptError(reason)

        conf = conf or OrderReceiptConfirmation(transaction=txn)
        conf.customer_confirmed_at = timezone.now()
        conf.customer_confirmed_by = user
        conf.save()

        record_detail(
            txn,
            f'{txn.order_number} receipt confirmed by the customer'
            + (' (now fully confirmed).' if conf.is_fully_confirmed else '.'),
        )
        notify_receipt_confirmed(txn, conf, user, by_customer=True)
    return conf


# ---------------------------------------------------------------------------
# The Order Console's Confirmation column and filter
# ---------------------------------------------------------------------------

def filter_by_receipt_status(queryset, code):
    """Narrow an order queryset to one OrderReceiptConfirmation.Status.

    "Not confirmed" means online orders still waiting on both sides — a
    walk-in sale or a cancelled order has nothing to confirm, so it belongs to
    no option rather than to "Not confirmed". Unknown codes change nothing.
    """
    staff = dj_models.Q(receipt_confirmation__staff_confirmed_at__isnull=False)
    customer = dj_models.Q(receipt_confirmation__customer_confirmed_at__isnull=False)
    if code == ReceiptStatus.NONE:
        return queryset.filter(
            transaction_type=SalesTransaction.TransactionType.ONLINE,
        ).exclude(order_status=Status.CANCELLED).exclude(staff | customer)
    if code == ReceiptStatus.STAFF:
        return queryset.filter(staff).exclude(customer)
    if code == ReceiptStatus.CUSTOMER:
        return queryset.filter(customer).exclude(staff)
    if code == ReceiptStatus.FULL:
        return queryset.filter(staff & customer)
    return queryset


# ---------------------------------------------------------------------------
# The receipt document
# ---------------------------------------------------------------------------

def price_basis(item, discount_type):
    """'Wholesale', 'Retail', or '' when it cannot be told, for one order line.

    SalesItem freezes only the price paid, not which price ladder rung it came
    from, so this replays the line's pricing against the product's current
    ladder: the deepest bulk tier the quantity reaches, then retail, using the
    same price_line the order was written with (so a PWD/Senior line compares
    like with like). A product repriced since the sale matches neither and
    reads '' instead of a guess.
    """
    product = item.product
    if product is None:
        return ''
    reached = [
        tier for tier in product.price_tiers.all()
        if item.quantity_sold >= tier.min_quantity
    ]
    candidates = []
    if reached:
        candidates.append(('Wholesale', max(reached, key=lambda t: t.min_quantity).unit_price))
    candidates.append(('Retail', product.unit_price))
    for label, listed in candidates:
        priced = price_line(
            product, item.quantity_sold, unit_price=listed, discount_type=discount_type,
        )
        if priced['unit_price'] == item.unit_price:
            return label
    return ''


def receipt_context(txn, conf):
    """Everything the on-screen receipt and the PDF show, in one place so the
    two cannot disagree about what a receipt says."""
    from core.models import StoreSetting

    items = list(txn.items.select_related('product').prefetch_related('product__price_tiers'))
    for item in items:
        item.price_basis = price_basis(item, txn.discount_type)
    billing = getattr(txn, 'billing_record', None)
    discount = txn.total_discount_amount or Decimal('0')
    return {
        'transaction': txn,
        'conf': conf,
        'store': StoreSetting.load(),
        'billing': billing,
        'items': items,
        'gross_subtotal': txn.total_amount + discount,
        'discount_amount': discount,
        'payment_method': billing.payment_method if billing else txn.mode_of_payment,
        'payment_status': billing.status if billing else '',
        'staff_name': person_display_name(conf.confirmed_by),
        'customer_name': person_display_name(txn.customer),
        'hand_over_label': hand_over_label(txn),
        'is_delivery': txn.fulfillment_type == SalesTransaction.FulfillmentType.DELIVERY,
        # A receipt outlives a void: the goods did change hands, so the
        # document stays readable, but it must say the sale was reversed.
        'voided': txn.order_status == Status.CANCELLED,
    }


def _image_data_uri(field_file, *, max_edge, fmt):
    """A stored evidence image as a data URI sized for a PDF page, or ''.

    xhtml2pdf cannot reach private storage by URL, so the bytes are inlined.
    The photo is shrunk first — a phone camera frame would otherwise add
    several MB to a one-page receipt. '' on any failure: the receipt then
    says the image is on file rather than failing to render at all.
    """
    if not field_file:
        return ''
    try:
        from PIL import Image
        with field_file.open('rb') as handle:
            image = Image.open(handle)
            image.load()
        image.thumbnail((max_edge, max_edge))
        out = io.BytesIO()
        if fmt == 'JPEG':
            image.convert('RGB').save(out, 'JPEG', quality=80)
            mime = 'image/jpeg'
        else:
            # Flatten onto white: a transparent signature would render black.
            rgba = image.convert('RGBA')
            flat = Image.new('RGB', rgba.size, 'white')
            flat.paste(rgba, mask=rgba.getchannel('A'))
            flat.save(out, 'PNG')
            mime = 'image/png'
        return f'data:{mime};base64,{base64.b64encode(out.getvalue()).decode("ascii")}'
    except Exception:
        return ''


def render_receipt_pdf(txn, conf):
    """The receipt as PDF bytes. Raises ReceiptError if it cannot be built."""
    from xhtml2pdf import pisa

    context = receipt_context(txn, conf)
    context['signature_src'] = _image_data_uri(conf.signature, max_edge=600, fmt='PNG')
    context['photo_src'] = _image_data_uri(conf.proof_photo, max_edge=700, fmt='JPEG')
    html = render_to_string('sales/receipt_pdf.html', context)

    buffer = io.BytesIO()
    result = pisa.CreatePDF(src=html, dest=buffer)
    if result.err:
        raise ReceiptError('The PDF receipt could not be generated.')
    return buffer.getvalue()


def pdf_response(txn, conf):
    """The receipt PDF as a download. Raises ReceiptError if it cannot be built."""
    response = HttpResponse(render_receipt_pdf(txn, conf), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{conf.receipt_number}.pdf"'
    return response


def evidence_response(conf, kind):
    """Stream one evidence image ('photo' or 'signature'), or raise Http404.

    The caller has already decided the requester may see this order; this only
    refuses what does not exist. 404 rather than 403 throughout, so a missing
    file and a forbidden one look the same from outside.
    """
    field_name = EVIDENCE_KINDS.get(kind)
    if conf is None or field_name is None:
        raise Http404
    field_file = getattr(conf, field_name)
    if not field_file:
        raise Http404
    extension = '.' + field_file.name.rsplit('.', 1)[-1].lower()
    try:
        handle = field_file.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    return FileResponse(
        handle, content_type=_EVIDENCE_CONTENT_TYPES.get(extension, 'application/octet-stream'),
    )


def staff_panel(txn, billing, can_edit):
    """What the staff Order Detail page needs to draw its hand-over card.

    `can_edit` is the caller's Sales/Order Console Edit permission. Display
    only — the confirm view and record_staff_confirmation judge it again.
    """
    conf = confirmation_of(txn)
    applies = txn.transaction_type == SalesTransaction.TransactionType.ONLINE
    return {
        'applies': applies,
        'can_edit': can_edit,
        'conf': conf,
        'status': conf.status if conf else ReceiptStatus.NONE,
        'hand_over_label': hand_over_label(txn),
        'confirm_label': confirm_action_label(txn),
        'can_confirm': (
            applies and can_edit and staff_block_reason(txn, billing, conf) is None
        ),
        'payment_block': payment_block_reason(txn, billing),
        # Completed with no hand-over on record: the button records it late.
        'already_completed': txn.order_status == Status.COMPLETED,
        'has_receipt': can_view_receipt(conf),
        # Resolved here: confirmed_by is NULL once that account is removed,
        # and a template filter argument would raise on it.
        'confirmed_by_name': person_display_name(conf.confirmed_by) if conf else '',
    }


def customer_panel(txn):
    """What the customer's Track Order page needs to draw its confirmation
    card. Display only — record_customer_confirmation judges it again."""
    billing = getattr(txn, 'billing_record', None)
    conf = confirmation_of(txn)
    return {
        'conf': conf,
        'status': conf.status if conf else ReceiptStatus.NONE,
        'hand_over_label': hand_over_label(txn),
        'can_confirm': customer_block_reason(txn, billing, conf) is None,
        'has_receipt': can_view_receipt(conf),
    }
