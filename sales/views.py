from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models as dj_models
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from core.permissions import BRANCH_MANAGER, INTERNAL_ROLES, OWNER, STAFF, role_required
from core.scoping import branch_ids_for_user, scope_queryset, user_can_access_branch
from inventory.models import Inventory, Product, StockMovement

from .models import SalesItem, SalesTransaction


def price_for(product, order_type):
    """
    The price to charge for a product on this kind of order.

    Chapter 1 describes Gerlyn as a retail AND wholesale business, so a
    Wholesale order bills at `wholesale_price`. Not every product has one —
    those fall back to the retail price rather than being sold at ₱0.
    """
    if order_type == SalesTransaction.OrderType.WHOLESALE and product.wholesale_price:
        return product.wholesale_price
    return product.unit_price


# ─────────────────────────────────────────────
# ORDER CONSOLE
# ─────────────────────────────────────────────

def _pos_user_branch(request):
    """Staff and managers are pinned to their own branch; the Owner picks one.
    Extracted verbatim from transaction_add so the Walk-in Sale modal resolves
    the branch exactly the way the POST handler will."""
    if request.user.role == STAFF:
        return getattr(getattr(request.user, 'staff_profile', None), 'branch', None)
    if request.user.role == BRANCH_MANAGER:
        return getattr(getattr(request.user, 'manager_profile', None), 'branch', None)
    return None


def _pos_context(request):
    """Everything the Walk-in Sale modal needs. Prefixed pos_* because this
    lands in the Order Console's context, which already has its own `branches`
    for the filter toolbar — a different list answering a different question."""
    user_branch = _pos_user_branch(request)
    return {
        'pos_products': Product.objects.select_related('category').all(),
        'pos_branches': Branch.objects.all() if not user_branch else [],
        'pos_user_branch': user_branch,
        'pos_payment_choices': [
            c for c in SalesTransaction.ModeOfPayment.choices
            if c[0] in ['Cash', 'GCash', 'Card']
        ],
        'pos_order_type_choices': SalesTransaction.OrderType.choices,
    }


def _transaction_list_context(request):
    transactions = (
        SalesTransaction.objects
        .select_related('branch', 'staff', 'customer')
        .prefetch_related('billing_record')
        .order_by('-transaction_date')
    )
    # Server-side branch scoping (never trust a UI dropdown alone).
    transactions = scope_queryset(transactions, request.user)

    search_query = request.GET.get('q', '')
    status_filter = request.GET.get('status', '')
    channel_filter = request.GET.get('channel', '')
    branch_filter = request.GET.get('branch', '')
    type_filter = request.GET.get('type', '')

    if search_query:
        transactions = transactions.filter(
            dj_models.Q(order_number__icontains=search_query) |
            dj_models.Q(customer__first_name__icontains=search_query) |
            dj_models.Q(customer__last_name__icontains=search_query) |
            dj_models.Q(customer__username__icontains=search_query)
        )
    if channel_filter:
        transactions = transactions.filter(transaction_type=channel_filter)
    if branch_filter:
        transactions = transactions.filter(branch_id=branch_filter)
    if type_filter:
        transactions = transactions.filter(order_type=type_filter)

    # Counts per tab are computed BEFORE the status filter is applied.
    base = transactions
    counts = {
        'all': base.count(),
        'pending': base.filter(order_status=SalesTransaction.OrderStatus.PENDING).count(),
        'approved': base.filter(order_status=SalesTransaction.OrderStatus.APPROVED).count(),
        'preparing': base.filter(order_status=SalesTransaction.OrderStatus.PREPARING).count(),
        'completed': base.filter(order_status=SalesTransaction.OrderStatus.COMPLETED).count(),
        'cancelled': base.filter(order_status=SalesTransaction.OrderStatus.CANCELLED).count(),
    }
    if status_filter:
        transactions = transactions.filter(order_status=status_filter)

    show_branch_filter = branch_ids_for_user(request.user) is None
    can_pos = request.user.role in [OWNER, BRANCH_MANAGER, STAFF]

    context = {
        'transactions': transactions,
        'counts': counts,
        'search_query': search_query,
        'status_filter': status_filter,
        'channel_filter': channel_filter,
        'branch_filter': branch_filter,
        'type_filter': type_filter,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'can_pos': can_pos,
    }
    if can_pos:
        context.update(_pos_context(request))
    return context


@login_required(login_url='accounts:login')
def transaction_list(request):
    """Order Console — the queue of every order, filterable by status."""
    if request.user.role not in INTERNAL_ROLES:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    return render(
        request, 'sales/transaction_list.html', _transaction_list_context(request)
    )


@login_required(login_url='accounts:login')
def transaction_detail(request, pk):
    txn = get_object_or_404(
        SalesTransaction.objects.select_related('branch', 'staff', 'customer'), pk=pk
    )
    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    items = txn.items.select_related('product').all()
    return render(request, 'sales/transaction_detail.html', {
        'transaction': txn,
        'items': items,
        'billing': getattr(txn, 'billing_record', None),
        'next_actions': _next_actions(txn),
    })


# ─────────────────────────────────────────────
# ORDER LIFECYCLE
# Pending → Approved → Preparing → Completed, or → Cancelled.
# Stock is deducted when an ONLINE order is COMPLETED (walk-in POS deducts at
# the point of sale instead), so stock never leaves twice.
# ─────────────────────────────────────────────

