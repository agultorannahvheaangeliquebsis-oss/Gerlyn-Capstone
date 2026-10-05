from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from .models import BillingRecord
from sales.models import SalesTransaction
from accounts.models import User
from branches.models import Branch
from core.scoping import branch_for_user, user_can_access_branch
from core.permissions import OWNER, BRANCH_MANAGER, STAFF, CUSTOMER, SUPPLIER, role_required
from core.pagination import paginate


@role_required(OWNER, BRANCH_MANAGER, STAFF, CUSTOMER, SUPPLIER)
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

    search_query = request.GET.get('q', '')
    if search_query:
        records = records.filter(
            Q(transaction__order_number__icontains=search_query) |
            Q(reference_number__icontains=search_query)
        )

    # Owner-only branch filter — Manager/Staff are already pinned to their
    # own branch's records above, so a selector would do nothing for them.
    user_branch = branch_for_user(request.user)
    branch_filter = request.GET.get('branch', '') if request.user.role == User.Role.OWNER else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''
    if branch_filter:
        records = records.filter(transaction__branch_id=branch_filter)

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

    branches = Branch.objects.all() if request.user.role == User.Role.OWNER else []

    page_obj = paginate(request, records)

    return render(request, 'billing/billing_list.html', {
        'records': records,
        'page_obj': page_obj,
        'counts': counts,
        'status_filter': status_filter,
        'outstanding': outstanding,
        'search_query': search_query,
        'branches': branches,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Warehouse Stock, Product Master List, Stock Adjustments, Receive
        # Delivery and the Order Console.
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        'user_branch': user_branch,
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
        # The Pending card offers Mark as Paid / Reject; a Rejected record's
        # card offers Re-open (back to Pending). Anything else is a tampered
        # POST and is rejected outright rather than persisted as an arbitrary
        # string that would quietly break status-based filters.
        offered = {
            BillingRecord.PaymentStatus.PAID,
            BillingRecord.PaymentStatus.REJECTED,
            BillingRecord.PaymentStatus.PENDING,
        }
        if new_status not in offered:
            messages.error(request, 'Invalid payment status.')
            return redirect('billing_detail', pk=pk)

        with transaction.atomic():
            # Re-fetch and lock so two confirmations racing the same record
            # (double-click, retry) can't both pass the transition check
            # against the same stale, pre-lock status.
            locked = BillingRecord.objects.select_for_update().select_related(
                'transaction'
            ).get(pk=pk)

            # A-1 — maker/checker separation. Whether this user may act on
            # the record at all is logically prior to what they're moving it
            # to, so this runs before the transition check (same ordering as
            # C-6's branch-scope gate). Only a decision that records a
            # confirmer — Paid or Rejected — is gated; re-opening a rejected
            # record to Pending clears confirmed_by and has nothing to guard.
            if (
                new_status != BillingRecord.PaymentStatus.PENDING
                and locked.is_self_confirmation(request.user)
            ):
                messages.error(
                    request,
                    "You can't confirm a payment you recorded yourself — ask "
                    "another staff member, a manager, or the owner."
                )
                return redirect('billing_detail', pk=pk)

            if not locked.can_transition_to(new_status):
                messages.error(
                    request,
                    f'{locked.transaction.order_number}: a {locked.status} '
                    f'payment cannot be changed to {new_status}.'
                )
                return redirect('billing_detail', pk=pk)

            locked.status = new_status
            if new_status == BillingRecord.PaymentStatus.PENDING:
                # Re-opening a rejected payment — clear the prior decision so
                # the record reads as genuinely awaiting a fresh check, not
                # as still "confirmed by / on" the rejection.
                locked.confirmed_by = None
                locked.date_confirmed = None
            else:
                locked.confirmed_by = request.user
                locked.date_confirmed = timezone.now()
            locked.save()

        verb = 're-opened' if new_status == BillingRecord.PaymentStatus.PENDING else f'marked as {new_status}'
        messages.success(request, f'{record.transaction.order_number} payment {verb}.')

    return redirect('billing_detail', pk=pk)