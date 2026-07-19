from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from branches.models import Branch
from inventory.models import Inventory, Product, ProductCategory
from sales.models import SalesItem, SalesTransaction

# Cap on how many rows the live-search dropdown shows at once.
SEARCH_SUGGESTION_LIMIT = 8

# Free delivery threshold shown on the cart meter.
FREE_DELIVERY_OVER = Decimal('500')


def is_customer(user):
    return user.is_authenticated and user.role == 'CUSTOMER'


def _cart_items(request):
    """Resolve the session cart into rows + total. Skips stale entries."""
    branch_id = request.session.get('branch_id')
    cart = request.session.get('cart', {})
    items, total = [], Decimal('0')

    for product_id, qty in cart.items():
        inv = Inventory.objects.select_related('product').filter(
            product_id=product_id, branch_id=branch_id
        ).first()
        if not inv:
            continue
        subtotal = inv.product.unit_price * qty
        total += subtotal
        items.append({
            'product': inv.product,
            'quantity': qty,
            'subtotal': subtotal,
            'in_stock': inv.quantity_on_hand,
            'short': qty > inv.quantity_on_hand,
        })
    return items, total


# ─────────────────────────────────────────────
# BROWSING — open to guests (log in only to buy)
# ─────────────────────────────────────────────

def product_catalog(request):
    branch_id = request.session.get('branch_id')
    branches = Branch.objects.all()

    stock = Inventory.objects.select_related('product', 'product__category')
    if branch_id:
        stock = stock.filter(branch_id=branch_id)

    search_query = request.GET.get('q', '')
    category_filter = request.GET.get('category', '')
    if search_query:
        stock = stock.filter(product__product_name__icontains=search_query)
    if category_filter:
        stock = stock.filter(product__category_id=category_filter)

    # Collapse to one row per product when no branch is chosen; keep the
    # best availability so stock signals stay honest.
    rows = {}
    for inv in stock:
        key = inv.product_id
        if key not in rows or inv.quantity_on_hand > rows[key]['qty']:
            rows[key] = {'product': inv.product, 'qty': inv.quantity_on_hand}

    return render(request, 'orders/catalog.html', {
        'rows': sorted(rows.values(), key=lambda r: r['product'].product_name),
        'branches': branches,
        'selected_branch_id': branch_id,
        'categories': ProductCategory.objects.all(),
        'search_query': search_query,
        'category_filter': category_filter,
        'cart_count': sum(request.session.get('cart', {}).values()),
    })


def search_suggestions(request):
    """JSON endpoint backing the header's live-search dropdown."""
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'results': []})

    products = (
        Product.objects.select_related('category')
        .filter(Q(product_name__icontains=query) | Q(sku_code__icontains=query))
        .order_by('product_name')[:SEARCH_SUGGESTION_LIMIT]
    )

    results = [
        {
            'id': product.id,
            'name': product.product_name,
            'category': product.category.category_name if product.category else '',
            'price': str(product.unit_price),
            'url': reverse('orders:product_detail', args=[product.id]),
        }
        for product in products
    ]
    return JsonResponse({'results': results})