_FLOW = {
    SalesTransaction.OrderStatus.PENDING: ['Approved', 'Cancelled'],
    SalesTransaction.OrderStatus.APPROVED: ['Preparing', 'Cancelled'],
    SalesTransaction.OrderStatus.PREPARING: ['Completed', 'Cancelled'],
    SalesTransaction.OrderStatus.COMPLETED: [],
    SalesTransaction.OrderStatus.CANCELLED: [],
}


def _next_actions(txn):
    return _FLOW.get(txn.order_status, [])


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def transaction_set_status(request, pk, new_status):
    """Advance an order through its lifecycle."""
    txn = get_object_or_404(SalesTransaction, pk=pk)

    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    if request.method != 'POST':
        return redirect('transaction_detail', pk=pk)

    if new_status not in _next_actions(txn):
        messages.error(
            request,
            f'Cannot move {txn.order_number} from {txn.order_status} to {new_status}.'
        )
        return redirect('transaction_detail', pk=pk)

    with transaction.atomic():
        # Deduct stock once, when an online order is completed.
        if (new_status == SalesTransaction.OrderStatus.COMPLETED
                and txn.transaction_type == SalesTransaction.TransactionType.ONLINE):
            _deduct_stock(txn, request.user)

        txn.order_status = new_status
        txn.save()

    messages.success(request, f'{txn.order_number} is now {new_status}.')
    return redirect('transaction_detail', pk=pk)


def _deduct_stock(txn, user):
    """Deduct each line item from the order's branch floor stock + audit it."""
    for item in txn.items.select_related('product').all():
        inv = Inventory.objects.filter(
            product=item.product, branch=txn.branch
        ).first()
        if not inv:
            continue
        before = inv.quantity_on_hand
        inv.quantity_on_hand = max(0, inv.quantity_on_hand - item.quantity_sold)
        inv.save()
        StockMovement.objects.create(
            inventory=inv,
            location_type=StockMovement.LocationType.FLOOR,
            movement_type=StockMovement.MovementType.SALE,
            quantity_change=inv.quantity_on_hand - before,
            quantity_before=before,
            quantity_after=inv.quantity_on_hand,
            reference_id=txn.pk,
            notes=f'Order {txn.order_number}',
            created_by=user,
        )


# ─────────────────────────────────────────────
# WALK-IN POS
# ─────────────────────────────────────────────

@login_required(login_url='accounts:login')
def transaction_add(request):
    if request.user.role not in [OWNER, BRANCH_MANAGER, STAFF]:
        messages.error(request, 'You do not have permission to record sales.')
        return redirect('transaction_list')

    user_branch = _pos_user_branch(request)

    if request.method != 'POST':
        # Walk-in Sale is a modal on the Order Console now. A direct hit
        # (bookmark, back button) lands there with the modal open.
        return redirect(f"{reverse('transaction_list')}?add=1")

    branch_id = user_branch.id if user_branch else request.POST.get('branch')
    mode_of_payment = request.POST.get('mode_of_payment') or 'Cash'
    order_type = request.POST.get('order_type') or SalesTransaction.OrderType.RETAIL
    if order_type not in SalesTransaction.OrderType.values:
        order_type = SalesTransaction.OrderType.RETAIL
    product_ids = request.POST.getlist('product_id')
    quantities = request.POST.getlist('quantity')

    line_items = [
        (pid, int(q)) for pid, q in zip(product_ids, quantities)
        if pid and q and q.isdigit() and int(q) > 0
    ]

    # The same two checks, collected instead of redirected. A redirect threw
    # away every item row the cashier had already rung up.
    errors = []
    if not branch_id:
        errors.append('Please choose a branch for this sale.')
    if not line_items:
        errors.append('Add at least one item with a quantity.')

    if not errors:
        with transaction.atomic():
            txn = SalesTransaction.objects.create(
                branch_id=branch_id,
                staff=request.user if request.user.role == STAFF else None,
                transaction_type=SalesTransaction.TransactionType.PHYSICAL,
                mode_of_payment=mode_of_payment,
                order_type=order_type,
                order_status=SalesTransaction.OrderStatus.COMPLETED,  # walk-in = done
                total_amount=0,
            )

            total = 0
            for product_id, qty in line_items:
                product = Product.objects.get(pk=product_id)
                price = price_for(product, order_type)
                SalesItem.objects.create(
                    transaction=txn, product=product,
                    quantity_sold=qty, unit_price=price,
                )
                total += price * qty

            txn.total_amount = total
            txn.save()

            # Walk-in sales leave the shelf immediately.
            _deduct_stock(txn, request.user)

            # billing.signals already created the record; mark it paid.
            billing = getattr(txn, 'billing_record', None)
            if billing:
                billing.amount_due = total
                billing.payment_method = (
                    mode_of_payment if mode_of_payment in ['Cash', 'GCash', 'Card'] else 'Cash'
                )
                billing.status = 'Paid'
                billing.confirmed_by = request.user
                billing.save()

        messages.success(request, f'Sale {txn.order_number} recorded — ₱{total}.')
        return redirect('transaction_detail', pk=txn.pk)

    # Rejected. Re-render the console with the modal open, the header
    # selections re-chosen, and the item rows rebuilt from what was posted —
    # every pid/qty pair, including the ones that failed the digit check, so
    # a mistyped quantity comes back visible instead of vanishing.
    context = _transaction_list_context(request)
    context.update({
        'show_pos_modal': True,
        'pos_errors': errors,
        'pos_form_data': request.POST,
        'pos_restore_rows': [
            {'product_id': pid, 'quantity': qty}
            for pid, qty in zip(product_ids, quantities) if pid
        ],
    })
    return render(request, 'sales/transaction_list.html', context)
