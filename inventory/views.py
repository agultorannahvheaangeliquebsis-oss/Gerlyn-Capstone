from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models, transaction, IntegrityError
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST
from datetime import date
from decimal import InvalidOperation as DecimalException

from .models import (
    ProductCategory, Product, ProductImage, Inventory,
    Warehouse, WarehouseStock, Batch,
    StockTransfer, StockTransferItem, StockMovement
)
from .alerts import alert_summary, stock_status
from accounts.models import User
from branches.models import Branch
from core.permissions import INTERNAL_ROLES, OWNER, BRANCH_MANAGER, STAFF, role_required
from core.refdata import bust_categories_cache, cached_branches, cached_categories
from core.scoping import branch_ids_for_user, user_can_access_branch
from core.timebounds import local_month_bounds


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
    categories = cached_categories()

    search_query = request.GET.get('q', '')
    category_filter = request.GET.get('category', '')
    # The catalog itself has no owning branch — a product just carries
    # Inventory rows at whichever branches stock it — so this filter narrows
    # the LIST to products carried at the chosen branch rather than scoping a
    # branch-owned field. Owner only: Manager/Staff already see the same
    # unrestricted catalog everyone else does, so a selector here would only
    # ever have one meaningful choice for them.
    show_branch_filter = request.user.role == User.Role.OWNER
    branch_filter = request.GET.get('branch', '') if show_branch_filter else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''

    if search_query:
        products = products.filter(
            models.Q(product_name__icontains=search_query) |
            models.Q(sku_code__icontains=search_query)
        )
    if category_filter:
        products = products.filter(category_id=category_filter)
    if branch_filter:
        products = products.filter(inventory_records__branch_id=branch_filter)

    # Availability = the branches that actually carry the product.
    branches = cached_branches()
    branch_total = len(branches)
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
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        'total_count': Product.objects.count(),
        'can_edit': request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER],
        'unit_choices': Product.UnitOfMeasure.choices,
        'next_sku': Product.next_sku_code(),
        'branches': branches,
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
        'barcode': pick('barcode', product.barcode or ''),
        'category_id': pick('category', str(product.category_id or '')),
        'unit_of_measure': pick('unit_of_measure', product.unit_of_measure),
        'unit_price': pick('unit_price', product.unit_price),
        'wholesale_price': pick('wholesale_price', product.wholesale_price),
        'description': pick('description', product.description),
        # Checkboxes can't use pick(): an unticked box posts nothing, which
        # pick() would read as "not supplied" and fall back to the stored
        # True — making the box impossible to turn off. On a redisplay the
        # posted data is the whole truth about what was ticked. Every tax box
        # starts unticked on a fresh Add form, so there is no default to
        # preserve here — only what the user actually chose.
        'is_vat_exempt': (
            bool(form_data.get('is_vat_exempt')) if form_data is not None
            else product.is_vat_exempt
        ),
        'is_pwd_eligible': (
            bool(form_data.get('is_pwd_eligible')) if form_data is not None
            else product.is_pwd_eligible
        ),
        'is_senior_eligible': (
            bool(form_data.get('is_senior_eligible')) if form_data is not None
            else product.is_senior_eligible
        ),
    }


@login_required(login_url='accounts:login')
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


@login_required(login_url='accounts:login')
def barcode_lookup(request):
    """AJAX: resolve a scanned/typed code to product details for the shared
    barcode field on Add Product and Stock Adjustment. Checks the real
    `barcode` field first, falling back to `sku_code` (covers codes that
    were only ever assigned internally). Returns {'found': False} for no
    match — an expected, handled outcome here, not an error."""
    if request.user.role not in INTERNAL_ROLES:
        return JsonResponse({'found': False}, status=403)

    code = request.GET.get('code', '').strip()
    if not code:
        return JsonResponse({'found': False})

    product = (
        Product.objects.filter(barcode__iexact=code).first()
        or Product.objects.filter(sku_code__iexact=code).first()
    )
    if not product:
        return JsonResponse({'found': False})

    return JsonResponse({
        'found': True,
        'id': product.id,
        'sku_code': product.sku_code,
        'barcode': product.barcode or '',
        'product_name': product.product_name,
        'category_id': product.category_id,
        'unit_of_measure': product.unit_of_measure,
        'unit_price': str(product.unit_price),
        'wholesale_price': str(product.wholesale_price) if product.wholesale_price else '',
        'edit_url': reverse('product_edit', args=[product.id]),
    })


@login_required(login_url='accounts:login')
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
    barcode = request.POST.get('barcode', '').strip() or None
    unit_price = request.POST.get('unit_price', '').strip()
    wholesale_price = request.POST.get('wholesale_price', '').strip()
    description = request.POST.get('description', '').strip()
    branch_ids = request.POST.getlist('branches')

    errors = []
    if not product_name:
        errors.append('Product name is required.')
    if not unit_price:
        errors.append('Retail price is required.')
    if barcode and Product.objects.filter(barcode__iexact=barcode).exists():
        errors.append(f'Barcode "{barcode}" is already assigned to another product.')

    images = request.FILES.getlist('images')

    if not errors:
        try:
            with transaction.atomic():
                product = Product.objects.create(
                    category_id=request.POST.get('category') or None,
                    sku_code=Product.next_sku_code(),
                    barcode=barcode,
                    product_name=product_name,
                    unit_of_measure=request.POST.get('unit_of_measure'),
                    unit_price=unit_price,
                    wholesale_price=wholesale_price or None,
                    description=description,
                    # Tax/discount flags. All three boxes start unticked, so
                    # an absent value simply means "not ticked" — a product is
                    # only discount-eligible when someone says so.
                    is_vat_exempt=bool(request.POST.get('is_vat_exempt')),
                    is_pwd_eligible=bool(request.POST.get('is_pwd_eligible')),
                    is_senior_eligible=bool(request.POST.get('is_senior_eligible')),
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
                # A Branch Manager only gets to do this for their own branch —
                # branch_ids comes straight from POST, so a tampered request
                # (devtools/proxy) listing another branch must not create a
                # stock row there. Owner is unrestricted, as everywhere else.
                for branch_id in branch_ids:
                    if branch_id.isdigit() and user_can_access_branch(request.user, int(branch_id)):
                        Inventory.objects.create(product=product, branch_id=int(branch_id))
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


@login_required(login_url='accounts:login')
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
    barcode = request.POST.get('barcode', '').strip() or None
    unit_price = request.POST.get('unit_price', '').strip()
    wholesale_price = request.POST.get('wholesale_price', '').strip()
    description = request.POST.get('description', '').strip()

    errors = []
    if not product_name:
        errors.append('Product name is required.')
    if not unit_price:
        errors.append('Retail price is required.')
    # Excludes this product itself — re-saving with its own existing barcode
    # unchanged must not read as a collision.
    if barcode and Product.objects.filter(barcode__iexact=barcode).exclude(pk=product.pk).exists():
        errors.append(f'Barcode "{barcode}" is already assigned to another product.')

    if not errors:
        try:
            with transaction.atomic():
                product.category_id = request.POST.get('category') or None
                # sku_code is locked in the edit modal — never trust a posted
                # value for it, so it can't be tampered with client-side.
                product.product_name = product_name
                product.barcode = barcode
                product.unit_of_measure = request.POST.get('unit_of_measure')
                product.unit_price = unit_price
                product.wholesale_price = wholesale_price or None
                product.description = description
                product.is_vat_exempt = bool(request.POST.get('is_vat_exempt'))
                product.is_pwd_eligible = bool(request.POST.get('is_pwd_eligible'))
                product.is_senior_eligible = bool(request.POST.get('is_senior_eligible'))
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


@login_required(login_url='accounts:login')
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

def _category_list_context(request):
    return {
        'categories': ProductCategory.objects.annotate(
            product_count=models.Count('products')
        ).order_by('category_name'),
        'can_edit': request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER],
    }


