from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from .models import Procurement, ProcurementItem
from inventory.models import Product, Inventory
from accounts.models import User


@login_required(login_url='login')
def procurement_list(request):
    procurements = Procurement.objects.select_related('branch', 'supplier', 'owner').all()

    if request.user.role == User.Role.BRANCH_MANAGER:
        try:
            branch = request.user.manager_profile.branch
            procurements = procurements.filter(branch=branch)
        except Exception:
            procurements = procurements.none()
    elif request.user.role == User.Role.SUPPLIER:
        procurements = procurements.filter(supplier=request.user)

    return render(request, 'procurement/procurement_list.html', {'procurements': procurements})


@login_required(login_url='login')
def procurement_detail(request, pk):
    proc = get_object_or_404(Procurement, pk=pk)
    items = proc.items.select_related('product').all()
    return render(request, 'procurement/procurement_detail.html', {'procurement': proc, 'items': items})


@login_required(login_url='login')
def procurement_add(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to create procurement orders.')
        return redirect('procurement_list')

    products = Product.objects.all()
    suppliers = User.objects.filter(role=User.Role.SUPPLIER)

    from branches.models import Branch
    branches = Branch.objects.all()

    if request.method == 'POST':
        if request.user.role == User.Role.BRANCH_MANAGER:
            try:
                branch_id = request.user.manager_profile.branch.id
            except Exception:
                messages.error(request, 'No branch assigned to your account.')
                return redirect('procurement_list')
        else:
            branch_id = request.POST.get('branch')

        supplier_id = request.POST.get('supplier')
        order_date = request.POST.get('order_date')
        delivery_date = request.POST.get('delivery_date') or None
        product_ids = request.POST.getlist('product_id')
        quantities = request.POST.getlist('quantity_ordered')
        cost_prices = request.POST.getlist('cost_price')

        with transaction.atomic():
            owner_user = request.user if request.user.role == User.Role.OWNER else None

            proc = Procurement.objects.create(
                branch_id=branch_id,
                supplier_id=supplier_id,
                owner=owner_user,
                order_date=order_date,
                delivery_date=delivery_date,
                total_cost=0,
                status=Procurement.Status.ORDERED,
            )

            total = 0
            for product_id, qty, cost in zip(product_ids, quantities, cost_prices):
                if not product_id or not qty or not cost:
                    continue
                qty = int(qty)
                cost = float(cost)

                ProcurementItem.objects.create(
                    procurement=proc,
                    product_id=product_id,
                    quantity_ordered=qty,
                    cost_price=cost,
                )
                total += qty * cost

            proc.total_cost = total
            proc.save()

        messages.success(request, 'Procurement order created successfully.')
        return redirect('procurement_list')

    return render(request, 'procurement/procurement_form.html', {
        'products': products,
        'suppliers': suppliers,
        'branches': branches,
    })


@login_required(login_url='login')
def procurement_update_status(request, pk):
    proc = get_object_or_404(Procurement, pk=pk)

    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.SUPPLIER]:
        messages.error(request, 'You do not have permission to update this order.')
        return redirect('procurement_list')

    if request.method == 'POST':
        new_status = request.POST.get('status')

        with transaction.atomic():
            proc.status = new_status
            proc.save()

            if new_status == Procurement.Status.RECEIVED:
                for item in proc.items.all():
                    item.quantity_received = item.quantity_ordered
                    item.save()

                    inv, created = Inventory.objects.get_or_create(
                        product=item.product,
                        branch=proc.branch,
                        defaults={'quantity_on_hand': 0, 'reorder_level': 0}
                    )
                    inv.quantity_on_hand += item.quantity_ordered
                    inv.save()

        messages.success(request, 'Status updated.')

    return redirect('procurement_detail', pk=pk)