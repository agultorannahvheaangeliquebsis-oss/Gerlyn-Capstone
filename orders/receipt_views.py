"""Customer-side delivery / pickup confirmation and receipt.

"Confirm I received my order" on Track Order, and the receipt (on screen, as a
PDF, and its evidence images) once the store has recorded the hand-over. The
rules live in sales.receipts; the staff side is sales.receipt_views.

Every view looks the order up by id AND customer=request.user, so another
customer's order is a 404 — it does not confirm the order exists.
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from sales import receipts
from sales.models import SalesTransaction

from .customer_views import customer_required


def _own_order(request, transaction_id):
    return get_object_or_404(
        SalesTransaction.objects.select_related(
            'branch', 'customer', 'receipt_confirmation', 'billing_record',
        ),
        id=transaction_id, customer=request.user,   # never someone else's
    )


@customer_required
@require_POST
def confirm_received(request, transaction_id):
    """The customer says they have the order."""
    sale = _own_order(request, transaction_id)
    try:
        conf = receipts.record_customer_confirmation(sale.pk, request.user)
    except receipts.ReceiptError as exc:
        messages.error(request, str(exc))
    else:
        if conf.is_fully_confirmed:
            messages.success(request, f'Thanks! {sale.order_number} is now fully confirmed.')
        else:
            messages.success(
                request,
                f'Thanks! We recorded that you received {sale.order_number}.',
            )
    return redirect('orders:track_order', transaction_id=sale.pk)


def _receipt_or_redirect(request, transaction_id):
    """(order, confirmation, None), or (None, None, redirect) when the store
    has not recorded the hand-over yet and so there is no receipt."""
    sale = _own_order(request, transaction_id)
    conf = receipts.confirmation_of(sale)
    if not receipts.can_view_receipt(conf):
        messages.error(request, 'There is no receipt for this order yet.')
        return None, None, redirect('orders:track_order', transaction_id=sale.pk)
    return sale, conf, None


@customer_required
def order_receipt(request, transaction_id):
    """The receipt, on screen."""
    sale, conf, bail = _receipt_or_redirect(request, transaction_id)
    if bail:
        return bail
    context = receipts.receipt_context(sale, conf)
    context.update({
        'pdf_url_name': 'orders:receipt_pdf',
        'evidence_url_name': 'orders:receipt_evidence',
        'evidence_args': [sale.pk],
    })
    return render(request, 'orders/receipt.html', context)


@customer_required
def order_receipt_pdf(request, transaction_id):
    sale, conf, bail = _receipt_or_redirect(request, transaction_id)
    if bail:
        return bail
    try:
        return receipts.pdf_response(sale, conf)
    except receipts.ReceiptError as exc:
        messages.error(request, str(exc))
        return redirect('orders:track_order', transaction_id=sale.pk)


@customer_required
@require_GET
def order_receipt_evidence(request, transaction_id, kind):
    """Stream the photo or signature recorded on the customer's own order."""
    sale = _own_order(request, transaction_id)
    return receipts.evidence_response(receipts.confirmation_of(sale), kind)