@login_required(login_url='accounts:login')
def category_list(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    context = _category_list_context(request)

    edit_pk = request.GET.get('edit')
    if edit_pk and context['can_edit']:
        category = ProductCategory.objects.filter(pk=edit_pk).first()
        if category:
            context.update({'show_edit_modal': True, 'edit_category': category})

    return render(request, 'inventory/category_list.html', context)


@login_required(login_url='accounts:login')
def category_form(request, pk=None):
    """Add/Edit Category. Both live in a modal on the category list now, the
    same way Add/Edit Product do — this view is the POST target and no longer
    renders a page of its own.

    On a validation error it re-renders the list with the modal reopened and
    the submitted values still in the fields, rather than redirecting with a
    message: a redirect discards what was typed, which on a two-field form is
    the whole form."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to manage categories.')
        return redirect('category_list')

    category = get_object_or_404(ProductCategory, pk=pk) if pk else None

    if request.method != 'POST':
        # Direct hit (bookmark, back button) on what used to be a page: send
        # them to the list with the flag that opens the right modal.
        flag = f'edit={pk}' if category else 'add=1'
        return redirect(f"{reverse('category_list')}?{flag}")

    name = request.POST.get('category_name', '').strip()
    description = request.POST.get('description', '').strip()

    errors = []
    if not name:
        errors.append('Category name is required.')
    else:
        clash = ProductCategory.objects.filter(category_name__iexact=name)
        if category:
            clash = clash.exclude(pk=category.pk)
        if clash.exists():
            errors.append(f'A category called "{name}" already exists.')

    if not errors:
        if category is None:
            ProductCategory.objects.create(category_name=name, description=description)
            messages.success(request, f'Category "{name}" created.')
        else:
            category.category_name = name
            category.description = description
            category.save()
            messages.success(request, f'Category "{name}" updated.')
        bust_categories_cache()
        return redirect('category_list')

    context = _category_list_context(request)
    if category:
        context.update({
            'show_edit_modal': True,
            'edit_category': category,
            'edit_errors': errors,
            'edit_form_data': request.POST,
        })
    else:
        context.update({
            'show_add_modal': True,
            'add_errors': errors,
            'add_form_data': request.POST,
        })
    return render(request, 'inventory/category_list.html', context)


@login_required(login_url='accounts:login')
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
        bust_categories_cache()
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

@role_required(OWNER, BRANCH_MANAGER, STAFF)
def alerts_center(request):
    branch_ids = branch_ids_for_user(request.user)  # None = all branches
    summary = alert_summary(branch_ids)

    return render(request, 'inventory/alerts_center.html', {
        'critical_count': summary['critical_count'],
        'low_count': summary['low_count'],
        'warehouse_critical_count': summary['warehouse_critical_count'],
        'warehouse_low_count': summary['warehouse_low_count'],
        'expired_count': summary['expired_count'],
        'expiring_soon_count': summary['expiring_soon_count'],
        'approaching_expiry_count': summary['approaching_expiry_count'],
        'low_stock': summary['low_stock'],
        'low_warehouse_stock': summary['low_warehouse_stock'],
        'expiry_expired': summary['expiry_expired'],
        'expiry_soon': summary['expiry_soon'],
        'expiry_approaching': summary['expiry_approaching'],
    })


@role_required(OWNER, BRANCH_MANAGER, STAFF)
@require_POST
def expiry_write_off(request, pk):
    """Confirm-and-remove one expired Floor Stock BATCH, reached from the
    Expired tab of Alerts Center. Zeroes just that lot — not the whole
    product/branch row, which may still have other, unexpired batches — and
    logs it as a write-off. A deliberate, one-way action a human triggers,
    never anything automatic.

    Distinct from inventory_edit (Owner/Branch Manager only, for the
    reorder level): this only ever zeroes a lot that is ALREADY expired, so
    it is open to Staff too — "only after staff confirms the expiry" is the
    whole point of this action existing.
    """
    batch = get_object_or_404(
        Batch.objects.select_related('product', 'branch'),
        pk=pk, location=Batch.Location.FLOOR,
    )
    if not user_can_access_branch(request.user, batch.branch_id):
        messages.error(request, 'That batch belongs to another branch.')
        return redirect('alerts_center')

    if not batch.expiry_date or batch.expiry_date > timezone.localdate():
        messages.error(
            request,
            f'{batch.product.product_name} at {batch.branch.branch_name} is not past its expiry date.'
        )
        return redirect('alerts_center')

    with transaction.atomic():
        locked = Batch.objects.select_for_update().get(pk=batch.pk)
        before = locked.quantity
        if before > 0:
            locked.quantity = 0
            locked.save(update_fields=['quantity'])

            # Batches back the aggregate row's quantity_on_hand — writing
            # off a lot has to bring the row down with it, the same as any
            # other floor-stock-reducing movement.
            inv = get_object_or_404(
                Inventory.objects.select_for_update(),
                product=locked.product, branch=locked.branch,
            )
            inv_before = inv.quantity_on_hand
            inv.quantity_on_hand = max(inv.quantity_on_hand - before, 0)
            inv.save()

            StockMovement.objects.create(
                inventory=inv,
                location_type=StockMovement.LocationType.FLOOR,
                movement_type=StockMovement.MovementType.MANUAL_OUT,
                quantity_change=-before,
                quantity_before=inv_before,
                quantity_after=inv.quantity_on_hand,
                notes=f'Expired batch {locked.batch_number} written off by '
                      f'{request.user.get_full_name() or request.user.username}',
                created_by=request.user,
            )

    messages.success(
        request,
        f'{batch.product.product_name} at {batch.branch.branch_name} written off ({before} unit{"s" if before != 1 else ""}).'
    )
    return redirect('alerts_center')


# ─────────────────────────────────────────────
# STORE FLOOR INVENTORY VIEW
# ─────────────────────────────────────────────

def _inventory_list_context(request):
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
    # A non-numeric value (tampered URL) must not reach the ORM as a pk
    # lookup — .filter(branch_id='abc') raises ValueError rather than
    # returning no rows, since the pk field is numeric.
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''

    if search_query:
        records = records.filter(
            models.Q(product__product_name__icontains=search_query) |
            models.Q(product__sku_code__icontains=search_query)
        )
    if branch_filter:
        records = records.filter(branch_id=branch_filter)
    if category_filter:
        records = records.filter(product__category_id=category_filter)

    records = list(records)

    # Keys are template-safe (no spaces) so they can be used with dot lookup.
    counts = {'in_stock': 0, 'low': 0, 'critical': 0, 'no_stock': 0}
    key_for = {
        'Critical': 'critical', 'Low Stock': 'low',
        'In Stock': 'in_stock', 'No Stock': 'no_stock',
    }
    matching = []
    for r in records:
        # Computed live from quantity_on_hand/reorder_level, not from the
        # stored Inventory.restock flag — that flag is only ever recomputed
        # inside Inventory.save(), so anything that bypasses save() (bulk
        # update, a data import) leaves it stale with nothing to catch it.
        status = stock_status(r.quantity_on_hand, r.reorder_level)
        counts[key_for[status]] += 1
        if status_filter and status != status_filter:
            continue
        matching.append((r, status))

    # One table row per BATCH, not per product/branch — a restock with a
    # different expiry never merges into an existing row this way (see the
    # Batch model's docstring). Each batch row still carries its parent
    # product/branch's aggregate qty_on_hand/reorder_level/status, since
    # those are row-level facts a single lot doesn't have its own version of.
    pairs = {(r.product_id, r.branch_id) for r, _ in matching}
    batches_by_pair = {}
    if pairs:
        batch_qs = Batch.objects.filter(
            location=Batch.Location.FLOOR, quantity__gt=0
        ).order_by(models.F('expiry_date').asc(nulls_last=True))
        for b in batch_qs:
            key = (b.product_id, b.branch_id)
            if key in pairs:
                batches_by_pair.setdefault(key, []).append(b)

    rows = []
    for r, status in matching:
        batches = batches_by_pair.get((r.product_id, r.branch_id))
        if batches:
            for b in batches:
                rows.append({'inv': r, 'status': status, 'batch': b})
        else:
            # No batch to show (typically a 0-quantity "No Stock" row) —
            # still needs a row, or it would simply vanish from the table.
            rows.append({'inv': r, 'status': status, 'batch': None})

    return {
        'rows': rows,
        'counts': counts,
        'branches': cached_branches() if not user_branch else [],
        'categories': cached_categories(),
        'search_query': search_query,
        'branch_filter': branch_filter,
        'category_filter': category_filter,
        'status_filter': status_filter,
        'user_branch': user_branch,
        'can_adjust': request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER],
        'adjust_products': Product.objects.order_by('product_name'),
    }


@login_required(login_url='accounts:login')
def inventory_list(request):
    """Stock per Branch — real-time levels across branches, with status."""
    # Suppliers/customers must never reach this: get_user_branch() returns None
    # for them, which the branch filter below would read as "show everything".
    if request.user.role not in INTERNAL_ROLES:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    return render(request, 'inventory/inventory_list.html', _inventory_list_context(request))


@login_required(login_url='accounts:login')
def inventory_edit(request, pk):
    """Set the reorder threshold for one existing Inventory row. Owner/Branch
    Manager only — Staff can view Floor Stock but not change it, matching
    Stock Adjustment's own gating.

    Expiry is no longer set here: it lives on that row's Batches now (see
    the Batch model's docstring), each with its own lot and expiry_date, so
    there is no single "the row's expiry" left to edit — receiving a
    delivery or a stock adjustment is what sets a lot's expiry going
    forward.
    """
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'Only the Owner or a Branch Manager can edit Floor Stock.')
        return redirect('inventory_list')
    if request.method != 'POST':
        return redirect('inventory_list')

    inv = get_object_or_404(Inventory, pk=pk)
    if not user_can_access_branch(request.user, inv.branch_id):
        messages.error(request, 'That inventory record belongs to another branch.')
        return redirect('inventory_list')

    reorder_level = request.POST.get('reorder_level', '').strip()
    if not reorder_level.isdigit():
        messages.error(request, 'Enter a valid reorder level.')
        return redirect('inventory_list')

    inv.reorder_level = int(reorder_level)
    inv.save()

    messages.success(
        request,
        f'Reorder level for {inv.product.product_name} at {inv.branch.branch_name} updated.'
    )
    return redirect('inventory_list')


@login_required(login_url='accounts:login')
def stock_adjustment_add(request):
    """Direct floor-stock correction — bumps Inventory.quantity_on_hand
    without going through the normal Warehouse -> StockTransfer chain, so
    it's restricted to Owner/Branch Manager and exists for corrections,
    spot-counts, and initial seeding, not as a substitute for withdrawal
    slips. Every adjustment is logged as a StockMovement for the same
    traceability the transfer flow gets."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'Only the Owner or a Branch Manager can make a stock adjustment.')
        return redirect('inventory_list')

    if request.method != 'POST':
        return redirect('inventory_list')

    user_branch = get_user_branch(request.user)
    product_id = request.POST.get('product_id', '').strip()
    branch_id = user_branch.id if user_branch else request.POST.get('branch_id', '').strip()
    quantity = request.POST.get('quantity', '').strip()
    expiry_date = request.POST.get('expiry_date', '').strip()

    product = Product.objects.filter(pk=product_id).first() if product_id else None
    branch = Branch.objects.filter(pk=branch_id).first() if branch_id else None

    errors = []
    if not product:
        errors.append('Choose a valid product — scan a barcode or pick one from the list.')
    if not branch:
        errors.append('Choose a valid branch.')
    if not quantity.isdigit() or int(quantity) <= 0:
        errors.append('Enter a quantity greater than zero.')

    if errors:
        for e in errors:
            messages.error(request, e)
        context = _inventory_list_context(request)
        context.update({
            'show_adjust_modal': True,
            'adjust_form_data': request.POST,
        })
        return render(request, 'inventory/inventory_list.html', context)

    with transaction.atomic():
        inv, _created = Inventory.objects.get_or_create(product=product, branch=branch)
        qty_before = inv.quantity_on_hand
        inv.quantity_on_hand += int(quantity)
        inv.save()

        StockMovement.objects.create(
            inventory=inv,
            location_type=StockMovement.LocationType.FLOOR,
            movement_type=StockMovement.MovementType.ADJUSTMENT,
            quantity_change=int(quantity),
            quantity_before=qty_before,
            quantity_after=inv.quantity_on_hand,
            notes=f'Manual stock adjustment by {request.user.get_full_name() or request.user.username}',
            created_by=request.user,
        )

        # A correction/spot-count is its own lot too — it must not silently
        # widen an existing batch's quantity (or worse, have no batch at all
        # and drift the row's batches out of sync with quantity_on_hand).
        Batch.objects.create(
            product=product,
            branch=branch,
            location=Batch.Location.FLOOR,
            quantity=int(quantity),
            received_date=timezone.localdate(),
            expiry_date=parse_date(expiry_date) if expiry_date else None,
            notes='Manual stock adjustment',
        )

    messages.success(request, f'Added {quantity} to {product.product_name} at {branch.branch_name}.')
    return redirect('inventory_list')


# ─────────────────────────────────────────────
# WAREHOUSE STOCK VIEW
# ─────────────────────────────────────────────

@login_required(login_url='accounts:login')
def warehouse_stock_list(request):
    if request.user.role not in [
        User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF
    ]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    # Get the warehouse for this user's branch
    user_branch = get_user_branch(request.user)

    # Owner-only branch filter — a Manager/Staff account is already pinned to
    # its own single warehouse above, so a selector would do nothing for them.
    branch_filter = request.GET.get('branch', '') if not user_branch else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''

    if user_branch:
        try:
            warehouse = Warehouse.objects.get(branch=user_branch)
            stock_records = WarehouseStock.objects.filter(
                warehouse=warehouse
            ).select_related('product', 'product__category', 'warehouse', 'warehouse__branch')
        except Warehouse.DoesNotExist:
            messages.error(
                request,
                'No warehouse is set up for your branch yet. '
                'Ask the Owner to create one.'
            )
            return redirect('inventory_list')
    else:
        # Owner sees all warehouses, narrowed to one via branch_filter if set.
        stock_records = WarehouseStock.objects.select_related(
            'product', 'product__category', 'warehouse', 'warehouse__branch'
        ).all()
        warehouse = None
        if branch_filter:
            stock_records = stock_records.filter(warehouse__branch_id=branch_filter)

    search_query = request.GET.get('q', '')
    category_filter = request.GET.get('category', '')
    if search_query:
        stock_records = stock_records.filter(
            models.Q(product__product_name__icontains=search_query) |
            models.Q(product__sku_code__icontains=search_query)
        )
    if category_filter:
        stock_records = stock_records.filter(product__category_id=category_filter)

    # Computed live from quantity_on_hand/reorder_level, not the stored
    # restock_flag — same reasoning as Inventory's floor-stock status: a
    # write that bypasses save() would leave the stored flag stale with
    # nothing to catch it, so this reads the real numbers every time.
    stock_records = list(stock_records)
    counts = {'in_stock': 0, 'low': 0, 'critical': 0, 'no_stock': 0}
    key_for = {
        'Critical': 'critical', 'Low Stock': 'low',
        'In Stock': 'in_stock', 'No Stock': 'no_stock',
    }
    matching = [(s, stock_status(s.quantity_on_hand, s.reorder_level)) for s in stock_records]
    for _, status in matching:
        counts[key_for[status]] += 1

    # One table row per BATCH, not per product/warehouse — see
    # inventory.views._inventory_list_context's identical treatment of
    # Floor Stock, and the Batch model's own docstring for why. Batch has no
    # warehouse FK (it's keyed by branch — a branch has exactly one
    # warehouse), so rows are matched on (product, branch) throughout.
    pairs = {(s.product_id, s.warehouse.branch_id) for s, _ in matching}
    batches_by_pair = {}
    if pairs:
        batch_qs = Batch.objects.filter(
            location=Batch.Location.WAREHOUSE, quantity__gt=0
        ).order_by(models.F('expiry_date').asc(nulls_last=True))
        for b in batch_qs:
            key = (b.product_id, b.branch_id)
            if key in pairs:
                batches_by_pair.setdefault(key, []).append(b)

    rows = []
    for s, status in matching:
        batches = batches_by_pair.get((s.product_id, s.warehouse.branch_id))
        if batches:
            for b in batches:
                rows.append({'stock': s, 'status': status, 'batch': b})
        else:
            rows.append({'stock': s, 'status': status, 'batch': None})

    return render(request, 'inventory/warehouse_stock_list.html', {
        'active_page': 'warehouse_stock',
        'rows': rows,
        'counts': counts,
        'warehouse': warehouse,
        'categories': cached_categories(),
        'search_query': search_query,
        'category_filter': category_filter,
        'branches': cached_branches() if not user_branch else [],
        'branch_filter': branch_filter,
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        # Distinct products, not len(rows) — rows is now one-per-batch, so a
        # product with two lots would otherwise count itself twice.
        'total_products': len(matching),
        'can_edit_reorder': request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER],
        # Always available to any internal role — when there's no single
        # pinned branch (Owner), warehouse_stock_request shows a branch
        # picker first rather than the button being hidden outright.
        'can_request_to_floor': True,
    })


