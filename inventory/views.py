from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models, transaction
from django.utils import timezone
from datetime import date

from .models import (
    ProductCategory, Product, Inventory,
    Warehouse, WarehouseStock,
    StockTransfer, StockTransferItem, StockMovement
)
from accounts.models import User
from branches.models import Branch


# ─────────────────────────────────────────────
# HELPER: get branch for current user
# ─────────────────────────────────────────────

def get_user_branch(user):
    try:
        if user.role == User.Role.BRANCH_MANAGER:
            return user.manager_profile.branch
        elif user.role == User.Role.STAFF:
            return user.staff_profile.branch
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────
# PRODUCT VIEWS
# ─────────────────────────────────────────────

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
        wholesale_price = request.POST.get('wholesale_price') or None

        Product.objects.create(
            category_id=category_id,
            sku_code=sku_code,
            product_name=product_name,
            unit_of_measure=unit_of_measure,
            unit_price=unit_price,
            wholesale_price=wholesale_price,
        )
        messages.success(request, 'Product added successfully.')
        return redirect('product_list')

    return render(request, 'inventory/product_form.html', {
        'categories': categories,
    })


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
def product_delete(request, pk):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to delete products.')
        return redirect('product_list')

    product = get_object_or_404(Product, pk=pk)

    if request.method == 'POST':
        product.delete()
        messages.success(request, 'Product deleted.')
        return redirect('product_list')

    return render(request, 'inventory/product_confirm_delete.html', {
        'product': product,
    })


# ─────────────────────────────────────────────
# STORE FLOOR INVENTORY VIEW
# ─────────────────────────────────────────────

@login_required(login_url='login')
def inventory_list(request):
    inventory_records = Inventory.objects.select_related(
        'product', 'branch'
    ).all()

    user_branch = get_user_branch(request.user)
    if user_branch:
        inventory_records = inventory_records.filter(branch=user_branch)

    return render(request, 'inventory/inventory_list.html', {
        'inventory_records': inventory_records,
    })


# ─────────────────────────────────────────────
# WAREHOUSE STOCK VIEW
# ─────────────────────────────────────────────

@login_required(login_url='login')
def warehouse_stock_list(request):
    if request.user.role not in [
        User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF
    ]:
        messages.error(request, 'Access denied.')
        return redirect('home')

    # Get the warehouse for this user's branch
    user_branch = get_user_branch(request.user)

    if user_branch:
        try:
            warehouse = Warehouse.objects.get(branch=user_branch)
            stock_records = WarehouseStock.objects.filter(
                warehouse=warehouse
            ).select_related('product', 'product__category')
        except Warehouse.DoesNotExist:
            messages.error(
                request,
                'No warehouse is set up for your branch yet. '
                'Ask the Owner to create one.'
            )
            return redirect('inventory_list')
    else:
        # Owner sees all warehouses
        stock_records = WarehouseStock.objects.select_related(
            'product', 'warehouse', 'warehouse__branch'
        ).all()
        warehouse = None

    low_stock_count = stock_records.filter(restock_flag=True).count()
    total_products = stock_records.count()

    return render(request, 'inventory/warehouse_stock_list.html', {
        'stock_records': stock_records,
        'warehouse': warehouse,
        'low_stock_count': low_stock_count,
        'total_products': total_products,
    })


# ─────────────────────────────────────────────
# WITHDRAWAL SLIP (STOCK TRANSFER) VIEWS
# ─────────────────────────────────────────────

