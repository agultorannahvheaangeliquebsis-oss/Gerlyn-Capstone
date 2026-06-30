from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from .models import SalesTransaction, SalesItem
from inventory.models import Product, Inventory
from accounts.models import User


@login_required(login_url='login')
def transaction_list(request):
    transactions = SalesTransaction.objects.select_related('branch', 'staff', 'customer').all()

    # Branch managers and staff only see their own branch's transactions
    if request.user.role in [User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        try:
            branch = request.user.manager_profile.branch if request.user.role == User.Role.BRANCH_MANAGER \
                else request.user.staff_profile.branch
            transactions = transactions.filter(branch=branch)
        except Exception:
            transactions = transactions.none()

    return render(request, 'sales/transaction_list.html', {'transactions': transactions})


@login_required(login_url='login')
def transaction_detail(request, pk):
    txn = get_object_or_404(SalesTransaction, pk=pk)
    items = txn.items.select_related('product').all()
    return render(request, 'sales/transaction_detail.html', {'transaction': txn, 'items': items})


@login_required(login_url='login')
def transaction_add(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'You do not have permission to record sales.')
        return redirect('transaction_list')

    products = Product.objects.all()

    if request.method == 'POST':
        branch_id = request.POST.get('branch')
        transaction_type = request.POST.get('transaction_type')
        mode_of_payment = request.POST.get('mode_of_payment')
        product_ids = request.POST.getlist('product_id')
        quantities = request.POST.getlist('quantity')

        with transaction.atomic():
            txn = SalesTransaction.objects.create(
                branch_id=branch_id,
                staff=request.user if request.user.role == User.Role.STAFF else None,
                transaction_type=transaction_type,
                mode_of_payment=mode_of_payment,
                total_amount=0,
            )

            total = 0
            for product_id, qty in zip(product_ids, quantities):
                if not product_id or not qty:
                    continue
                product = Product.objects.get(pk=product_id)
                qty = int(qty)
                subtotal = product.unit_price * qty

                SalesItem.objects.create(
                    transaction=txn,
                    product=product,
                    quantity_sold=qty,
                    unit_price=product.unit_price,
                )
                total += subtotal

                # Reduce stock if an Inventory record exists for this branch/product
                inv = Inventory.objects.filter(product=product, branch_id=branch_id).first()
                if inv:
                    inv.quantity_on_hand = max(0, inv.quantity_on_hand - qty)
                    inv.save()

            txn.total_amount = total
            txn.save()

        messages.success(request, 'Transaction recorded successfully.')
        return redirect('transaction_list')

    from branches.models import Branch
    branches = Branch.objects.all()
    return render(request, 'sales/transaction_form.html', {
        'products': products,
        'branches': branches,
    })