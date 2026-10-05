from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models as dj_models
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.models import User
from billing.models import BillingRecord
from branches.models import Branch
from core.permissions import BRANCH_MANAGER, INTERNAL_ROLES, OWNER, STAFF, role_required
from core.scoping import branch_for_user, branch_ids_for_user, scope_queryset, user_can_access_branch
from core.timebounds import local_month_bounds
from core.pagination import paginate
from core.validation import MAX_QUANTITY, parse_int_field
from inventory.alerts import stock_status
from inventory.models import Batch, Inventory, Product, StockMovement

from .models import SalesItem, SalesReturn, SalesReturnItem, SalesTransaction
from .pricing import price_cart, PWD_SENIOR_DISCOUNT_RATE, VAT_RATE


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
    Shared by transaction_add's GET (rendering the Walk-in Sale form) and its
    POST handler, so both resolve the branch the same way."""
    if request.user.role == STAFF:
        return getattr(getattr(request.user, 'staff_profile', None), 'branch', None)
    if request.user.role == BRANCH_MANAGER:
        return getattr(getattr(request.user, 'manager_profile', None), 'branch', None)
    return None


def _pos_stock_map(user_branch):
    """product_id -> branch_id -> {qty, status}, for every branch the
    cashier could possibly ring this sale under: just their own pinned
    branch for Staff/Branch Manager, or every branch for the Owner (who
    picks one via #pos-branch). Keyed by both ids as strings since that's
    how they round-trip through json_script/JS.

    Sourced from Inventory (floor stock) — Walk-in Sale sells off the
    shelf, never the warehouse — and status is inventory.alerts.stock_status(),
    the same tiering the Floor Stock list and Alerts Center already use, so
    the indicator can't drift out of sync with what those screens call
    "low stock".
    """
    qs = Inventory.objects.all()
    if user_branch:
        qs = qs.filter(branch_id=user_branch.id)
    stock_map = {}
    for inv in qs.values('product_id', 'branch_id', 'quantity_on_hand', 'reorder_level'):
        stock_map.setdefault(str(inv['product_id']), {})[str(inv['branch_id'])] = {
            'qty': inv['quantity_on_hand'],
            'status': stock_status(inv['quantity_on_hand'], inv['reorder_level']),
        }
    return stock_map


def _pos_context(request):
    """Everything the Walk-in Sale page needs. Prefixed pos_* to distinguish
    it from unrelated fields elsewhere (e.g. the Order Console's own
    `branches` for its filter toolbar — a different list answering a
    different question)."""
    user_branch = _pos_user_branch(request)
    return {
        'pos_products': Product.objects.select_related('category').all(),
        'pos_branches': Branch.objects.all() if not user_branch else [],
        'pos_user_branch': user_branch,
        'pos_stock_map': _pos_stock_map(user_branch),
        'pos_payment_choices': [
            c for c in SalesTransaction.ModeOfPayment.choices
            if c[0] in ['Cash', 'GCash', 'Card']
        ],
        'pos_order_type_choices': SalesTransaction.OrderType.choices,
        # Only the two real claims — "None" is the absence of a claim, and
        # rendering it as a third radio invites picking it as if it were one.
        'pos_discount_choices': [
            c for c in SalesTransaction.DiscountType.choices
            if c[0] != SalesTransaction.DiscountType.NONE
        ],
        'pos_discount_rate_pct': int(PWD_SENIOR_DISCOUNT_RATE * 100),
        'pos_vat_rate_pct': int(VAT_RATE * 100),
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
    branches = Branch.objects.all() if show_branch_filter else []

    context = {
        'transactions': transactions,
        'page_obj': paginate(request, transactions),
        'counts': counts,
        'search_query': search_query,
        'status_filter': status_filter,
        'channel_filter': channel_filter,
        'branch_filter': branch_filter,
        'type_filter': type_filter,
        'branches': branches,
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Warehouse Stock, Product Master List, Stock Adjustments and
        # Receive Delivery.
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        'user_branch': branch_for_user(request.user),
        'can_pos': can_pos,
    }
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
        'next_actions': _next_actions(txn, request.user),
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
    # Completed -> Cancelled is the void path: unlike cancelling from any
    # earlier stage (stock was never touched), this has to reverse the stock
    # _deduct_stock already applied. Gated to Owner/Branch Manager only
    # inside transaction_set_status — Staff can cancel an unfulfilled order
    # but not undo a completed sale.
    SalesTransaction.OrderStatus.COMPLETED: ['Cancelled'],
    SalesTransaction.OrderStatus.CANCELLED: [],
}


def _next_actions(txn, user=None):
    actions = _FLOW.get(txn.order_status, [])
    if (txn.order_status == SalesTransaction.OrderStatus.COMPLETED
            and user is not None and user.role not in [OWNER, BRANCH_MANAGER]):
        # Staff can cancel an unfulfilled order but not void a completed
        # one — don't offer a button transaction_set_status would reject.
        actions = [a for a in actions if a != 'Cancelled']
    return actions


class _OrderActionError(Exception):
    """Raised inside an atomic block (invalid transition, or _deduct_stock
    hitting insufficient stock) so the whole block rolls back — including
    any earlier line items already deducted in the same loop — instead of
    partially applying a rejected action."""


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def transaction_set_status(request, pk, new_status):
    """Advance an order through its lifecycle."""
    txn = get_object_or_404(SalesTransaction, pk=pk)

    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    if request.method != 'POST':
        return redirect('transaction_detail', pk=pk)

    try:
        with transaction.atomic():
            # Re-fetch and lock so a second, concurrent POST advancing the
            # same order (double-click, retry) can't also pass this guard
            # against the same stale, pre-lock read — and can't both reach
            # _deduct_stock for the same order.
            locked_txn = SalesTransaction.objects.select_for_update().get(pk=pk)

            if new_status not in _next_actions(locked_txn):
                raise _OrderActionError(
                    f'Cannot move {locked_txn.order_number} from '
                    f'{locked_txn.order_status} to {new_status}.'
                )

            was_completed = locked_txn.order_status == SalesTransaction.OrderStatus.COMPLETED

            if was_completed and new_status == SalesTransaction.OrderStatus.CANCELLED:
                # Voiding a completed sale — undo what _deduct_stock already
                # applied. Distinct from cancelling an earlier-stage order,
                # which never touched stock in the first place. Reversing a
                # completed sale is a materially bigger action (real stock,
                # possibly real money, already moved), so it's restricted
                # below to Owner/Branch Manager even though Staff can cancel
                # unfulfilled orders.
                if request.user.role not in [OWNER, BRANCH_MANAGER]:
                    raise _OrderActionError(
                        'Only the Owner or a Branch Manager can void a completed sale.'
                    )
                _reverse_stock(locked_txn, request.user)

            # Deduct stock once, when an online order is completed.
            if (new_status == SalesTransaction.OrderStatus.COMPLETED
                    and locked_txn.transaction_type == SalesTransaction.TransactionType.ONLINE):
                _deduct_stock(locked_txn, request.user)

            locked_txn.order_status = new_status
            locked_txn.save()

            if new_status == SalesTransaction.OrderStatus.CANCELLED:
                # Keep billing in step — this is the one field
                # transaction_set_status doesn't already own, so it used to
                # sit at Pending indefinitely after a cancellation. A voided
                # completed sale had real money involved (Paid or Pending);
                # any actual refund happens outside the system (no payment
                # gateway integration — see billing.PaymentMethod.COD) and
                # this only reflects that in the record.
                billing = getattr(locked_txn, 'billing_record', None)
                if billing and billing.status != BillingRecord.PaymentStatus.CANCELLED:
                    billing.status = BillingRecord.PaymentStatus.CANCELLED
                    billing.confirmed_by = request.user
                    billing.save()
    except _OrderActionError as e:
        messages.error(request, str(e))
        return redirect('transaction_detail', pk=pk)

    messages.success(request, f'{txn.order_number} is now {new_status}.')
    return redirect('transaction_detail', pk=pk)


def _deduct_stock(txn, user):
    """Deduct each line item from the order's branch floor stock + audit it.

    Locks each Inventory row (select_for_update) and raises
    _OrderActionError instead of silently floor-ing at 0 (or skipping
    entirely when no Inventory row exists at all) when there isn't enough —
    the caller's atomic block rolls back the whole sale so a rejected sale
    never partially applies.
    """
    for item in txn.items.select_related('product').all():
        # available_qty nets out anything past its expiry_date — stock that
        # expired after the order was placed but before it was fulfilled must
        # not be handed to the customer just because the order predates it.
        inv = Inventory.objects.select_for_update().sellable().filter(
            product=item.product, branch=txn.branch
        ).first()
        available = inv.available_qty if inv else 0
        if available < item.quantity_sold:
            raise _OrderActionError(
                f'Not enough stock for {item.product.product_name} — '
                f'available: {available}, needed: {item.quantity_sold}.'
            )
        before = inv.quantity_on_hand
        inv.quantity_on_hand -= item.quantity_sold
        inv.save()
        # FEFO: drain the nearest-expiry floor batches for this product/
        # branch. Batches now fully back quantity_on_hand (see Batch's own
        # docstring), so this should always find enough to consume — the
        # available_qty check above already confirmed non-expired batches
        # cover this sale.
        Batch.consume_fefo(item.product, txn.branch, Batch.Location.FLOOR, item.quantity_sold)
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


def _reverse_stock(txn, user):
    """Undo a completed sale's stock deduction when it's voided — the
    inverse of _deduct_stock, called from transaction_set_status when a
    COMPLETED order moves to Cancelled. If the original Inventory row was
    deleted since the sale, get_or_create it rather than losing the
    reversal (mirrors how a fresh receiving credits a possibly-new row).

    Deliberately does NOT restore the batches Batch.consume_fefo drew down
    at sale time — nothing records which specific batch(es) supplied a given
    sale, so there's no correct batch to credit back. The aggregate
    Inventory row (source of truth for quantity_on_hand) is fully reversed
    either way; only the batch-level expiry breakdown is left as-is."""
    for item in txn.items.select_related('product').all():
        inv, _ = Inventory.objects.select_for_update().get_or_create(
            product=item.product, branch=txn.branch,
            defaults={'quantity_on_hand': 0, 'reorder_level': 0},
        )
        before = inv.quantity_on_hand
        inv.quantity_on_hand += item.quantity_sold
        inv.save()
        StockMovement.objects.create(
            inventory=inv,
            location_type=StockMovement.LocationType.FLOOR,
            movement_type=StockMovement.MovementType.ADJUSTMENT,
            quantity_change=inv.quantity_on_hand - before,
            quantity_before=before,
            quantity_after=inv.quantity_on_hand,
            reference_id=txn.pk,
            notes=f'Voided order {txn.order_number} — stock reversal',
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
        return render(request, 'sales/transaction_form.html', _pos_context(request))

    branch_id = user_branch.id if user_branch else request.POST.get('branch')
    mode_of_payment = request.POST.get('mode_of_payment') or 'Cash'
    order_type = request.POST.get('order_type') or SalesTransaction.OrderType.RETAIL
    if order_type not in SalesTransaction.OrderType.values:
        order_type = SalesTransaction.OrderType.RETAIL
    product_ids = request.POST.getlist('product_id')
    quantities = request.POST.getlist('quantity')

    # --- cash tendered -------------------------------------------------------
    # Only meaningful for a Cash sale — GCash/Card never had cash handed over,
    # so the field is parsed but required (below) only when Cash is chosen.
    amount_tendered_raw = (request.POST.get('amount_tendered') or '').strip()
    amount_tendered = None
    if mode_of_payment == SalesTransaction.ModeOfPayment.CASH and amount_tendered_raw:
        try:
            amount_tendered = Decimal(amount_tendered_raw)
        except InvalidOperation:
            amount_tendered = None

    line_items = []
    for pid, q in zip(product_ids, quantities):
        if not pid:
            continue
        qty, _qty_error = parse_int_field(q, 'Quantity', min_value=1, max_value=MAX_QUANTITY)
        if qty is not None:
            line_items.append((pid, qty))

    # --- PWD / Senior Citizen discount ------------------------------------
    # Claimed once for the sale. The ID number is not optional: the statutory
    # discount is conditioned on presenting one, and a claim with no ID on
    # record is exactly what an audit would ask about.
    discount_type = request.POST.get('discount_type') or SalesTransaction.DiscountType.NONE
    if discount_type not in SalesTransaction.DiscountType.values:
        discount_type = SalesTransaction.DiscountType.NONE
    discount_claimed = discount_type != SalesTransaction.DiscountType.NONE
    discount_id_number = request.POST.get('discount_id_number', '').strip() if discount_claimed else ''
    discount_holder_name = request.POST.get('discount_holder_name', '').strip() if discount_claimed else ''

    # The same two checks, collected instead of redirected. A redirect threw
    # away every item row the cashier had already rung up.
    errors = []
    if not branch_id:
        errors.append('Please choose a branch for this sale.')
    if not line_items:
        errors.append('Add at least one item with a quantity.')
    if discount_claimed and not discount_id_number:
        errors.append(
            f'Enter the {discount_type} ID number — a discount cannot be '
            f'recorded without it.'
        )
    if discount_claimed and not discount_holder_name:
        errors.append(f'Enter the name on the {discount_type} ID.')
    if mode_of_payment == SalesTransaction.ModeOfPayment.CASH:
        if not amount_tendered_raw:
            errors.append('Enter the amount tendered for this cash sale.')
        elif amount_tendered is None:
            errors.append('Amount tendered must be a number.')

    if not errors:
        # Prices resolved and the total computed up front — same order as the
        # DRF API's create() — instead of creating the transaction at
        # total_amount=0 first (which left billing.signals creating an
        # amount_due=0 BillingRecord) and patching both afterward.
        #
        # sales.pricing owns the VAT and PWD/Senior arithmetic; this only
        # decides the price TIER (retail vs wholesale) and hands the lines
        # over. Keeping the two apart is what stops a discounted wholesale
        # line from being computed off the retail price by accident.
        cart_lines = []
        for product_id, qty in line_items:
            product = Product.objects.get(pk=product_id)
            cart_lines.append((product, qty, price_for(product, order_type)))

        priced = price_cart(cart_lines, discount_type=discount_type)
        total = priced['total_amount']

        # Now that the total is known: a Cash sale can't tender less than
        # what's due. Checked here rather than up front — total isn't known
        # until the cart is priced.
        change_amount = None
        if mode_of_payment == SalesTransaction.ModeOfPayment.CASH and amount_tendered is not None:
            if amount_tendered < total:
                errors.append(
                    f'Amount tendered (₱{amount_tendered}) is less than the amount due (₱{total}).'
                )
            else:
                change_amount = amount_tendered - total

        if not errors:
            try:
                with transaction.atomic():
                    txn = SalesTransaction.objects.create(
                        branch_id=branch_id,
                        staff=request.user if request.user.role == STAFF else None,
                        transaction_type=SalesTransaction.TransactionType.PHYSICAL,
                        mode_of_payment=mode_of_payment,
                        order_type=order_type,
                        order_status=SalesTransaction.OrderStatus.COMPLETED,  # walk-in = done
                        total_amount=total,
                        discount_type=discount_type,
                        discount_id_number=discount_id_number,
                        discount_holder_name=discount_holder_name,
                        total_discount_amount=priced['total_discount_amount'],
                        total_vat_amount=priced['total_vat_amount'],
                        amount_tendered=amount_tendered,
                        change_amount=change_amount,
                    )

                    for line in priced['lines']:
                        SalesItem.objects.create(
                            transaction=txn, product=line['product'],
                            quantity_sold=line['quantity'],
                            # The already-discounted unit price — SalesItem.save
                            # multiplies it out, so the discount must be in the
                            # price, not applied again on top of the subtotal.
                            unit_price=line['unit_price'],
                            is_vat_exempt_line=line['is_vat_exempt_line'],
                            vat_amount=line['vat_amount'],
                            discount_amount=line['discount_amount'],
                        )

                    # Walk-in sales leave the shelf immediately. Raises
                    # _OrderActionError (rolling back this whole atomic block,
                    # including the transaction/items just created above) if any
                    # line item is oversold.
                    _deduct_stock(txn, request.user)

                    # billing.signals already created the record (Pending, no
                    # confirmer). A cash walk-in is settled the moment it's
                    # rung — the money crossed the counter — so it's marked
                    # Paid here with the ringing user as confirmer, exactly as
                    # before. Every other method (GCash, Card) has to clear as
                    # a separate step through billing_confirm, checked by
                    # someone who didn't record the sale (A-1 maker/checker
                    # separation), so the record is left Pending for that.
                    billing = getattr(txn, 'billing_record', None)
                    if billing:
                        billing.amount_due = total
                        billing.payment_method = (
                            mode_of_payment if mode_of_payment in ['Cash', 'GCash', 'Card'] else 'Cash'
                        )
                        if mode_of_payment == SalesTransaction.ModeOfPayment.CASH:
                            billing.status = 'Paid'
                            billing.confirmed_by = request.user
                        billing.save()
            except _OrderActionError as e:
                errors.append(str(e))
            else:
                messages.success(request, f'Sale {txn.order_number} recorded — ₱{total}.')
                return redirect('transaction_detail', pk=txn.pk)

    # Rejected. Re-render the form with the header selections re-chosen and
    # the item rows rebuilt from what was posted — every pid/qty pair,
    # including the ones that failed the digit check, so a mistyped quantity
    # comes back visible instead of vanishing.
    context = _pos_context(request)
    context.update({
        'pos_errors': errors,
        'pos_form_data': request.POST,
        'pos_restore_rows': [
            {'product_id': pid, 'quantity': qty}
            for pid, qty in zip(product_ids, quantities) if pid
        ],
    })
    return render(request, 'sales/transaction_form.html', context)


# ─────────────────────────────────────────────
# CUSTOMER RETURNS
# Pending -> Approved -> Refunded, or -> Rejected. Per line item, against a
# COMPLETED sale — distinct from transaction_set_status's whole-order void.
# ─────────────────────────────────────────────

def _safe_positive_int(value):
    """int(value) if it's a clean positive integer no greater than
    MAX_QUANTITY, else None — guards against a tampered or malformed POST
    instead of a bare int() raising."""
    parsed, _error = parse_int_field(value, 'Quantity', min_value=1, max_value=MAX_QUANTITY)
    return parsed


def _already_returned_qty(sales_item):
    """How many units of this SalesItem are already spoken for by a return
    that isn't Rejected — Pending and Approved both hold their claim on the
    remaining quantity (a second cashier must not also accept a return for
    stock the first request already covers), Refunded is the same claim
    made final. Only Rejected releases it back to the returnable pool."""
    total = SalesReturnItem.objects.filter(sales_item=sales_item).exclude(
        sales_return__status=SalesReturn.Status.REJECTED
    ).aggregate(total=dj_models.Sum('quantity_returned'))['total']
    return total or 0


class _ReturnActionError(Exception):
    """Raised inside a return_create/return_approve atomic block so the
    whole block rolls back instead of partially applying a rejected action."""


def _restock_return(sales_return, user):
    """Credit floor stock for a Resellable return, the inverse of
    _deduct_stock/_reverse_stock above. Called once, from return_approve,
    only when condition is Resellable — a Damaged/Write-off return is
    approved (the refund is still owed) without ever touching Inventory,
    the same way a floor write-off (inventory.views.transfer_create) never
    credits stock back either."""
    txn = sales_return.transaction
    for return_item in sales_return.items.select_related('sales_item__product').all():
        product = return_item.sales_item.product
        inv, _ = Inventory.objects.select_for_update().get_or_create(
            product=product, branch=txn.branch,
            defaults={'quantity_on_hand': 0, 'reorder_level': 0},
        )
        before = inv.quantity_on_hand
        inv.quantity_on_hand += return_item.quantity_returned
        inv.save()
        StockMovement.objects.create(
            inventory=inv,
            location_type=StockMovement.LocationType.FLOOR,
            movement_type=StockMovement.MovementType.CUSTOMER_RETURN,
            quantity_change=inv.quantity_on_hand - before,
            quantity_before=before,
            quantity_after=inv.quantity_on_hand,
            reference_id=sales_return.pk,
            notes=f'{sales_return.return_number} — return of {product.product_name} from {txn.order_number}',
            created_by=user,
        )


def _return_list_context(request):
    returns = (
        SalesReturn.objects
        .select_related('transaction', 'transaction__branch', 'transaction__customer', 'requested_by')
        .order_by('-created_at')
    )
    returns = scope_queryset(returns, request.user, branch_field='transaction__branch')

    search_query = request.GET.get('q', '')
    status_filter = request.GET.get('status', '')
    # Owner-only branch filter — a Manager/Staff account is already pinned
    # to its own branch's returns above, so a selector would do nothing.
    user_branch = branch_for_user(request.user)
    branch_filter = request.GET.get('branch', '') if not user_branch else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''
    if branch_filter:
        returns = returns.filter(transaction__branch_id=branch_filter)

    if search_query:
        returns = returns.filter(
            dj_models.Q(return_number__icontains=search_query) |
            dj_models.Q(transaction__order_number__icontains=search_query) |
            dj_models.Q(transaction__customer__first_name__icontains=search_query) |
            dj_models.Q(transaction__customer__last_name__icontains=search_query)
        )

    # Counts per tab computed BEFORE the status filter, same reasoning as
    # _transaction_list_context — a tab's own count must not shrink when
    # that tab is the one currently selected.
    base = returns
    counts = {
        'all': base.count(),
        'pending': base.filter(status=SalesReturn.Status.PENDING).count(),
        'approved': base.filter(status=SalesReturn.Status.APPROVED).count(),
        'refunded': base.filter(status=SalesReturn.Status.REFUNDED).count(),
        'rejected': base.filter(status=SalesReturn.Status.REJECTED).count(),
    }
    if status_filter:
        returns = returns.filter(status=status_filter)

    today = timezone.localtime().date()
    month_start, month_end = local_month_bounds(today.year, today.month)
    refunded_this_month = base.filter(
        status=SalesReturn.Status.REFUNDED,
        refunded_at__gte=month_start, refunded_at__lt=month_end,
    ).aggregate(total=dj_models.Sum('refund_amount'))['total'] or 0

    # "New Return" modal, step 1: look up the original sale by order number.
    # Same two-step shape as Warehouse Stock's "Request Stock to Floor" and
    # Stock Adjustments' "New Stock Adjustment" modals (a picker step, then
    # the item form) — see inventory.views._branch_scoped_modal_target and
    # its callers. Read from POST first so a validation error in
    # return_create can redisplay this same step-2 form without losing the
    # order the cashier already looked up.
    order_number = (request.POST.get('order_number') or request.GET.get('order_number', '')).strip()
    origin_transaction = None
    origin_items = []
    if order_number:
        origin_transaction = SalesTransaction.objects.filter(
            order_number__iexact=order_number,
            order_status=SalesTransaction.OrderStatus.COMPLETED,
        ).select_related('branch', 'customer').first()
        if origin_transaction and not user_can_access_branch(request.user, origin_transaction.branch_id):
            origin_transaction = None
        if origin_transaction:
            for item in origin_transaction.items.select_related('product').all():
                already_returned = _already_returned_qty(item)
                remaining = item.quantity_sold - already_returned
                if remaining > 0:
                    origin_items.append({
                        'item': item,
                        'already_returned': already_returned,
                        'remaining': remaining,
                    })

    branches = Branch.objects.all() if not user_branch else []
    return {
        'returns': returns,
        'page_obj': paginate(request, returns),
        'counts': counts,
        'search_query': search_query,
        'status_filter': status_filter,
        'branches': branches,
        'branch_filter': branch_filter,
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        'user_branch': user_branch,
        'refunded_this_month': refunded_this_month,
        'month_label': today.strftime('%B'),
        'reason_choices': SalesReturn.Reason.choices,
        'refund_method_choices': SalesReturn.RefundMethod.choices,
        'order_number': order_number,
        'origin_transaction': origin_transaction,
        'origin_items': origin_items,
        'show_create_modal': request.GET.get('open_create') == '1' or bool(request.POST),
    }


@login_required(login_url='accounts:login')
def return_list(request):
    """Customer Returns — every return, filterable by status, plus the
    New Return modal that creates one against a completed sale."""
    if request.user.role not in INTERNAL_ROLES:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    return render(request, 'sales/return_list.html', _return_list_context(request))


@login_required(login_url='accounts:login')
def return_create(request):
    """POST handler for the New Return modal on Customer Returns. Staff can
    request a return same as they can ring up a walk-in sale; only Owner/
    Branch Manager can approve one (return_approve) — same request/approve
    split as a Stock Request slip.

    Validation failure re-renders the list template with the modal reopened
    on step 2 (item selection), same pattern as transfer_create/
    warehouse_stock_request — see _return_list_context's order_number
    handling for how the already-looked-up sale survives the redisplay.
    """
    if request.user.role not in [OWNER, BRANCH_MANAGER, STAFF]:
        messages.error(request, 'You do not have permission to record a return.')
        return redirect('return_list')

    if request.method != 'POST':
        return redirect('return_list')

    order_number = request.POST.get('order_number', '').strip()
    txn = SalesTransaction.objects.filter(
        order_number__iexact=order_number,
        order_status=SalesTransaction.OrderStatus.COMPLETED,
    ).select_related('branch').first()

    errors = []
    if not txn:
        errors.append(f'No completed order found for "{order_number}".')
    elif not user_can_access_branch(request.user, txn.branch_id):
        errors.append('That order belongs to another branch.')

    reason = request.POST.get('reason')
    if reason not in SalesReturn.Reason.values:
        errors.append('Choose a reason for the return.')

    condition = request.POST.get('condition')
    if condition not in SalesReturn.Condition.values:
        errors.append('Choose the item condition.')

    refund_method = request.POST.get('refund_method')
    if refund_method not in SalesReturn.RefundMethod.values:
        errors.append('Choose a refund method.')

    notes = request.POST.get('notes', '').strip()
    item_ids = request.POST.getlist('sales_item_id')
    quantities = request.POST.getlist('quantity_returned')
    valid_lines = [
        (item_id, _safe_positive_int(qty))
        for item_id, qty in zip(item_ids, quantities)
        if item_id and _safe_positive_int(qty)
    ]
    if not valid_lines:
        errors.append('Select at least one item and quantity to return.')

    if not errors:
        try:
            with transaction.atomic():
                sales_return = SalesReturn.objects.create(
                    transaction=txn,
                    reason=reason,
                    condition=condition,
                    refund_method=refund_method,
                    notes=notes,
                    requested_by=request.user,
                )
                total_refund = Decimal('0')
                for item_id, qty in valid_lines:
                    sales_item = txn.items.select_related('product').filter(pk=item_id).first()
                    if not sales_item:
                        raise _ReturnActionError('That item is not on this order.')
                    remaining = sales_item.quantity_sold - _already_returned_qty(sales_item)
                    if qty > remaining:
                        raise _ReturnActionError(
                            f'Cannot return {qty} of {sales_item.product.product_name} — '
                            f'only {remaining} remain returnable.'
                        )
                    refund_subtotal = (sales_item.unit_price * qty).quantize(Decimal('0.01'))
                    SalesReturnItem.objects.create(
                        sales_return=sales_return,
                        sales_item=sales_item,
                        quantity_returned=qty,
                        refund_subtotal=refund_subtotal,
                    )
                    total_refund += refund_subtotal
                sales_return.refund_amount = total_refund
                sales_return.save(update_fields=['refund_amount'])
        except _ReturnActionError as e:
            errors.append(str(e))
        else:
            messages.success(request, f'{sales_return.return_number} created — awaiting approval.')
            return redirect('return_detail', pk=sales_return.pk)

    for e in errors:
        messages.error(request, e)
    context = _return_list_context(request)
    context['show_create_modal'] = True
    return render(request, 'sales/return_list.html', context)


@login_required(login_url='accounts:login')
def return_detail(request, pk):
    sales_return = get_object_or_404(
        SalesReturn.objects.select_related(
            'transaction', 'transaction__branch', 'transaction__customer',
            'requested_by', 'decided_by',
        ), pk=pk
    )
    if not user_can_access_branch(request.user, sales_return.transaction.branch_id):
        messages.error(request, 'That return is not available to you.')
        return redirect('return_list')

    items = sales_return.items.select_related('sales_item__product').all()
    return render(request, 'sales/return_detail.html', {
        'sales_return': sales_return,
        'items': items,
        'can_decide': request.user.role in [OWNER, BRANCH_MANAGER],
    })


@role_required(OWNER, BRANCH_MANAGER)
def return_approve(request, pk):
    """Approve a Pending return — restocks the floor if Resellable (never
    if Damaged/Write-off). The refund itself is a separate step
    (return_mark_refunded): approval says the return is accepted and the
    stock side is settled, not that money has moved yet."""
    sales_return = get_object_or_404(SalesReturn.objects.select_related('transaction'), pk=pk)
    if not user_can_access_branch(request.user, sales_return.transaction.branch_id):
        messages.error(request, 'That return belongs to another branch.')
        return redirect('return_list')
    if request.method != 'POST':
        return redirect('return_detail', pk=pk)

    try:
        with transaction.atomic():
            locked = SalesReturn.objects.select_for_update().select_related('transaction').get(pk=pk)
            if locked.status != SalesReturn.Status.PENDING:
                raise _ReturnActionError(f'{locked.return_number} has already been processed.')

            if locked.condition == SalesReturn.Condition.RESELLABLE:
                _restock_return(locked, request.user)

            locked.status = SalesReturn.Status.APPROVED
            locked.decided_by = request.user
            locked.decided_at = timezone.now()
            locked.save()
    except _ReturnActionError as e:
        messages.error(request, str(e))
        return redirect('return_detail', pk=pk)

    messages.success(request, f'{sales_return.return_number} approved.')
    return redirect('return_detail', pk=pk)


@role_required(OWNER, BRANCH_MANAGER)
def return_reject(request, pk):
    sales_return = get_object_or_404(SalesReturn.objects.select_related('transaction'), pk=pk)
    if not user_can_access_branch(request.user, sales_return.transaction.branch_id):
        messages.error(request, 'That return belongs to another branch.')
        return redirect('return_list')
    if request.method != 'POST':
        return redirect('return_detail', pk=pk)

    decision_notes = request.POST.get('decision_notes', '').strip()
    with transaction.atomic():
        locked = SalesReturn.objects.select_for_update().get(pk=pk)
        if locked.status != SalesReturn.Status.PENDING:
            messages.error(request, f'{locked.return_number} has already been processed.')
            return redirect('return_detail', pk=pk)
        locked.status = SalesReturn.Status.REJECTED
        locked.decided_by = request.user
        locked.decided_at = timezone.now()
        locked.decision_notes = decision_notes
        locked.save()

    messages.success(request, f'{sales_return.return_number} rejected.')
    return redirect('return_detail', pk=pk)


@role_required(OWNER, BRANCH_MANAGER)
def return_mark_refunded(request, pk):
    sales_return = get_object_or_404(SalesReturn.objects.select_related('transaction'), pk=pk)
    if not user_can_access_branch(request.user, sales_return.transaction.branch_id):
        messages.error(request, 'That return belongs to another branch.')
        return redirect('return_list')
    if request.method != 'POST':
        return redirect('return_detail', pk=pk)

    with transaction.atomic():
        locked = SalesReturn.objects.select_for_update().get(pk=pk)
        if locked.status != SalesReturn.Status.APPROVED:
            messages.error(request, 'Only an approved return can be marked refunded.')
            return redirect('return_detail', pk=pk)
        locked.status = SalesReturn.Status.REFUNDED
        locked.refunded_at = timezone.now()
        locked.save()

    messages.success(request, f'{sales_return.return_number} marked as refunded.')
    return redirect('return_detail', pk=pk)