@login_required(login_url='login')
def transfer_list(request):
    if request.user.role not in [
        User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF
    ]:
        messages.error(request, 'Access denied.')
        return redirect('home')

    transfers = StockTransfer.objects.select_related(
        'source_branch', 'destination_branch',
        'requested_by', 'warehouse'
    ).all()

    # Branch managers and staff only see their branch's transfers
    user_branch = get_user_branch(request.user)
    if user_branch:
        transfers = transfers.filter(source_branch=user_branch)

    # Filters from GET parameters
    search_query = request.GET.get('q', '')
    status_filter = request.GET.get('status', '')
    purpose_filter = request.GET.get('purpose', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    if search_query:
        transfers = transfers.filter(
            models.Q(slip_number__icontains=search_query) |
            models.Q(items__product__product_name__icontains=search_query)
        ).distinct()

    if status_filter:
        transfers = transfers.filter(status=status_filter)

    if purpose_filter:
        transfers = transfers.filter(purpose=purpose_filter)

    if date_from:
        transfers = transfers.filter(transfer_date__gte=date_from)

    if date_to:
        transfers = transfers.filter(transfer_date__lte=date_to)

    # Summary stats for the top cards
    from django.utils.timezone import now
    this_month = now().month
    this_year = now().year

    total_released_this_month = transfers.filter(
        status=StockTransfer.Status.RELEASED,
        created_at__month=this_month,
        created_at__year=this_year,
    ).count()

    pending_acknowledgment = transfers.filter(
        status=StockTransfer.Status.RELEASED
    ).count()

    flagged_discrepancies = transfers.filter(
        status=StockTransfer.Status.DISCREPANCY
    ).count()

    return render(request, 'inventory/transfer_list.html', {
        'transfers': transfers,
        'search_query': search_query,
        'status_filter': status_filter,
        'purpose_filter': purpose_filter,
        'date_from': date_from,
        'date_to': date_to,
        'status_choices': StockTransfer.Status.choices,
        'purpose_choices': StockTransfer.Purpose.choices,
        'total_released_this_month': total_released_this_month,
        'pending_acknowledgment': pending_acknowledgment,
        'flagged_discrepancies': flagged_discrepancies,
    })


@login_required(login_url='login')
def transfer_create(request):
    if request.user.role not in [
        User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER
    ]:
        messages.error(request, 'You do not have permission to create withdrawal slips.')
        return redirect('transfer_list')

    user_branch = get_user_branch(request.user)

    # Get warehouse for this branch
    try:
        warehouse = Warehouse.objects.get(
            branch=user_branch or Branch.objects.first()
        )
    except Warehouse.DoesNotExist:
        messages.error(request, 'No warehouse found for your branch.')
        return redirect('transfer_list')

    # Only show products that actually have warehouse stock
    warehouse_stock = WarehouseStock.objects.filter(
        warehouse=warehouse, quantity_on_hand__gt=0
    ).select_related('product')

    branches = Branch.objects.exclude(
        id=warehouse.branch.id
    ) if user_branch else Branch.objects.all()

    if request.method == 'POST':
        destination_branch_id = request.POST.get('destination_branch') or None
        purpose = request.POST.get('purpose')
        notes = request.POST.get('notes', '')
        transfer_date = request.POST.get('transfer_date') or date.today()
        product_ids = request.POST.getlist('product_id')
        quantities = request.POST.getlist('quantity_requested')

        # Validate at least one item
        valid_items = [
            (pid, qty) for pid, qty in zip(product_ids, quantities)
            if pid and qty and int(qty) > 0
        ]

        if not valid_items:
            messages.error(request, 'Please add at least one product.')
            return render(request, 'inventory/transfer_form.html', {
                'warehouse_stock': warehouse_stock,
                'branches': branches,
                'purpose_choices': StockTransfer.Purpose.choices,
            })

        with transaction.atomic():
            transfer = StockTransfer.objects.create(
                warehouse=warehouse,
                source_branch=warehouse.branch,
                destination_branch_id=destination_branch_id,
                purpose=purpose,
                requested_by=request.user,
                status=StockTransfer.Status.PENDING,
                transfer_date=transfer_date,
                notes=notes,
            )

            for product_id, qty in valid_items:
                StockTransferItem.objects.create(
                    transfer=transfer,
                    product_id=product_id,
                    quantity_requested=int(qty),
                    quantity_approved=0,
                )

        messages.success(
            request,
            f'Withdrawal Slip {transfer.slip_number} created successfully. '
            f'Awaiting approval.'
        )
        return redirect('transfer_list')

    return render(request, 'inventory/transfer_form.html', {
        'warehouse_stock': warehouse_stock,
        'branches': branches,
        'purpose_choices': StockTransfer.Purpose.choices,
    })


@login_required(login_url='login')
def transfer_detail(request, pk):
    transfer = get_object_or_404(
        StockTransfer.objects.select_related(
            'warehouse', 'source_branch', 'destination_branch',
            'requested_by', 'approved_by'
        ), pk=pk
    )
    items = transfer.items.select_related('product').all()

    return render(request, 'inventory/transfer_detail.html', {
        'transfer': transfer,
        'items': items,
    })


@login_required(login_url='login')
def transfer_approve(request, pk):
    if request.user.role not in [User.Role.BRANCH_MANAGER, User.Role.OWNER]:
        messages.error(request, 'Only Branch Managers and Owners can approve transfers.')
        return redirect('transfer_list')

    transfer = get_object_or_404(StockTransfer, pk=pk)

    if transfer.status != StockTransfer.Status.PENDING:
        messages.error(request, 'This transfer has already been processed.')
        return redirect('transfer_detail', pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        approved_quantities = request.POST.getlist('quantity_approved')

        with transaction.atomic():
            if action == 'approve':
                items = transfer.items.all()
                all_valid = True

                for item, approved_qty in zip(items, approved_quantities):
                    approved_qty = int(approved_qty or 0)

                    # Check warehouse has enough stock
                    try:
                        ws = WarehouseStock.objects.get(
                            warehouse=transfer.warehouse,
                            product=item.product
                        )
                        if approved_qty > ws.quantity_on_hand:
                            messages.error(
                                request,
                                f'Not enough warehouse stock for '
                                f'{item.product.product_name}. '
                                f'Available: {ws.quantity_on_hand}'
                            )
                            all_valid = False
                            break
                    except WarehouseStock.DoesNotExist:
                        messages.error(
                            request,
                            f'{item.product.product_name} not found in warehouse.'
                        )
                        all_valid = False
                        break

                    item.quantity_approved = approved_qty
                    item.save()

                if all_valid:
                    transfer.status = StockTransfer.Status.APPROVED
                    transfer.approved_by = request.user
                    transfer.save()
                    messages.success(
                        request,
                        f'Transfer {transfer.slip_number} approved.'
                    )

            elif action == 'reject':
                transfer.status = StockTransfer.Status.REJECTED
                transfer.approved_by = request.user
                transfer.save()
                messages.success(
                    request,
                    f'Transfer {transfer.slip_number} rejected.'
                )

        return redirect('transfer_detail', pk=pk)

    items = transfer.items.select_related('product').all()
    return render(request, 'inventory/transfer_approve.html', {
        'transfer': transfer,
        'items': items,
    })


@login_required(login_url='login')
def transfer_complete(request, pk):
    if request.user.role not in [User.Role.BRANCH_MANAGER, User.Role.OWNER]:
        messages.error(request, 'Only Branch Managers and Owners can release transfers.')
        return redirect('transfer_list')

    transfer = get_object_or_404(StockTransfer, pk=pk)

    if transfer.status != StockTransfer.Status.APPROVED:
        messages.error(request, 'Transfer must be approved before it can be released.')
        return redirect('transfer_detail', pk=pk)

    if request.method == 'POST':
        with transaction.atomic():
            for item in transfer.items.select_related('product').all():
                if item.quantity_approved <= 0:
                    continue

                # 1. Deduct from warehouse stock
                ws = get_object_or_404(
                    WarehouseStock,
                    warehouse=transfer.warehouse,
                    product=item.product
                )
                qty_before_warehouse = ws.quantity_on_hand
                ws.quantity_on_hand -= item.quantity_approved
                ws.save()

                # Log warehouse movement
                StockMovement.objects.create(
                    warehouse_stock=ws,
                    location_type=StockMovement.LocationType.WAREHOUSE,
                    movement_type=StockMovement.MovementType.WAREHOUSE_TO_FLOOR,
                    quantity_change=-item.quantity_approved,
                    quantity_before=qty_before_warehouse,
                    quantity_after=ws.quantity_on_hand,
                    reference_id=transfer.pk,
                    notes=f'Withdrawal Slip {transfer.slip_number}',
                    created_by=request.user,
                )

                # 2. Add to store floor inventory
                if transfer.destination_branch:
                    inv, created = Inventory.objects.get_or_create(
                        product=item.product,
                        branch=transfer.destination_branch,
                        defaults={
                            'quantity_on_hand': 0,
                            'reorder_level': 0
                        }
                    )
                    qty_before_floor = inv.quantity_on_hand
                    inv.quantity_on_hand += item.quantity_approved
                    inv.save()

                    # Log floor movement
                    StockMovement.objects.create(
                        inventory=inv,
                        location_type=StockMovement.LocationType.FLOOR,
                        movement_type=StockMovement.MovementType.WAREHOUSE_TO_FLOOR,
                        quantity_change=item.quantity_approved,
                        quantity_before=qty_before_floor,
                        quantity_after=inv.quantity_on_hand,
                        reference_id=transfer.pk,
                        notes=f'Withdrawal Slip {transfer.slip_number}',
                        created_by=request.user,
                    )

            transfer.status = StockTransfer.Status.RELEASED
            transfer.save()

        messages.success(
            request,
            f'Withdrawal Slip {transfer.slip_number} released. '
            f'Stock has been moved to the store floor.'
        )
        return redirect('transfer_detail', pk=pk)

    return redirect('transfer_detail', pk=pk)