@login_required(login_url='accounts:login')
def warehouse_stock_edit(request, pk):
    """Set the reorder threshold for one WarehouseStock row. Owner/Branch
    Manager only, matching Floor Stock's inventory_edit.

    Previously Staff could set this row's expiry date here (just not its
    reorder level) — that's no longer a thing this endpoint does at all:
    expiry now lives on that row's Batches, each with its own lot (see the
    Batch model's docstring), so there is no single row-level expiry left
    for Staff to set, and nothing else remains here for them to edit.
    """
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'Only the Owner or a Branch Manager can edit Warehouse Stock.')
        return redirect('warehouse_stock_list')
    if request.method != 'POST':
        return redirect('warehouse_stock_list')

    ws = get_object_or_404(WarehouseStock, pk=pk)
    if not user_can_access_branch(request.user, ws.warehouse.branch_id):
        messages.error(request, 'That warehouse stock record belongs to another branch.')
        return redirect('warehouse_stock_list')

    reorder_level = request.POST.get('reorder_level', '').strip()
    if not reorder_level.isdigit():
        messages.error(request, 'Enter a valid reorder level.')
        return redirect('warehouse_stock_list')

    ws.reorder_level = int(reorder_level)
    ws.save()

    messages.success(
        request,
        f'Warehouse reorder level for {ws.product.product_name} '
        f'at {ws.warehouse.branch.branch_name} updated.'
    )
    return redirect('warehouse_stock_list')


