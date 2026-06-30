from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import ProductCategory, Product, Inventory
from accounts.models import User
from django.db import models


def role_required(*allowed_roles):
    """Simple helper to restrict a view to certain roles."""
    def check(user):
        return user.is_authenticated and user.role in allowed_roles
    return check


@login_required(login_url='login')
def product_list(request):
    products = Product.objects.select_related('category').all()
    categories = ProductCategory.objects.all()

    search_query = request.GET.get('q', '')
    category_filter = request.GET.get('category', '')

    if search_query:
        products = products.filter(
            models.Q(product_name__icontains=search_query) |
            models.Q(sku_code__icontains=search_query)
        )
    if category_filter:
        products = products.filter(category_id=category_filter)

    return render(request, 'inventory/product_list.html', {
        'products': products,
        'categories': categories,
        'search_query': search_query,
        'category_filter': category_filter,
        'total_count': Product.objects.count(),
    })


@login_required(login_url='login')
def product_add(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to add products.')
        return redirect('product_list')

    categories = ProductCategory.objects.all()

    if request.method == 'POST':
        category_id = request.POST.get('category')
        sku_code = request.POST.get('sku_code')
        product_name = request.POST.get('product_name')
        unit_of_measure = request.POST.get('unit_of_measure')
        unit_price = request.POST.get('unit_price')

        Product.objects.create(
        category_id=category_id,
        sku_code=sku_code,
        product_name=product_name,
        unit_of_measure=unit_of_measure,
        unit_price=unit_price,
        wholesale_price=request.POST.get('wholesale_price') or None,
    )
        messages.success(request, 'Product added successfully.')
        return redirect('product_list')

    return render(request, 'inventory/product_form.html', {'categories': categories})


@login_required(login_url='login')
def product_edit(request, pk):
    product = get_object_or_404(Product, pk=pk)
    categories = ProductCategory.objects.all()

    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to edit products.')
        return redirect('product_list')

    if request.method == 'POST':
        product.category_id = request.POST.get('category')
        product.sku_code = request.POST.get('sku_code')
        product.product_name = request.POST.get('product_name')
        product.unit_of_measure = request.POST.get('unit_of_measure')
        product.unit_price = request.POST.get('unit_price')
        product.wholesale_price = request.POST.get('wholesale_price') or None
        product.save()
        messages.success(request, 'Product updated successfully.')
        return redirect('product_list')

    return render(request, 'inventory/product_form.html', {
        'product': product,
        'categories': categories,
    })


@login_required(login_url='login')
def inventory_list(request):
    inventory_records = Inventory.objects.select_related('product', 'branch').all()

    # Branch managers and staff only see their own branch's inventory
    if request.user.role in [User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        try:
            branch = request.user.manager_profile.branch if request.user.role == User.Role.BRANCH_MANAGER \
                else request.user.staff_profile.branch
            inventory_records = inventory_records.filter(branch=branch)
        except Exception:
            inventory_records = inventory_records.none()

    return render(request, 'inventory/inventory_list.html', {'inventory_records': inventory_records})