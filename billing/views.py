from django.db.models import Sum
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from .models import BillingRecord
from sales.models import SalesTransaction
from accounts.models import User
from core.scoping import user_can_access_branch
from core.permissions import OWNER, BRANCH_MANAGER, CUSTOMER, SUPPLIER, role_required


@role_required(OWNER, BRANCH_MANAGER, CUSTOMER, SUPPLIER)
def billing_list(request):
    records = BillingRecord.objects.select_related('transaction', 'transaction__branch').all()

    # Branch managers/staff only see their own branch's billing records
    if request.user.role in [User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        try:
            branch = request.user.manager_profile.branch if request.user.role == User.Role.BRANCH_MANAGER \
                else request.user.staff_profile.branch
            records = records.filter(transaction__branch=branch)
        except Exception:
            records = records.none()

    # Customers only see their own billing records
    elif request.user.role == User.Role.CUSTOMER:
        records = records.filter(transaction__customer=request.user)

    # Suppliers have no business in the store's billing at all.
    elif request.user.role == User.Role.SUPPLIER:
        records = records.none()

    records = records.order_by('-date_recorded')

    status_filter = request.GET.get('status', '')
    counts = {
        'all': records.count(),
        'pending': records.filter(status=BillingRecord.PaymentStatus.PENDING).count(),
        'paid': records.filter(status=BillingRecord.PaymentStatus.PAID).count(),
        'rejected': records.filter(status=BillingRecord.PaymentStatus.REJECTED).count(),
    }
    if status_filter:
        records = records.filter(status=status_filter)

    outstanding = records.filter(
        status=BillingRecord.PaymentStatus.PENDING
    ).aggregate(total=Sum('amount_due'))['total'] or 0

    return render(request, 'billing/billing_list.html', {
        'records': records,
        'counts': counts,
        'status_filter': status_filter,
        'outstanding': outstanding,
    })


@login_required(login_url='accounts:login')
def billing_detail(request, pk):
    record = get_object_or_404(
        BillingRecord.objects.select_related('transaction', 'transaction__branch'),
        pk=pk,
    )

    # The list is scoped, so the detail must be too — otherwise another branch's
    # billing record is readable just by changing the URL.
    if request.user.role == User.Role.CUSTOMER:
        allowed = record.transaction.customer_id == request.user.id
    else:
        allowed = user_can_access_branch(request.user, record.transaction.branch_id)

    if not allowed:
        messages.error(request, 'That billing record is not available to you.')
        return redirect('billing_list')

    return render(request, 'billing/billing_detail.html', {'record': record})


@login_required(login_url='accounts:login')
def billing_create(request, transaction_id):
    txn = get_object_or_404(SalesTransaction, pk=transaction_id)

    # Same scoping rule as billing_detail — otherwise anyone logged in could
    # create or edit the payment record for someone else's transaction just
    # by guessing the transaction id in the URL.
    if request.user.role == User.Role.CUSTOMER:
        allowed = txn.customer_id == request.user.id
    else:
        allowed = user_can_access_branch(request.user, txn.branch_id)

    if not allowed:
        messages.error(request, 'That transaction is not available to you.')
        return redirect('billing_list')

    # The signal may have already created a pending record.
    # If it exists and is still Pending, let the user fill in payment details.
    # If it's already Paid/Rejected, just show the existing record.
    existing = getattr(txn, 'billing_record', None)

    if existing and existing.status != BillingRecord.PaymentStatus.PENDING:
        messages.info(request, 'A billing record already exists for this transaction.')
        return redirect('billing_detail', pk=existing.pk)

    if request.method == 'POST':
        payment_method = request.POST.get('payment_method')
        reference_number = request.POST.get('reference_number', '')
        proof_of_payment = request.FILES.get('proof_of_payment')

        if existing:
            # Update the auto-created record with the actual payment details
            existing.payment_method = payment_method
            existing.reference_number = reference_number
            if proof_of_payment:
                existing.proof_of_payment = proof_of_payment
            existing.save()
            record = existing
        else:
            # Fallback: create fresh if somehow the signal didn't fire
            record = BillingRecord.objects.create(
                transaction=txn,
                amount_due=txn.total_amount,
                payment_method=payment_method,
                reference_number=reference_number,
                proof_of_payment=proof_of_payment,
                status=BillingRecord.PaymentStatus.PENDING,
            )

        messages.success(request, 'Billing record saved.')
        return redirect('billing_detail', pk=record.pk)

    # This page has two front doors — the order's own detail page, and the
    # billing record's "Edit payment details" / "Upload proof". It used to
    # carry one hardcoded back-link to the order, so anyone who arrived from
    # Billing was quietly moved into Sales & Orders on the way back, while
    # the page header still said "Billing".
    #
    # The entry links state where they came from. A `from` flag rather than a
    # `?next=<url>`: this only ever chooses between two known routes, and a
    # flag cannot be pointed at an arbitrary destination, so there is nothing
    # to validate. Anything unrecognised falls through to the order, which is
    # the older of the two paths and the safe default.
    #
    # Three levels deep with a record in the middle is the case DESIGN.md's
    # breadcrumb rule reserves the component for: the section and the record
    # are both invisible otherwise.
    if request.GET.get('from') == 'billing' and existing is not None:
        trail = [
            ('Billing', reverse('billing_list')),
            (txn.order_number, reverse('billing_detail', args=[existing.pk])),
        ]
    else:
        trail = [
            ('Sales & Orders', reverse('transaction_list')),
            (txn.order_number, reverse('transaction_detail', args=[txn.pk])),
        ]

    return render(request, 'billing/billing_form.html', {
        'transaction': txn,
        'record': existing,  # Pass to template so it can pre-fill payment_method if desired
        'payment_choices': BillingRecord.PaymentMethod.choices,
        'breadcrumb': trail,
    })


@login_required(login_url='accounts:login')
def billing_confirm(request, pk):
    record = get_object_or_404(
        BillingRecord.objects.select_related('transaction'), pk=pk
    )

    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'You do not have permission to confirm payments.')
        return redirect('billing_detail', pk=pk)

    # Same scoping rule as billing_detail/billing_create — without this, any
    # Staff/Manager could confirm or reject a payment belonging to another
    # branch just by knowing the record id.
    if not user_can_access_branch(request.user, record.transaction.branch_id):
        messages.error(request, 'That billing record is not available to you.')
        return redirect('billing_list')

    if request.method == 'POST':
        new_status = request.POST.get('status')
        # This form only ever offers Mark as Paid / Reject — anything else
        # (a tampered POST) is rejected outright rather than persisted as an
        # arbitrary string that would quietly break status-based filters.
        if new_status not in (BillingRecord.PaymentStatus.PAID, BillingRecord.PaymentStatus.REJECTED):
            messages.error(request, 'Invalid payment status.')
            return redirect('billing_detail', pk=pk)
        record.status = new_status
        record.confirmed_by = request.user
        record.date_confirmed = timezone.now()
        record.save()
        messages.success(request, f'Payment marked as {new_status}.')

    return redirect('billing_detail', pk=pk)