# ─────────────────────────────────────────────
# STOCK MOVEMENT LOG — the audit trail behind Stock Adjustments' Movement
# Log tab. StockMovement rows are already created by every stock-affecting
# action in the app (PO receiving, a released restock/write-off, a sale) —
# this only reads and formats them, it never moves stock itself.
# ─────────────────────────────────────────────

def _describe_movement(m):
    """(product name, source label, destination label) for one StockMovement.
    The model only records a single location + a signed quantity, so the
    from/to pair is derived here from movement_type — the one place this
    mapping lives, so the log and any future consumer of it agree."""
    if m.inventory_id:
        branch = m.inventory.branch
        product_name = m.inventory.product.product_name
    elif m.warehouse_stock_id:
        branch = m.warehouse_stock.warehouse.branch
        product_name = m.warehouse_stock.product.product_name
    else:
        # The Inventory/WarehouseStock row this movement pointed at was
        # later deleted (SET_NULL) — the audit record still deserves to
        # show up, just without a live product/branch to describe.
        return '(record removed)', '—', '—'

    branch_name = branch.branch_name if branch else '—'
    mt = StockMovement.MovementType

    if m.movement_type == mt.PROCUREMENT_RECEIVED:
        return product_name, 'Supplier', f'{branch_name} Warehouse'
    if m.movement_type == mt.WAREHOUSE_TO_FLOOR:
        return product_name, f'{branch_name} Warehouse', f'{branch_name} Floor'
    if m.movement_type == mt.FLOOR_TO_WAREHOUSE:
        return product_name, f'{branch_name} Floor', f'{branch_name} Warehouse'
    if m.movement_type == mt.SALE:
        return product_name, f'{branch_name} Floor', 'Customer sale'
    if m.movement_type == mt.MANUAL_OUT:
        return product_name, f'{branch_name} Floor', 'Written off'
    if m.movement_type == mt.MANUAL_IN:
        return product_name, 'Manual entry', f'{branch_name} Floor'
    if m.movement_type == mt.ADJUSTMENT:
        return product_name, f'{branch_name} Floor', 'Correction'
    return product_name, '—', '—'


