from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models, transaction, IntegrityError
from django.utils import timezone
from datetime import date
from decimal import InvalidOperation as DecimalException

from .models import (
    ProductCategory, Product, ProductImage, Inventory,
    Warehouse, WarehouseStock,
    StockTransfer, StockTransferItem, StockMovement
)
from .alerts import alert_summary
from accounts.models import User
from branches.models import Branch
from core.permissions import INTERNAL_ROLES, OWNER, BRANCH_MANAGER, role_required
from core.scoping import branch_ids_for_user, user_can_access_branch


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

def _product_list_context(request):
    products = (
        Product.objects.select_related('category')
        .prefetch_related('inventory_records__branch', 'images')
        .all()
    )
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

    # Availability = the branches that actually carry the product.
    branch_total = Branch.objects.count()
    rows = []
    for p in products:
        names = sorted({i.branch.branch_name for i in p.inventory_records.all() if i.branch})
        if branch_total and len(names) >= branch_total:
            availability = 'All Branches'
        else:
            availability = ', '.join(names) if names else 'Not stocked'
        rows.append({'product': p, 'availability': availability})

    return {
        'rows': rows,
        'shown_count': len(rows),
        'categories': categories,
        'search_query': search_query,
        'category_filter': category_filter,
        'total_count': Product.objects.count(),
        'can_edit': request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER],
        'unit_choices': Product.UnitOfMeasure.choices,
        'next_sku': Product.next_sku_code(),
        'branches': Branch.objects.all(),
        'add_selected_branches': None,  # None = fresh modal, default all branches checked
    }


def _edit_modal_values(product, form_data=None):
    """Field values for the Edit Product modal: posted values win (so a
    validation error redisplays what the user typed), otherwise fall back
    to the product's current values (fresh open from the list page)."""
    def pick(field, product_value):
        posted = form_data.get(field) if form_data else None
        return posted if posted not in (None, '') else product_value

    return {
        'sku_code': product.sku_code,  # locked — never editable via this form
        'product_name': pick('product_name', product.product_name),
        'category_id': pick('category', str(product.category_id or '')),
        'unit_of_measure': pick('unit_of_measure', product.unit_of_measure),
        'unit_price': pick('unit_price', product.unit_price),
        'wholesale_price': pick('wholesale_price', product.wholesale_price),
        'description': pick('description', product.description),
    }


@login_required(login_url='login')
def product_list(request):
    if request.user.role not in INTERNAL_ROLES:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    context = _product_list_context(request)

    edit_pk = request.GET.get('edit')
    if edit_pk and context['can_edit']:
        product = Product.objects.filter(pk=edit_pk).first()
        if product:
            context.update({
                'show_edit_modal': True,
                'edit_product': product,
                'edit_values': _edit_modal_values(product),
            })

    return render(request, 'inventory/product_list.html', context)