def product_detail(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    branches = Branch.objects.all()

    branch_stock = []
    for branch in branches:
        inv = Inventory.objects.filter(product=product, branch=branch).first()
        branch_stock.append({
            'branch': branch,
            'available': bool(inv and inv.quantity_on_hand > 0),
            'qty': inv.quantity_on_hand if inv else 0,
        })

    return render(request, 'orders/product_detail.html', {
        'product': product,
        'branch_stock': branch_stock,
        'related': Product.objects.filter(category=product.category).exclude(id=product.id)[:6],
        'selected_branch_id': request.session.get('branch_id'),
        'cart_count': sum(request.session.get('cart', {}).values()),
    })


def select_branch(request):
    branches = Branch.objects.all()
    if request.method == 'POST':
        request.session['branch_id'] = int(request.POST['branch_id'])
        request.session['cart'] = {}
        return redirect('orders:catalog')
    return render(request, 'orders/select_branch.html', {'branches': branches})


def set_branch(request, branch_id):
    request.session['branch_id'] = branch_id
    request.session['cart'] = {}
    return redirect('orders:catalog')


def clear_branch(request):
    request.session.pop('branch_id', None)
    return redirect('orders:catalog')


# ─────────────────────────────────────────────
# CART + CHECKOUT — customers only
# ─────────────────────────────────────────────

@user_passes_test(is_customer)
def add_to_cart(request, product_id):
    branch_id = request.session.get('branch_id')
    if not branch_id:
        messages.info(request, 'Choose a branch first so we can check stock.')
        return redirect('orders:select_branch')

    inv = get_object_or_404(Inventory, product_id=product_id, branch_id=branch_id)
    qty = int(request.POST.get('quantity', 1) or 1)

    cart = request.session.get('cart', {})
    already = cart.get(str(product_id), 0)

    if already + qty > inv.quantity_on_hand:
        messages.error(
            request,
            f'Only {inv.quantity_on_hand} of {inv.product.product_name} left at this branch.'
        )
        return redirect('orders:catalog')

    cart[str(product_id)] = already + qty
    request.session['cart'] = cart
    messages.success(request, f'Added {inv.product.product_name} to your cart.')
    return redirect(request.POST.get('next') or 'orders:catalog')


@user_passes_test(is_customer)
def view_cart(request):
    items, total = _cart_items(request)
    remaining = max(FREE_DELIVERY_OVER - total, Decimal('0'))
    return render(request, 'orders/cart.html', {
        'items': items,
        'total': total,
        'free_delivery_over': FREE_DELIVERY_OVER,
        'remaining_for_free': remaining,
        'qualifies': total >= FREE_DELIVERY_OVER and total > 0,
        'progress_pct': min(int((total / FREE_DELIVERY_OVER) * 100), 100) if total else 0,
        'branch': Branch.objects.filter(id=request.session.get('branch_id')).first(),
        'payment_choices': [
            ('Cash on Delivery', 'Cash on Delivery'),
            ('Pay at Store', 'Pay at Store'),
            ('GCash', 'GCash'),
        ],
        'cart_count': sum(request.session.get('cart', {}).values()),
    })


@user_passes_test(is_customer)
def checkout(request):
    branch_id = request.session.get('branch_id')
    cart = request.session.get('cart', {})

    if not branch_id or not cart:
        messages.error(request, 'Your cart is empty.')
        return redirect('orders:catalog')

    if request.method != 'POST':
        return redirect('orders:cart')

    with transaction.atomic():
        # Validate availability WITHOUT deducting: an online order only leaves
        # the shelf when staff mark it Completed in the Order Console.
        planned = []
        for product_id, qty in cart.items():
            inv = get_object_or_404(
                Inventory.objects.select_for_update().select_related('product'),
                product_id=product_id, branch_id=branch_id,
            )
            if inv.quantity_on_hand < qty:
                messages.error(
                    request,
                    f'Sorry — only {inv.quantity_on_hand} of {inv.product.product_name} left.'
                )
                transaction.set_rollback(True)
                return redirect('orders:cart')
            planned.append((inv.product, qty))

        sale = SalesTransaction.objects.create(
            branch_id=branch_id,
            staff=None,
            customer=request.user,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            order_status=SalesTransaction.OrderStatus.PENDING,
            mode_of_payment=request.POST.get('mode_of_payment')
                            or SalesTransaction.ModeOfPayment.COD,
            delivery_address=request.POST.get('delivery_address', '').strip(),
        )

        total = Decimal('0')
        for product, qty in planned:
            SalesItem.objects.create(
                transaction=sale, product=product,
                quantity_sold=qty, unit_price=product.unit_price,
            )
            total += product.unit_price * qty

        sale.total_amount = total
        sale.save()

        # billing.signals already created the record — update it, never re-create
        # it (a second create violates the OneToOne and crashes checkout).
        billing = getattr(sale, 'billing_record', None)
        if billing:
            billing.amount_due = total
            billing.payment_method = sale.mode_of_payment
            billing.reference_number = request.POST.get('reference_number', '').strip()
            billing.save()

    request.session['cart'] = {}
    messages.success(request, f'Order {sale.order_number} placed!')
    return redirect('orders:confirmation', transaction_id=sale.id)


# ─────────────────────────────────────────────
# MY ORDERS / TRACK ORDER
# The Context Diagram says the Customer receives order status back from the
# system — this is that. Strictly scoped to the logged-in customer's own orders.
# ─────────────────────────────────────────────

# What the customer is told at each stage, in plain language.
STATUS_STEPS = ['Pending', 'Approved', 'Preparing', 'Completed']
STATUS_BLURB = {
    'Pending': "We've received your order. The store will confirm it shortly.",
    'Approved': 'The store has confirmed your order and will prepare it soon.',
    'Preparing': 'Your order is being packed right now.',
    'Completed': 'Done — your order has been handed over.',
    'Cancelled': 'This order was cancelled. Contact the branch if that looks wrong.',
}


@user_passes_test(is_customer)
def my_orders(request):
    orders = (
        SalesTransaction.objects.filter(customer=request.user)
        .select_related('branch')
        .prefetch_related('billing_record')
        .order_by('-transaction_date')
    )

    status_filter = request.GET.get('status', '')
    counts = {
        'all': orders.count(),
        'active': orders.exclude(
            order_status__in=['Completed', 'Cancelled']
        ).count(),
        'completed': orders.filter(order_status='Completed').count(),
    }
    if status_filter == 'active':
        orders = orders.exclude(order_status__in=['Completed', 'Cancelled'])
    elif status_filter == 'completed':
        orders = orders.filter(order_status='Completed')

    return render(request, 'orders/my_orders.html', {
        'orders': orders,
        'counts': counts,
        'status_filter': status_filter,
        'cart_count': sum(request.session.get('cart', {}).values()),
    })


@user_passes_test(is_customer)
def track_order(request, transaction_id):
    """Follow one order through its stages."""
    sale = get_object_or_404(
        SalesTransaction.objects.select_related('branch'),
        id=transaction_id, customer=request.user,   # never someone else's
    )

    cancelled = sale.order_status == 'Cancelled'
    current = 0 if cancelled else STATUS_STEPS.index(sale.order_status)
    steps = [
        {
            'name': name,
            'done': (not cancelled) and i < current,
            'current': (not cancelled) and i == current,
        }
        for i, name in enumerate(STATUS_STEPS)
    ]

    return render(request, 'orders/track_order.html', {
        'sale': sale,
        'items': sale.items.select_related('product').all(),
        'billing': getattr(sale, 'billing_record', None),
        'steps': steps,
        'cancelled': cancelled,
        'blurb': STATUS_BLURB.get(sale.order_status, ''),
        'cart_count': sum(request.session.get('cart', {}).values()),
    })


@user_passes_test(is_customer)
def order_confirmation(request, transaction_id):
    sale = get_object_or_404(
        SalesTransaction.objects.select_related('branch'),
        id=transaction_id, customer=request.user,
    )
    return render(request, 'orders/confirmation.html', {
        'sale': sale,
        'items': sale.items.select_related('product').all(),
        'billing': getattr(sale, 'billing_record', None),
    })