def _movement_log(branch_ids=None):
    """Every stock movement as one log row (when, product, type, quantity,
    source, destination, who) — a complete audit trail across PO receiving,
    restock requests, write-offs, and sales, all in one place.

    A restock (WAREHOUSE_TO_FLOOR) writes two StockMovement rows — one
    deduction on the warehouse side, one credit on the floor side, sharing
    a reference_id — so only the warehouse-side row is shown here, as a
    single "moved X units" entry, rather than two rows that would read like
    they cancel each other out.
    """
    qs = StockMovement.objects.select_related(
        'inventory__branch', 'inventory__product',
        'warehouse_stock__warehouse__branch', 'warehouse_stock__product',
        'created_by',
    ).exclude(
        movement_type=StockMovement.MovementType.WAREHOUSE_TO_FLOOR,
        location_type=StockMovement.LocationType.FLOOR,
    ).order_by('-created_at')

    if branch_ids is not None:
        qs = qs.filter(
            models.Q(inventory__branch_id__in=branch_ids) |
            models.Q(warehouse_stock__warehouse__branch_id__in=branch_ids)
        )

    rows = []
    for m in qs:
        product, source, destination = _describe_movement(m)
        rows.append({
            'movement': m,
            'product': product,
            'source': source,
            'destination': destination,
            'quantity': abs(m.quantity_change),
        })
    return rows


# ─────────────────────────────────────────────
# WITHDRAWAL SLIP (STOCK TRANSFER) VIEWS
# ─────────────────────────────────────────────

@role_required(OWNER, BRANCH_MANAGER, STAFF)
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
    # Owner-only branch filter — Manager/Staff are already pinned to their own
    # branch's transfers above, so a selector would do nothing for them.
    branch_filter = request.GET.get('branch', '') if not user_branch else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''
    if branch_filter:
        transfers = transfers.filter(source_branch_id=branch_filter)

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

    # Summary stats for the top cards.
    # Half-open aware-datetime bounds, NOT created_at__month=/__year= —
    # `created_at` is a DateTimeField, so __month= is compiled through
    # CONVERT_TZ on MySQL and silently matches zero rows here, pinning this
    # tile to 0 forever. See core.timebounds.
    # localtime(), not now(): now() is UTC under USE_TZ, so on the 1st or
    # last day of a month it reports the wrong month for Asia/Manila.
    today = timezone.localtime().date()
    month_start, month_end = local_month_bounds(today.year, today.month)

    total_released_this_month = transfers.filter(
        status=StockTransfer.Status.RELEASED,
        created_at__gte=month_start,
        created_at__lt=month_end,
    ).count()

    pending_acknowledgment = transfers.filter(
        status=StockTransfer.Status.RELEASED
    ).count()

    flagged_discrepancies = transfers.filter(
        status=StockTransfer.Status.DISCREPANCY
    ).count()

    # The Movement Log tab shares this page, so the same branch choice narrows
    # it too — [int(branch_filter)] when the Owner has picked one, otherwise
    # the pre-existing role-based scoping (None = every branch for the Owner).
    movement_branch_ids = [int(branch_filter)] if branch_filter else branch_ids_for_user(request.user)

    return render(request, 'inventory/transfer_list.html', {
        'transfers': transfers,
        'movement_log': _movement_log(movement_branch_ids),
        'search_query': search_query,
        'status_filter': status_filter,
        'purpose_filter': purpose_filter,
        'date_from': date_from,
        'date_to': date_to,
        'branches': cached_branches() if not user_branch else [],
        'branch_filter': branch_filter,
        'status_choices': StockTransfer.Status.choices,
        'purpose_choices': StockTransfer.Purpose.choices,
        'total_released_this_month': total_released_this_month,
        'pending_acknowledgment': pending_acknowledgment,
        'flagged_discrepancies': flagged_discrepancies,
        # Names the window the card above counts. From `today`, which is
        # already localtime()-derived, so it cannot disagree with the
        # month_start/month_end bounds the count itself was filtered on.
        'month_label': today.strftime('%B'),
    })


class _InsufficientStockError(Exception):
    """Raised inside a transfer_complete atomic block to abort the whole
    release (and roll back everything applied so far in the loop) when the
    current stock no longer covers an already-approved quantity."""


def _safe_positive_int(value):
    """int(value) if it's a clean positive integer, else None — used instead
    of a bare int() in the transfer-quantity parsing below, which used to
    raise an unhandled ValueError (500) on any non-numeric POST value."""
    if value is None:
        return None
    value = str(value).strip()
    if not value.isdigit():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _try_auto_approve(transfer, is_restock):
    """If the requester is the Owner, approve this brand-new transfer in
    full immediately — there is nobody above the Owner to approve it, so
    transfer_approve's separate step would otherwise just make them approve
    their own request. Falls back to leaving it Pending (unchanged) if any
    item doesn't have enough stock for its full requested quantity right
    now, the same shortfall transfer_approve's own check would reject —
    auto-approval only ever grants exactly what a manual approval already
    would have.

    Must be called from inside the same transaction that created `transfer`
    and its items, before anything outside this function reads their
    Pending/0-approved state.
    """
    if transfer.requested_by is None or transfer.requested_by.role != User.Role.OWNER:
        return

    items = list(transfer.items.select_related('product').all())
    for item in items:
        if is_restock:
            stock = WarehouseStock.objects.select_for_update().filter(
                warehouse=transfer.warehouse, product=item.product
            ).first()
        else:
            stock = Inventory.objects.select_for_update().filter(
                branch=transfer.source_branch, product=item.product
            ).first()
        if stock is None or item.quantity_requested > stock.quantity_on_hand:
            return  # leave Pending for a human to resolve the shortfall

    for item in items:
        item.quantity_approved = item.quantity_requested
        item.save(update_fields=['quantity_approved'])
    transfer.status = StockTransfer.Status.APPROVED
    transfer.approved_by = transfer.requested_by
    transfer.save(update_fields=['status', 'approved_by'])


