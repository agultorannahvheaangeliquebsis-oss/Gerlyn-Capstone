from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from .models import BillingRecord
from sales.models import SalesTransaction
from accounts.models import User


@login_required(login_url='login')
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

    return render(request, 'billing/billing_list.html', {'records': records})


@login_required(login_url='login')
def billing_detail(request, pk):
    record = get_object_or_404(BillingRecord, pk=pk)
    return render(request, 'billing/billing_detail.html', {'record': record})


@login_required(login_url='login')
def billing_create(request, transaction_id):
    txn = get_object_or_404(SalesTransaction, pk=transaction_id)

    # Prevent duplicate billing records for the same transaction
    if hasattr(txn, 'billing_record'):
        messages.info(request, 'A billing record already exists for this transaction.')
        return redirect('billing_detail', pk=txn.billing_record.pk)

    if request.method == 'POST':
        payment_method = request.POST.get('payment_method')
        reference_number = request.POST.get('reference_number', '')
        proof_of_payment = request.FILES.get('proof_of_payment')

        record = BillingRecord.objects.create(
            transaction=txn,
            amount_due=txn.total_amount,
            payment_method=payment_method,
            reference_number=reference_number,
            proof_of_payment=proof_of_payment,
            status=BillingRecord.PaymentStatus.PENDING,
        )
        messages.success(request, 'Billing record created.')
        return redirect('billing_detail', pk=record.pk)

    return render(request, 'billing/billing_form.html', {'transaction': txn})


@login_required(login_url='login')
def billing_confirm(request, pk):
    record = get_object_or_404(BillingRecord, pk=pk)

    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'You do not have permission to confirm payments.')
        return redirect('billing_detail', pk=pk)

    if request.method == 'POST':
        new_status = request.POST.get('status')
        record.status = new_status
        record.confirmed_by = request.user
        record.date_confirmed = timezone.now()
        record.save()
        messages.success(request, f'Payment marked as {new_status}.')

    return redirect('billing_detail', pk=pk)