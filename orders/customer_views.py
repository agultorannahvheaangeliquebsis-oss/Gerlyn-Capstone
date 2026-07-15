from decimal import Decimal
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from branches.models import Branch
from inventory.models import Inventory
from sales.models import SalesTransaction, SalesItem
from billing.models import BillingRecord


def is_customer(user):
    return user.is_authenticated and user.role == 'CUSTOMER'


@user_passes_test(is_customer)
def select_branch(request):
    branches = Branch.objects.all()
    if request.method == 'POST':
        request.session['branch_id'] = int(request.POST['branch_id'])
        request.session['cart'] = {}
        return redirect('orders:catalog')
    return render(request, 'orders/select_branch.html', {'branches': branches})


@user_passes_test(is_customer)
def product_catalog(request):
    branch_id = request.session.get('branch_id')
    branches = Branch.objects.all()

    if branch_id:
        stock_records = Inventory.objects.filter(
            branch_id=branch_id, quantity_on_hand__gt=0
        ).select_related('product', 'product__category')
    else:
        stock_records = Inventory.objects.filter(
            quantity_on_hand__gt=0
        ).select_related('product', 'product__category').distinct()

    from inventory.models import ProductCategory
    categories = ProductCategory.objects.all()

    return render(request, 'orders/catalog.html', {
        'stock_records': stock_records,
        'branches': branches,
        'selected_branch_id': branch_id,
        'categories': categories,
    })


@user_passes_test(is_customer)
def add_to_cart(request, product_id):
    branch_id = request.session.get('branch_id')
    if not branch_id:
        return redirect('orders:select_branch')

    inv = get_object_or_404(Inventory, product_id=product_id, branch_id=branch_id)
    qty = int(request.POST.get('quantity', 1))

    if qty > inv.quantity_on_hand:
        messages.error(request, f"Only {inv.quantity_on_hand} of {inv.product.product_name} in stock.")
        return redirect('orders:catalog')

    cart = request.session.get('cart', {})
    cart[str(product_id)] = cart.get(str(product_id), 0) + qty
    request.session['cart'] = cart
    messages.success(request, f"Added {inv.product.product_name} to cart.")
    return redirect('orders:catalog')


@user_passes_test(is_customer)
def view_cart(request):
    branch_id = request.session.get('branch_id')
    cart = request.session.get('cart', {})
    items, total = [], Decimal('0')

    for product_id, qty in cart.items():
        inv = get_object_or_404(Inventory, product_id=product_id, branch_id=branch_id)
        subtotal = inv.product.unit_price * qty
        total += subtotal
        items.append({'product': inv.product, 'quantity': qty, 'subtotal': subtotal})

    return render(request, 'orders/cart.html', {'items': items, 'total': total})


@user_passes_test(is_customer)
def checkout(request):
    branch_id = request.session.get('branch_id')
    cart = request.session.get('cart', {})
    if not branch_id or not cart:
        messages.error(request, "Your cart is empty.")
        return redirect('orders:catalog')

    with transaction.atomic():
        sale = SalesTransaction.objects.create(
            branch_id=branch_id,
            staff=None,
            customer=request.user,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            mode_of_payment=request.POST.get('mode_of_payment', SalesTransaction.ModeOfPayment.CASH),
        )

        running_total = Decimal('0')
        for product_id, qty in cart.items():
            inv = get_object_or_404(
                Inventory.objects.select_for_update(),
                product_id=product_id, branch_id=branch_id
            )
            if inv.quantity_on_hand < qty:
                messages.error(request, f"Not enough stock for {inv.product.product_name}.")
                transaction.set_rollback(True)
                return redirect('orders:cart')

            SalesItem.objects.create(
                transaction=sale,
                product=inv.product,
                quantity_sold=qty,
                unit_price=inv.product.unit_price,
            )
            inv.quantity_on_hand -= qty
            inv.save()
            running_total += inv.product.unit_price * qty

        sale.total_amount = running_total
        sale.save()

        # Auto-create pending billing record
        BillingRecord.objects.create(
            transaction=sale,
            amount_due=sale.total_amount,
            payment_method=sale.mode_of_payment,
            status=BillingRecord.PaymentStatus.PENDING,
        )

    request.session['cart'] = {}
    return redirect('orders:confirmation', transaction_id=sale.id)


@user_passes_test(is_customer)
def order_confirmation(request, transaction_id):
    sale = get_object_or_404(SalesTransaction, id=transaction_id, customer=request.user)
    return render(request, 'orders/confirmation.html', {'sale': sale})

def set_branch(request, branch_id):
    request.session['branch_id'] = branch_id
    return redirect('orders:catalog')

def clear_branch(request):
    request.session.pop('branch_id', None)
    return redirect('orders:catalog')