@login_required(login_url='accounts:login')
def warehouse_stock_request(request):
    """Dedicated "Request Stock to Floor" entry point, reached from the
    Warehouse Stock page — always a Branch Restock, no Purpose picker.
    Separate on purpose from New Stock Adjustment (transfer_create below),
    which now only handles floor-level write-offs, so the two actions can
    never be confused for each other.

    Staff/Manager are pinned to their own branch as before. The Owner has no
    single branch to default to, so they pick one via ?branch=<id> (or the
    branch_id field on resubmit) — same pattern as the Floor Stock page's
    Stock Adjustment modal, which shows a branch picker under the same
    condition rather than hiding the action outright."""
    if request.user.role not in [
        User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER
    ]:
        messages.error(request, 'You do not have permission to request stock.')
        return redirect('warehouse_stock_list')

    user_branch = get_user_branch(request.user)
    if user_branch:
        target_branch = user_branch
    else:
        branch_id = request.GET.get('branch') or request.POST.get('branch_id')
        target_branch = Branch.objects.filter(pk=branch_id).first() if branch_id else None

    if not target_branch:
        # Owner hasn't picked a branch yet — show the picker instead of the
        # item form (there's nothing to list warehouse stock FROM yet).
        return render(request, 'inventory/warehouse_stock_request.html', {
            'warehouse': None,
            'branches': cached_branches(),
        })

    try:
        warehouse = Warehouse.objects.get(branch=target_branch)
    except Warehouse.DoesNotExist:
        messages.error(request, f'No warehouse found for {target_branch.branch_name}.')
        return redirect('warehouse_stock_list')

    warehouse_stock = WarehouseStock.objects.filter(
        warehouse=warehouse, quantity_on_hand__gt=0
    ).select_related('product')

    if request.method == 'POST':
        transfer_date = request.POST.get('transfer_date') or date.today()
        notes = request.POST.get('notes', '')
        product_ids = request.POST.getlist('product_id')
        quantities = request.POST.getlist('quantity_requested')

        valid_items = [
            (pid, _safe_positive_int(qty)) for pid, qty in zip(product_ids, quantities)
            if pid and _safe_positive_int(qty) is not None
        ]

        if not valid_items:
            messages.error(request, 'Please add at least one product.')
            return render(request, 'inventory/warehouse_stock_request.html', {
                'warehouse_stock': warehouse_stock,
                'warehouse': warehouse,
            })

        with transaction.atomic():
            transfer = StockTransfer.objects.create(
                warehouse=warehouse,
                source_branch=warehouse.branch,
                destination_branch=target_branch,
                purpose=StockTransfer.Purpose.BRANCH_RESTOCK,
                requested_by=request.user,
                status=StockTransfer.Status.PENDING,
                transfer_date=transfer_date,
                notes=notes,
            )
            for product_id, qty in valid_items:
                StockTransferItem.objects.create(
                    transfer=transfer,
                    product_id=product_id,
                    quantity_requested=qty,
                    quantity_approved=0,
                )

            _try_auto_approve(transfer, is_restock=True)

        if transfer.status == StockTransfer.Status.APPROVED:
            messages.success(
                request,
                f'Stock request {transfer.slip_number} approved automatically — '
                f'ready to release.'
            )
        else:
            messages.success(
                request,
                f'Stock request {transfer.slip_number} created — awaiting approval.'
            )
        return redirect('transfer_list')

    return render(request, 'inventory/warehouse_stock_request.html', {
        'warehouse_stock': warehouse_stock,
        'warehouse': warehouse,
    })


@login_required(login_url='accounts:login')
def transfer_create(request):
    """New Stock Adjustment — floor-level write-offs only (damaged/spoiled,
    sample/display, other). Restocking the floor FROM the warehouse is now
    its own dedicated flow (see warehouse_stock_request, reached from the
    Warehouse Stock page's "Request Stock to Floor" button) — keeping the
    two entry points separate is the whole point: a write-off never touches
    the warehouse, and a restock request never touches the floor directly."""
    if request.user.role not in [
        User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER
    ]:
        messages.error(request, 'You do not have permission to create stock withdrawals.')
        return redirect('transfer_list')

    user_branch = get_user_branch(request.user)

    # StockTransfer.warehouse is a required FK even for a floor write-off —
    # it's what identifies which branch this slip belongs to. The write-off
    # path below never reads or writes WarehouseStock through it.
    try:
        warehouse = Warehouse.objects.get(
            branch=user_branch or Branch.objects.first()
        )
    except Warehouse.DoesNotExist:
        messages.error(request, 'No warehouse found for your branch.')
        return redirect('transfer_list')

    floor_stock = Inventory.objects.filter(
        branch=warehouse.branch, quantity_on_hand__gt=0
    ).select_related('product')

    write_off_purposes = [
        c for c in StockTransfer.Purpose.choices
        if c[0] != StockTransfer.Purpose.BRANCH_RESTOCK
    ]

    if request.method == 'POST':
        purpose = request.POST.get('purpose')
        if purpose not in dict(write_off_purposes):
            purpose = StockTransfer.Purpose.OTHER  # guards against a tampered POST
        notes = request.POST.get('notes', '')
        transfer_date = request.POST.get('transfer_date') or date.today()
        product_ids = request.POST.getlist('product_id')
        quantities = request.POST.getlist('quantity_requested')

        # Validate at least one item
        valid_items = [
            (pid, _safe_positive_int(qty)) for pid, qty in zip(product_ids, quantities)
            if pid and _safe_positive_int(qty) is not None
        ]

        if not valid_items:
            messages.error(request, 'Please add at least one product.')
            return render(request, 'inventory/transfer_form.html', {
                'floor_stock': floor_stock,
                'purpose_choices': write_off_purposes,
            })

        with transaction.atomic():
            transfer = StockTransfer.objects.create(
                warehouse=warehouse,
                source_branch=warehouse.branch,
                destination_branch=None,  # write-offs go nowhere
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
                    quantity_requested=qty,
                    quantity_approved=0,
                )

            _try_auto_approve(transfer, is_restock=False)

        if transfer.status == StockTransfer.Status.APPROVED:
            messages.success(
                request,
                f'Stock Adjustment {transfer.slip_number} approved automatically — '
                f'ready to release.'
            )
        else:
            messages.success(
                request,
                f'Stock Adjustment {transfer.slip_number} created successfully. '
                f'Awaiting approval.'
            )
        return redirect('transfer_list')

    return render(request, 'inventory/transfer_form.html', {
        'floor_stock': floor_stock,
        'purpose_choices': write_off_purposes,
    })


@login_required(login_url='accounts:login')
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
        messages.error(request, 'That stock withdrawal is not available to you.')
        return redirect('transfer_list')

    items = transfer.items.select_related('product').all()

    return render(request, 'inventory/transfer_detail.html', {
        'transfer': transfer,
        'items': items,
    })


