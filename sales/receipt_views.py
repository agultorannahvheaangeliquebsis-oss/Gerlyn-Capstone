"""Staff-side views for an online order's delivery / pickup receipt.

Record the hand-over (a form with an optional photo and a drawn signature),
open the receipt, download it as a PDF, and stream the evidence images. The
rules live in sales.receipts; the customer's own confirm / receipt pages are in
orders.receipt_views and call the same service.

Every view here is internal-only and branch-scoped on the server, the way
transaction_detail is: a Customer or Supplier following a stale link is sent
home with one accurate message, and an order from another branch is refused.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from audit import context as audit_context
from core.permissions import INTERNAL_ROLES
from core.people import person_display_name
from core.scoping import user_can_access_branch

from . import receipts
from .models import SalesTransaction


def _internal_order(request, pk):
    """(order, None) for an order this user may work on, else (None, redirect).

    The same two refusals transaction_detail makes, in the same words.
    """
    if request.user.role not in INTERNAL_ROLES:
        messages.error(
            request,
            "That page is part of the internal Order Console — it isn't available to your account.",
        )
        return None, redirect('accounts:home')
    txn = get_object_or_404(
        SalesTransaction.objects.select_related(
            'branch', 'customer', 'receipt_confirmation', 'billing_record',
        ),
        pk=pk,
    )
    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return None, redirect('transaction_list')
    return txn, None


def _local_input_value(moment):
    """A datetime in the browser's <input type="datetime-local"> format."""
    return timezone.localtime(moment).strftime('%Y-%m-%dT%H:%M')


def _form_context(txn, billing, form, error=''):
    return {
        'transaction': txn,
        'billing': billing,
        'items': txn.items.select_related('product'),
        'customer_name': person_display_name(txn.customer) or '—',
        'hand_over_label': receipts.hand_over_label(txn),
        'confirm_label': receipts.confirm_action_label(txn),
        'payment_block': receipts.payment_block_reason(txn, billing),
        # Already Completed: the form only records the hand-over late.
        'already_completed': txn.order_status == SalesTransaction.OrderStatus.COMPLETED,
        'form': form,
        'error': error,
        'placed_value': _local_input_value(txn.transaction_date),
        'proof_max_mb': settings.PROOF_OF_PAYMENT_MAX_MB,
    }


@login_required(login_url='accounts:login')
def transaction_confirm_receipt(request, pk):
    """Record that an order was handed over (GET: the form, POST: save it)."""
    txn, bail = _internal_order(request, pk)
    if bail:
        return bail
    billing = getattr(txn, 'billing_record', None)
    conf = receipts.confirmation_of(txn)

    blocked = receipts.staff_block_reason(txn, billing, conf)
    if blocked:
        messages.error(request, blocked)
        return redirect('transaction_detail', pk=pk)

    if request.method != 'POST':
        form = {
            'received_by_name': person_display_name(txn.customer),
            'received_at': _local_input_value(timezone.now()),
            'notes': '',
            'signature_data': '',
        }
        return render(request, 'sales/receipt_confirm_form.html',
                      _form_context(txn, billing, form))

    form = {
        'received_by_name': request.POST.get('received_by_name', ''),
        'received_at': request.POST.get('received_at', ''),
        'notes': request.POST.get('notes', ''),
        'signature_data': request.POST.get('signature_data', ''),
    }
    # Names the whole request's audit rows ("Confirmed an order hand-over")
    # instead of the first row's raw field diff. Reset when the request ends.
    audit_context.set_action_label('Confirmed an order hand-over')
    completed_now = txn.order_status == SalesTransaction.OrderStatus.PREPARING
    try:
        conf = receipts.record_staff_confirmation(
            pk, request.user,
            received_by_name=form['received_by_name'],
            received_at_raw=form['received_at'],
            notes=form['notes'],
            proof_photo=request.FILES.get('proof_photo'),
            signature_data=form['signature_data'],
        )
    except receipts.ReceiptError as exc:
        # Re-read: if the order is no longer confirmable (someone else got
        # there first) a form that cannot succeed is worse than a message.
        txn = SalesTransaction.objects.select_related(
            'branch', 'customer', 'receipt_confirmation', 'billing_record',
        ).get(pk=pk)
        billing = getattr(txn, 'billing_record', None)
        if receipts.staff_block_reason(txn, billing, receipts.confirmation_of(txn)):
            messages.error(request, str(exc))
            return redirect('transaction_detail', pk=pk)
        return render(request, 'sales/receipt_confirm_form.html',
                      _form_context(txn, billing, form, error=str(exc)))

    messages.success(
        request,
        f'{txn.order_number} confirmed as received. Receipt {conf.receipt_number} issued'
        + (' and the order is now Completed.' if completed_now else '.'),
    )
    return redirect('transaction_detail', pk=pk)


def _receipt_for_view(request, pk):
    """(order, confirmation, None) for a receipt that exists and may be read,
    else (None, None, response)."""
    txn, bail = _internal_order(request, pk)
    if bail:
        return None, None, bail
    conf = receipts.confirmation_of(txn)
    if not receipts.can_view_receipt(conf):
        messages.error(request, f'{txn.order_number} has no receipt yet — the hand-over has not been confirmed.')
        return None, None, redirect('transaction_detail', pk=pk)
    return txn, conf, None


@login_required(login_url='accounts:login')
def transaction_delivery_receipt(request, pk):
    """The delivery / pickup receipt, on screen."""
    txn, conf, bail = _receipt_for_view(request, pk)
    if bail:
        return bail
    context = receipts.receipt_context(txn, conf)
    context.update({
        'pdf_url_name': 'transaction_delivery_receipt_pdf',
        'evidence_url_name': 'transaction_receipt_evidence',
        'back_url_name': 'transaction_detail',
        'evidence_args': [txn.pk],
    })
    return render(request, 'sales/delivery_receipt.html', context)


@login_required(login_url='accounts:login')
def transaction_delivery_receipt_pdf(request, pk):
    """The same receipt as a downloadable PDF."""
    txn, conf, bail = _receipt_for_view(request, pk)
    if bail:
        return bail
    try:
        return receipts.pdf_response(txn, conf)
    except receipts.ReceiptError as exc:
        messages.error(request, str(exc))
        return redirect('transaction_detail', pk=pk)


@login_required(login_url='accounts:login')
@require_GET
def transaction_receipt_evidence(request, pk, kind):
    """Stream the proof photo or signature, to staff of the order's branch only."""
    if request.user.role not in INTERNAL_ROLES:
        raise Http404
    txn = get_object_or_404(SalesTransaction, pk=pk)
    if not user_can_access_branch(request.user, txn.branch_id):
        raise Http404
    return receipts.evidence_response(receipts.confirmation_of(txn), kind)