@login_required(login_url='login')
def product_add(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to add products.')
        return redirect('product_list')

    if request.method != 'POST':
        # The add flow lives in a modal on the product list page now — send
        # anyone who lands here directly (bookmark, back button) there with
        # a flag so the page JS opens the modal on load.
        return redirect(f"{reverse('product_list')}?add=1")

    product_name = request.POST.get('product_name', '').strip()
    unit_price = request.POST.get('unit_price', '').strip()
    wholesale_price = request.POST.get('wholesale_price', '').strip()
    description = request.POST.get('description', '').strip()
    branch_ids = request.POST.getlist('branches')

    errors = []
    if not product_name:
        errors.append('Product name is required.')
    if not unit_price:
        errors.append('Retail price is required.')

    images = request.FILES.getlist('images')

    if not errors:
        try:
            with transaction.atomic():
                product = Product.objects.create(
                    category_id=request.POST.get('category') or None,
                    sku_code=Product.next_sku_code(),
                    product_name=product_name,
                    unit_of_measure=request.POST.get('unit_of_measure'),
                    unit_price=unit_price,
                    wholesale_price=wholesale_price or None,
                    description=description,
                )
                # request.FILES.getlist() returns EVERY file sent under name="images".
                for index, image_file in enumerate(images):
                    ProductImage.objects.create(
                        product=product,
                        image=image_file,
                        is_primary=(index == 0),  # first upload becomes the hero image
                    )
                # Availability: a checked branch gets a zero-qty floor Inventory
                # row so it shows up as stocked and ready for a withdrawal slip.
                for branch_id in branch_ids:
                    Inventory.objects.create(product=product, branch_id=branch_id)
        except (ValueError, TypeError, IntegrityError, DecimalException):
            errors.append('Could not save the product — check that the prices are valid numbers.')
        else:
            if images:
                messages.success(request, f'Product added with {len(images)} image(s).')
            else:
                messages.success(request, 'Product added successfully.')
            return redirect('product_list')

    context = _product_list_context(request)
    context.update({
        'show_add_modal': True,
        'add_errors': errors,
        'add_form_data': request.POST,
        'add_selected_branches': branch_ids,
    })
    return render(request, 'inventory/product_list.html', context)


@login_required(login_url='login')
def product_edit(request, pk):
    product = get_object_or_404(Product, pk=pk)

    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to edit products.')
        return redirect('product_list')

    if request.method != 'POST':
        # The edit flow lives in a modal on the product list page now — send
        # anyone who lands here directly (bookmark, back button) there with
        # a flag so the page JS opens the modal on load.
        return redirect(f"{reverse('product_list')}?edit={pk}")

    product_name = request.POST.get('product_name', '').strip()
    unit_price = request.POST.get('unit_price', '').strip()
    wholesale_price = request.POST.get('wholesale_price', '').strip()
    description = request.POST.get('description', '').strip()

    errors = []
    if not product_name:
        errors.append('Product name is required.')
    if not unit_price:
        errors.append('Retail price is required.')

    if not errors:
        try:
            with transaction.atomic():
                product.category_id = request.POST.get('category') or None
                # sku_code is locked in the edit modal — never trust a posted
                # value for it, so it can't be tampered with client-side.
                product.product_name = product_name
                product.unit_of_measure = request.POST.get('unit_of_measure')
                product.unit_price = unit_price
                product.wholesale_price = wholesale_price or None
                product.description = description
                product.save()

                # 1. Delete images the user ticked for removal (file + row).
                delete_ids = request.POST.getlist('delete_images')
                if delete_ids:
                    for img in product.images.filter(pk__in=delete_ids):
                        img.image.delete(save=False)  # remove the file from storage
                        img.delete()

                # 2. Add any newly uploaded images.
                for image_file in request.FILES.getlist('images'):
                    ProductImage.objects.create(product=product, image=image_file)

                # 3. Resolve which image is primary. Explicit choice wins; otherwise
                #    make sure something is primary so the storefront always has a hero
                #    (e.g. if the old primary was just deleted).
                primary_id = request.POST.get('primary_image')
                if primary_id:
                    product.images.update(is_primary=False)
                    product.images.filter(pk=primary_id).update(is_primary=True)
                elif not product.images.filter(is_primary=True).exists():
                    first = product.images.first()
                    if first:
                        first.is_primary = True
                        first.save(update_fields=['is_primary'])
        except (ValueError, TypeError, IntegrityError, DecimalException):
            errors.append('Could not save the product — check that the prices are valid numbers.')
        else:
            messages.success(request, 'Product updated successfully.')
            return redirect('product_list')

    context = _product_list_context(request)
    context.update({
        'show_edit_modal': True,
        'edit_product': product,
        'edit_errors': errors,
        'edit_values': _edit_modal_values(product, request.POST),
    })
    return render(request, 'inventory/product_list.html', context)


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
# CATEGORIES
# Product categories drive the catalog filters and the storefront's
# "Shop by Category" section, so they need a real management screen.
# ─────────────────────────────────────────────

@login_required(login_url='login')
def category_list(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    categories = ProductCategory.objects.annotate(
        product_count=models.Count('products')
    ).order_by('category_name')

    return render(request, 'inventory/category_list.html', {
        'categories': categories,
        'can_edit': request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER],
    })


@login_required(login_url='login')
def category_form(request, pk=None):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to manage categories.')
        return redirect('category_list')

    category = get_object_or_404(ProductCategory, pk=pk) if pk else None

    if request.method == 'POST':
        name = request.POST.get('category_name', '').strip()
        description = request.POST.get('description', '').strip()

        if not name:
            messages.error(request, 'Category name is required.')
            return redirect(request.path)

        clash = ProductCategory.objects.filter(category_name__iexact=name)
        if category:
            clash = clash.exclude(pk=category.pk)
        if clash.exists():
            messages.error(request, f'A category called "{name}" already exists.')
            return redirect(request.path)

        if category is None:
            ProductCategory.objects.create(category_name=name, description=description)
            messages.success(request, f'Category "{name}" created.')
        else:
            category.category_name = name
            category.description = description
            category.save()
            messages.success(request, f'Category "{name}" updated.')
        return redirect('category_list')

    return render(request, 'inventory/category_form.html', {'category': category})


@login_required(login_url='login')
def category_delete(request, pk):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to manage categories.')
        return redirect('category_list')

    category = get_object_or_404(ProductCategory, pk=pk)
    product_count = category.products.count()

    if request.method == 'POST':
        # Products would be orphaned (category is SET_NULL), so make the user
        # move them first rather than silently uncategorising the catalog.
        if product_count:
            messages.error(
                request,
                f'Cannot delete "{category.category_name}" — {product_count} '
                f'product{"s" if product_count != 1 else ""} still use it.'
            )
            return redirect('category_list')
        name = category.category_name
        category.delete()
        messages.success(request, f'Category "{name}" deleted.')
        return redirect('category_list')

    return render(request, 'inventory/category_confirm_delete.html', {
        'category': category,
        'product_count': product_count,
    })


# ─────────────────────────────────────────────
# ALERTS CENTER
# Low/critical stock + expiry alerts, branch-scoped. Powered by the shared
# inventory.alerts engine so these numbers match the Dashboard exactly.
# ─────────────────────────────────────────────

@role_required(OWNER, BRANCH_MANAGER)
def alerts_center(request):
    branch_ids = branch_ids_for_user(request.user)  # None = all branches
    summary = alert_summary(branch_ids)

    return render(request, 'inventory/alerts_center.html', {
        'critical_count': summary['critical_count'],
        'low_count': summary['low_count'],
        'expiring_count': summary['expiring_count'],
        'low_stock': summary['low_stock'],
        'expiry': summary['expiry'],
    })


# ─────────────────────────────────────────────
# STORE FLOOR INVENTORY VIEW
# ─────────────────────────────────────────────

@login_required(login_url='login')
def inventory_list(request):
    """Stock per Branch — real-time levels across branches, with status."""
    # Suppliers/customers must never reach this: get_user_branch() returns None
    # for them, which the branch filter below would read as "show everything".
    if request.user.role not in INTERNAL_ROLES:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    records = Inventory.objects.select_related(
        'product', 'product__category', 'branch'
    ).all()

    # Managers/staff see only their own branch (server-side, not just the UI).
    user_branch = get_user_branch(request.user)
    if user_branch:
        records = records.filter(branch=user_branch)

    search_query = request.GET.get('q', '')
    branch_filter = request.GET.get('branch', '')
    category_filter = request.GET.get('category', '')
    status_filter = request.GET.get('status', '')

    if search_query:
        records = records.filter(
            models.Q(product__product_name__icontains=search_query) |
            models.Q(product__sku_code__icontains=search_query)
        )
    if branch_filter:
        records = records.filter(branch_id=branch_filter)
    if category_filter:
        records = records.filter(product__category_id=category_filter)

    rows = []
    # Keys are template-safe (no spaces) so they can be used with dot lookup.
    counts = {'in_stock': 0, 'low': 0, 'critical': 0}
    key_for = {'Critical': 'critical', 'Low Stock': 'low', 'In Stock': 'in_stock'}
    for r in records:
        if r.reorder_level and r.quantity_on_hand <= r.reorder_level * 0.5:
            status = 'Critical'
        elif r.restock:
            status = 'Low Stock'
        else:
            status = 'In Stock'
        counts[key_for[status]] += 1
        if status_filter and status != status_filter:
            continue
        rows.append({'inv': r, 'status': status})

    return render(request, 'inventory/inventory_list.html', {
        'rows': rows,
        'counts': counts,
        'branches': Branch.objects.all() if not user_branch else [],
        'categories': ProductCategory.objects.all(),
        'search_query': search_query,
        'branch_filter': branch_filter,
        'category_filter': category_filter,
        'status_filter': status_filter,
        'user_branch': user_branch,
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
        return redirect('accounts:home')

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

@role_required(OWNER, BRANCH_MANAGER)
def transfer_list(request):
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

    # A slip concerns two branches, so either side may view it — but nobody else.
    # (The list is scoped; without this the detail leaks via the URL.)
    if not (user_can_access_branch(request.user, transfer.source_branch_id)
            or user_can_access_branch(request.user, transfer.destination_branch_id)):
        messages.error(request, 'That withdrawal slip is not available to you.')
        return redirect('transfer_list')

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

    if not user_can_access_branch(request.user, transfer.source_branch_id):
        messages.error(request, 'That withdrawal slip belongs to another branch.')
        return redirect('transfer_list')

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

    if not user_can_access_branch(request.user, transfer.source_branch_id):
        messages.error(request, 'That withdrawal slip belongs to another branch.')
        return redirect('transfer_list')

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


# ─────────────────────────────────────────────
# WITHDRAWAL SLIP: ACKNOWLEDGMENT (receiving side)
# The destination branch confirms it received the released stock, or flags
# a discrepancy if the received quantity differs from what was released.
# ─────────────────────────────────────────────

@login_required(login_url='login')
def transfer_confirm_receipt(request, pk):
    """Destination-branch staff confirms receipt of a RELEASED slip."""
    if request.user.role not in [
        User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER
    ]:
        messages.error(request, 'You do not have permission to confirm receipt.')
        return redirect('transfer_list')

    transfer = get_object_or_404(StockTransfer, pk=pk)

    if not user_can_access_branch(request.user, transfer.destination_branch_id):
        messages.error(request, 'That withdrawal slip belongs to another branch.')
        return redirect('transfer_list')

    if transfer.status != StockTransfer.Status.RELEASED:
        messages.error(request, 'Only released slips can be acknowledged.')
        return redirect('transfer_detail', pk=pk)

    if request.method != 'POST':
        return redirect('transfer_detail', pk=pk)

    # Optional received_qty override; defaults to the total approved quantity.
    approved_total = sum(i.quantity_approved for i in transfer.items.all())
    received_qty = int(request.POST.get('received_qty') or approved_total)

    with transaction.atomic():
        transfer.status = StockTransfer.Status.ACKNOWLEDGED
        transfer.acknowledged_by = request.user
        transfer.received_qty = received_qty
        transfer.save()

    messages.success(
        request,
        f'Receipt confirmed for {transfer.slip_number} ({received_qty} received).'
    )
    return redirect('transfer_detail', pk=pk)


@login_required(login_url='login')
def transfer_flag_discrepancy(request, pk):
    """
    Destination-branch staff flags a discrepancy: the quantity actually
    received differs from what was released. The floor inventory is corrected
    by the difference and an ADJUSTMENT stock movement is logged for the trail.
    """
    if request.user.role not in [
        User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER
    ]:
        messages.error(request, 'You do not have permission to flag a discrepancy.')
        return redirect('transfer_list')

    transfer = get_object_or_404(StockTransfer, pk=pk)

    if not user_can_access_branch(request.user, transfer.destination_branch_id):
        messages.error(request, 'That withdrawal slip belongs to another branch.')
        return redirect('transfer_list')

    if transfer.status != StockTransfer.Status.RELEASED:
        messages.error(request, 'Only released slips can be flagged.')
        return redirect('transfer_detail', pk=pk)

    if request.method != 'POST':
        return redirect('transfer_detail', pk=pk)

    approved_total = sum(i.quantity_approved for i in transfer.items.all())
    received_qty = int(request.POST.get('received_qty') or 0)
    reason = request.POST.get('flag_reason', '').strip()

    if not reason:
        messages.error(request, 'A reason is required to flag a discrepancy.')
        return redirect('transfer_detail', pk=pk)

    shortfall = approved_total - received_qty  # >0 means fewer units arrived

    with transaction.atomic():
        # Correct the destination floor stock by the shortfall (single-item
        # slips are the common case; for multi-item slips the correction is
        # applied to the first item's inventory record and detailed in notes).
        if transfer.destination_branch and shortfall != 0:
            first_item = transfer.items.select_related('product').first()
            if first_item:
                inv, _ = Inventory.objects.get_or_create(
                    product=first_item.product,
                    branch=transfer.destination_branch,
                    defaults={'quantity_on_hand': 0, 'reorder_level': 0},
                )
                qty_before = inv.quantity_on_hand
                inv.quantity_on_hand = max(inv.quantity_on_hand - shortfall, 0)
                inv.save()
                StockMovement.objects.create(
                    inventory=inv,
                    location_type=StockMovement.LocationType.FLOOR,
                    movement_type=StockMovement.MovementType.ADJUSTMENT,
                    quantity_change=inv.quantity_on_hand - qty_before,
                    quantity_before=qty_before,
                    quantity_after=inv.quantity_on_hand,
                    reference_id=transfer.pk,
                    notes=f'Discrepancy on {transfer.slip_number}: {reason}',
                    created_by=request.user,
                )

        transfer.status = StockTransfer.Status.DISCREPANCY
        transfer.acknowledged_by = request.user
        transfer.received_qty = received_qty
        transfer.flag_reason = reason
        transfer.save()

    messages.warning(
        request,
        f'Discrepancy flagged on {transfer.slip_number} '
        f'(released {approved_total}, received {received_qty}).'
    )
    return redirect('transfer_detail', pk=pk)