@login_required(login_url='accounts:login')
def transfer_approve(request, pk):
    if request.user.role not in [User.Role.BRANCH_MANAGER, User.Role.OWNER]:
        messages.error(request, 'Only Branch Managers and Owners can approve transfers.')
        return redirect('transfer_list')

    transfer = get_object_or_404(StockTransfer, pk=pk)

    if not user_can_access_branch(request.user, transfer.source_branch_id):
        messages.error(request, 'That stock withdrawal belongs to another branch.')
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
                is_restock = transfer.purpose == StockTransfer.Purpose.BRANCH_RESTOCK

                for item, approved_qty in zip(items, approved_quantities):
                    # A bad/negative value here used to either 500 (bare
                    # int()) or, worse, pass a negative straight through —
                    # which on release would SUBTRACT a negative number,
                    # i.e. add stock. Clamped to 0 so it just approves nothing.
                    approved_qty = _safe_positive_int(approved_qty) or 0

                    # Branch Restock draws from the warehouse; write-offs
                    # (damaged/spoiled, sample/display, other) draw from
                    # what's already on the floor being written off.
                    # select_for_update() locks the row for the rest of this
                    # transaction so a second, concurrently-approved transfer
                    # against the same product can't read the same
                    # not-yet-committed quantity_on_hand.
                    if is_restock:
                        stock = WarehouseStock.objects.select_for_update().filter(
                            warehouse=transfer.warehouse, product=item.product
                        ).first()
                        location_label = 'warehouse'
                    else:
                        stock = Inventory.objects.select_for_update().filter(
                            branch=transfer.source_branch, product=item.product
                        ).first()
                        location_label = 'floor'

                    if stock is None:
                        messages.error(
                            request,
                            f'{item.product.product_name} not found in {location_label} stock.'
                        )
                        all_valid = False
                        break
                    if approved_qty > stock.quantity_on_hand:
                        messages.error(
                            request,
                            f'Not enough {location_label} stock for '
                            f'{item.product.product_name}. '
                            f'Available: {stock.quantity_on_hand}'
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


@login_required(login_url='accounts:login')
def transfer_complete(request, pk):
    if request.user.role not in [User.Role.BRANCH_MANAGER, User.Role.OWNER]:
        messages.error(request, 'Only Branch Managers and Owners can release transfers.')
        return redirect('transfer_list')

    transfer = get_object_or_404(StockTransfer, pk=pk)

    if not user_can_access_branch(request.user, transfer.source_branch_id):
        messages.error(request, 'That stock withdrawal belongs to another branch.')
        return redirect('transfer_list')

    if transfer.status != StockTransfer.Status.APPROVED:
        messages.error(request, 'Transfer must be approved before it can be released.')
        return redirect('transfer_detail', pk=pk)

    if request.method == 'POST':
        is_restock = transfer.purpose == StockTransfer.Purpose.BRANCH_RESTOCK

        # What was actually picked off the shelf, per item — scanned or typed
        # on the release screen. A POST without these fields (the old direct
        # "Release Stock" button, or any bookmarked form) means "release
        # exactly what was approved", which is what this used to always do.
        released_map = {}
        posted_ids = request.POST.getlist('item_id')
        posted_qtys = request.POST.getlist('quantity_released')
        posted_methods = request.POST.getlist('release_method')
        for idx, (item_id, qty) in enumerate(zip(posted_ids, posted_qtys)):
            parsed = _safe_positive_int(qty)
            released_map[str(item_id)] = {
                'qty': 0 if parsed is None else parsed,
                'method': (
                    posted_methods[idx] if idx < len(posted_methods) else ''
                ),
            }

        try:
            with transaction.atomic():
                any_short = False
                for item in transfer.items.select_related('product').all():
                    if item.quantity_approved <= 0:
                        continue

                    posted = released_map.get(str(item.pk))
                    qty_release = (
                        item.quantity_approved if posted is None else posted['qty']
                    )

                    # Releasing more than was approved would route around the
                    # approval itself — the approver decided the ceiling, and
                    # a picker cannot raise it by scanning extra units.
                    if qty_release > item.quantity_approved:
                        raise _InsufficientStockError(
                            f'{item.product.product_name}: {qty_release} picked but only '
                            f'{item.quantity_approved} approved. Re-approve the slip if '
                            f'more should go out.'
                        )

                    item.quantity_released = qty_release
                    if posted is not None:
                        item.release_method = (
                            StockTransferItem.ReleaseMethod.SCAN
                            if posted['method'] == 'Scan'
                            else StockTransferItem.ReleaseMethod.MANUAL
                        )
                    item.save(update_fields=['quantity_released', 'release_method'])

                    if qty_release < item.quantity_approved:
                        any_short = True
                    if qty_release <= 0:
                        continue

                    if is_restock:
                        # 1. Deduct from warehouse stock. select_for_update()
                        # locks the row, and the check below re-validates
                        # against the CURRENT quantity_on_hand rather than
                        # trusting the snapshot from approval time — another
                        # transfer approved against the same product may have
                        # already consumed the stock in between. Without this,
                        # two approved transfers could both release and drive
                        # quantity_on_hand negative.
                        ws = get_object_or_404(
                            WarehouseStock.objects.select_for_update(),
                            warehouse=transfer.warehouse,
                            product=item.product
                        )
                        if qty_release > ws.quantity_on_hand:
                            raise _InsufficientStockError(
                                f'Not enough warehouse stock for {item.product.product_name} to '
                                f'release this transfer — available: {ws.quantity_on_hand}, '
                                f'being released: {qty_release}.'
                            )
                        qty_before_warehouse = ws.quantity_on_hand
                        ws.quantity_on_hand -= qty_release
                        ws.save()

                        # Log warehouse movement
                        StockMovement.objects.create(
                            warehouse_stock=ws,
                            location_type=StockMovement.LocationType.WAREHOUSE,
                            movement_type=StockMovement.MovementType.WAREHOUSE_TO_FLOOR,
                            quantity_change=-qty_release,
                            quantity_before=qty_before_warehouse,
                            quantity_after=ws.quantity_on_hand,
                            reference_id=transfer.pk,
                            notes=f'Stock withdrawal {transfer.slip_number}',
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
                            inv.quantity_on_hand += qty_release
                            inv.save()

                            # Log floor movement
                            StockMovement.objects.create(
                                inventory=inv,
                                location_type=StockMovement.LocationType.FLOOR,
                                movement_type=StockMovement.MovementType.WAREHOUSE_TO_FLOOR,
                                quantity_change=qty_release,
                                quantity_before=qty_before_floor,
                                quantity_after=inv.quantity_on_hand,
                                reference_id=transfer.pk,
                                notes=f'Withdrawal Slip {transfer.slip_number}',
                                created_by=request.user,
                            )

                            # 3. Batches: consume the SOURCE warehouse's
                            # nearest-expiry lots first, and create one
                            # matching FLOOR batch per lot consumed — this is
                            # what stops a restock with a different expiry
                            # from merging into whatever the floor already
                            # had. Two lots split across this one release
                            # become two floor batches, each keeping its own
                            # real expiry_date.
                            consumed = Batch.consume_fefo(
                                item.product, transfer.source_branch,
                                Batch.Location.WAREHOUSE, qty_release,
                            )
                            for source_batch, taken in consumed:
                                Batch.objects.create(
                                    product=item.product,
                                    branch=transfer.destination_branch,
                                    location=Batch.Location.FLOOR,
                                    source_batch=source_batch,
                                    procurement=source_batch.procurement,
                                    quantity=taken,
                                    received_date=timezone.localdate(),
                                    expiry_date=source_batch.expiry_date,
                                    notes=f'Restocked via {transfer.slip_number}',
                                )
                            # Safety net: the aggregate WarehouseStock row
                            # above already confirmed enough TOTAL quantity
                            # existed, but if its batches don't quite cover
                            # qty_release (a gap somewhere, not expected once
                            # every write path stays batch-in-sync), the
                            # floor side still received the full amount, so
                            # it needs a batch for whatever wasn't traceable
                            # to a specific lot rather than silently drifting
                            # out of sync with quantity_on_hand.
                            untracked = qty_release - sum(t for _, t in consumed)
                            if untracked > 0:
                                Batch.objects.create(
                                    product=item.product,
                                    branch=transfer.destination_branch,
                                    location=Batch.Location.FLOOR,
                                    quantity=untracked,
                                    received_date=timezone.localdate(),
                                    expiry_date=None,
                                    notes=f'Restocked via {transfer.slip_number} '
                                          f'(no matching warehouse batch found)',
                                )
                    else:
                        # Write-off (damaged/spoiled, sample/display, other):
                        # deducted straight from the floor — it's already on
                        # the shelf, never touched the warehouse — and there's
                        # no destination to credit. Same select_for_update +
                        # re-check as the restock branch above: an
                        # over-approved write-off used to silently clamp to 0
                        # via max(0, ...) instead of being rejected.
                        inv = get_object_or_404(
                            Inventory.objects.select_for_update(),
                            branch=transfer.source_branch, product=item.product
                        )
                        if qty_release > inv.quantity_on_hand:
                            raise _InsufficientStockError(
                                f'Not enough floor stock for {item.product.product_name} to '
                                f'write off — available: {inv.quantity_on_hand}, '
                                f'being written off: {qty_release}.'
                            )
                        qty_before_floor = inv.quantity_on_hand
                        inv.quantity_on_hand -= qty_release
                        inv.save()

                        StockMovement.objects.create(
                            inventory=inv,
                            location_type=StockMovement.LocationType.FLOOR,
                            movement_type=StockMovement.MovementType.MANUAL_OUT,
                            quantity_change=-qty_release,
                            quantity_before=qty_before_floor,
                            quantity_after=inv.quantity_on_hand,
                            reference_id=transfer.pk,
                            notes=f'Withdrawal Slip {transfer.slip_number} — {transfer.purpose}',
                            created_by=request.user,
                        )
                        # A write-off pulls from the floor's own nearest-
                        # expiry batches first — the stock most worth
                        # clearing out is exactly what FEFO already orders
                        # ahead of everything else.
                        Batch.consume_fefo(
                            item.product, transfer.source_branch,
                            Batch.Location.FLOOR, qty_release,
                        )

                released_total = sum(
                    i.quantity_released for i in transfer.items.all()
                )
                if is_restock:
                    # Still needs the destination branch to confirm receipt.
                    transfer.status = StockTransfer.Status.RELEASED
                else:
                    # A write-off has no separate receiving party — releasing
                    # it IS the whole transaction, so it's done in one step.
                    transfer.status = StockTransfer.Status.ACKNOWLEDGED
                    transfer.acknowledged_by = request.user
                    transfer.received_qty = released_total
                transfer.save()
        except _InsufficientStockError as e:
            messages.error(request, str(e))
            return redirect('transfer_detail', pk=pk)

        short_note = (
            ' Less was picked than approved — the shortfall is recorded per item.'
            if any_short else ''
        )
        if is_restock:
            messages.success(
                request,
                f'Withdrawal Slip {transfer.slip_number} released. '
                f'Stock has been moved to the store floor.{short_note}'
            )
        else:
            messages.success(
                request,
                f'Withdrawal Slip {transfer.slip_number} completed — '
                f'floor stock adjusted for {transfer.purpose}.{short_note}'
            )
        return redirect('transfer_detail', pk=pk)

    # GET: the release screen — pick the goods, scanning or counting by hand,
    # then post the per-item quantities above. This used to redirect straight
    # back to the detail page, because releasing was a single button with no
    # per-item step to show.
    items = transfer.items.select_related('product').all()
    return render(request, 'inventory/transfer_release.html', {
        'transfer': transfer,
        'items': items,
    })


@login_required(login_url='accounts:login')
@require_POST
def transfer_scan(request, pk):
    """AJAX: resolve one scanned barcode against an approved slip's items.

    The release-step twin of procurement.delivery_scan, and deliberately the
    same shape — both answer "does this physical item match a line on this
    document", and staff who learn one screen should not have to learn the
    other. It differs in what it refuses: a product that is not on the slip
    cannot be added here, because a picker adding lines would route around
    the approval the slip exists to enforce.
    """
    if request.user.role not in [User.Role.BRANCH_MANAGER, User.Role.OWNER]:
        return JsonResponse({'ok': False, 'detail': 'Access denied.'}, status=403)

    transfer = get_object_or_404(StockTransfer, pk=pk)
    if not user_can_access_branch(request.user, transfer.source_branch_id):
        return JsonResponse({'ok': False, 'detail': 'Wrong branch.'}, status=403)
    if transfer.status != StockTransfer.Status.APPROVED:
        return JsonResponse({'ok': False, 'detail': 'This slip is not awaiting release.'}, status=400)

    code = (request.POST.get('code') or '').strip()
    if not code:
        return JsonResponse({'ok': False, 'detail': 'No code supplied.'}, status=400)

    product = (
        Product.objects.filter(barcode__iexact=code).first()
        or Product.objects.filter(sku_code__iexact=code).first()
    )
    item = transfer.items.filter(product=product).first() if product else None

    if item:
        return JsonResponse({
            'ok': True, 'status': 'matched',
            'item_id': item.pk,
            'product_name': item.product.product_name,
            'quantity_approved': item.quantity_approved,
        })
    if product:
        return JsonResponse({
            'ok': True, 'status': 'not_on_slip',
            'product_name': product.product_name,
        })
    return JsonResponse({'ok': True, 'status': 'unknown', 'code': code})


# ─────────────────────────────────────────────
# WITHDRAWAL SLIP: ACKNOWLEDGMENT (receiving side)
# The destination branch confirms it received the released stock, or flags
# a discrepancy if the received quantity differs from what was released.
# ─────────────────────────────────────────────

@login_required(login_url='accounts:login')
def transfer_confirm_receipt(request, pk):
    """Destination-branch staff confirms receipt of a RELEASED slip."""
    if request.user.role not in [
        User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER
    ]:
        messages.error(request, 'You do not have permission to confirm receipt.')
        return redirect('transfer_list')

    transfer = get_object_or_404(StockTransfer, pk=pk)

    if not user_can_access_branch(request.user, transfer.destination_branch_id):
        messages.error(request, 'That stock withdrawal belongs to another branch.')
        return redirect('transfer_list')

    if transfer.status != StockTransfer.Status.RELEASED:
        messages.error(request, 'Only released withdrawals can be acknowledged.')
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


@login_required(login_url='accounts:login')
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
        messages.error(request, 'That stock withdrawal belongs to another branch.')
        return redirect('transfer_list')

    if transfer.status != StockTransfer.Status.RELEASED:
        messages.error(request, 'Only released withdrawals can be flagged.')
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
                # Keep batches in step with the corrected aggregate: fewer
                # units arriving than released means some of what
                # transfer_complete just credited to a batch never actually
                # showed up, so it comes back out FEFO-first; more arriving
                # means an untraceable extra lot (nobody knows which
                # warehouse batch it really came from).
                actual_change = inv.quantity_on_hand - qty_before
                if actual_change < 0:
                    Batch.consume_fefo(
                        first_item.product, transfer.destination_branch,
                        Batch.Location.FLOOR, -actual_change,
                    )
                elif actual_change > 0:
                    Batch.objects.create(
                        product=first_item.product,
                        branch=transfer.destination_branch,
                        location=Batch.Location.FLOOR,
                        quantity=actual_change,
                        received_date=timezone.localdate(),
                        expiry_date=None,
                        notes=f'Discrepancy on {transfer.slip_number}: {reason}',
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