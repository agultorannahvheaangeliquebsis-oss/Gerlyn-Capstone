import os
import re
import uuid
import hashlib

from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models, transaction, IntegrityError
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST
from decimal import Decimal, InvalidOperation as DecimalException
from urllib.parse import urlencode

from .models import (
    ProductCategory, Product, ProductImage, ProductPriceTier, Inventory,
    Warehouse, WarehouseStock, Batch,
    StockTransfer, StockTransferItem, StockMovement,
    ProductImportMappingTemplate, OpeningStockImport,
)
from .services import (
    ExpiryProcessingError, OpeningStockDuplicateError, OpeningStockValidationError,
    canonical_opening_fingerprint, commit_opening_stock_import,
    credit_floor_stock, expire_due_batches, opening_preview_summary,
    parse_opening_stock_workbook, reverse_opening_stock_import,
    validate_opening_stock_rows,
)
from .alerts import alert_summary, stock_status
from accounts.models import User
from branches.models import Branch
from core.permissions import INTERNAL_ROLES, OWNER, BRANCH_MANAGER, STAFF, role_required
from core.module_permissions import (
    ADD, DELETE, EDIT, FLOOR_STOCK, PRODUCTS, WAREHOUSE_STOCK, WITHDRAWALS,
    user_can_perform_module_action,
)
from core.refdata import bust_categories_cache, cached_branches, cached_categories
from core.scoping import (
    branch_for_user, branch_ids_for_user, scope_queryset, user_can_access_branch,
)
from core.timebounds import local_month_bounds, range_from_request


# ─────────────────────────────────────────────
# HELPER: get branch for current user
# ─────────────────────────────────────────────

# One definition, in core.scoping. This module used to carry its own copy
# wrapped in a bare `except Exception: return None`, which turned any error
# (a missing profile, a DB hiccup) into "no branch" — and every caller reads
# "no branch" as "Owner, show everything". Aliased rather than renamed at the
# call sites below so the two implementations cannot drift apart again.
#
# NOTE: this returns None for an Owner AND for a Manager/Staff whose profile
# branch is NULL. Never use it to decide whether to filter a queryset — use
# scope_queryset() / branch_ids_for_user(), which distinguish the two and
# fail CLOSED. It is fine for "which branch do I default this form to", where
# None means "ask".
get_user_branch = branch_for_user


# ─────────────────────────────────────────────
# PRODUCT VIEWS
# ─────────────────────────────────────────────

def _product_list_context(request):
    products = (
        Product.objects.select_related('category')
        .prefetch_related('inventory_records__branch', 'images', 'price_tiers')
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
        # The topbar branch chip renders this, so choosing a branch in the
        # toolbar moves the chip too instead of leaving it reading "All
        # Branches" over a list that is no longer all branches. str() because
        # branch_filter is still the raw (validated-digits) GET string.
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        # Every other active filter, for the topbar branch switcher to carry
        # along — same idea as _daterange.html's daterange_carry.
        'branch_switch_carry': [('q', search_query), ('category', category_filter)],
        'total_count': Product.objects.count(),
        'can_add': (
            request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
            and user_can_perform_module_action(request.user, PRODUCTS, ADD)
        ),
        'can_edit': (
            request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
            and user_can_perform_module_action(request.user, PRODUCTS, EDIT)
        ),
        'can_delete': (
            request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
            and user_can_perform_module_action(request.user, PRODUCTS, DELETE)
        ),
        # `branches` is unrestricted — the Availability column needs every
        # branch regardless of role. The topbar switcher must not, or a
        # Manager/Staff account (show_branch_filter False) would get a live
        # control with nothing for it to do.
        'switch_branches': branches if show_branch_filter else [],
    }


def _posted_tier_rows(form_data):
    """The bulk-price rows exactly as they were typed, for redisplaying a form
    that failed validation. Parallel getlists, the same shape the PO modal's
    line items use."""
    if form_data is None:
        return []
    quantities = form_data.getlist('tier_min_quantity')
    prices = form_data.getlist('tier_price')
    rows = [
        {'min_quantity': q.strip(), 'unit_price': p.strip()}
        for q, p in zip(quantities, prices)
    ]
    return [r for r in rows if r['min_quantity'] or r['unit_price']]


def _parse_price_tiers(form_data, retail_price):
    """Validate the posted bulk-price ladder into [(min_quantity, price), ...].

    Returns (tiers, errors). Blank rows are dropped rather than refused — the
    form always renders spare rows, and an untouched one is not a mistake.

    The rules exist because a bad ladder silently mis-bills rather than
    crashing: a tier at or above retail charges MORE for buying more, a tier
    at quantity 1 quietly replaces the retail price outright, and a ladder
    whose prices do not fall as quantities rise means a customer is punished
    for crossing a threshold. Each is caught here, where it can still be
    explained, rather than at the register.
    """
    errors = []
    tiers = []
    seen_quantities = set()

    for index, row in enumerate(_posted_tier_rows(form_data), start=1):
        raw_quantity = row['min_quantity']
        raw_price = row['unit_price']

        if not raw_quantity or not raw_price:
            errors.append(
                f'Bulk price {index}: fill in both the quantity and the price, '
                f'or clear the row.'
            )
            continue
        if not raw_quantity.isdigit() or int(raw_quantity) < 2:
            errors.append(
                f'Bulk price {index}: the quantity must be a whole number of 2 '
                f'or more — a break at 1 would just replace the retail price.'
            )
            continue
        try:
            price = Decimal(raw_price)
        except DecimalException:
            errors.append(f'Bulk price {index}: "{raw_price}" is not a valid price.')
            continue
        if price <= 0:
            errors.append(f'Bulk price {index}: the price must be more than ₱0.')
            continue

        quantity = int(raw_quantity)
        if quantity in seen_quantities:
            errors.append(
                f'Bulk price {index}: there is already a break at {quantity} — '
                f'each quantity can only have one price.'
            )
            continue
        seen_quantities.add(quantity)

        if retail_price is not None and price >= retail_price:
            errors.append(
                f'Bulk price {index}: ₱{price} is not below the retail price of '
                f'₱{retail_price}, so buying {quantity} would cost more per unit, '
                f'not less.'
            )
            continue

        tiers.append((quantity, price))

    tiers.sort()
    for (lower_qty, lower_price), (higher_qty, higher_price) in zip(tiers, tiers[1:]):
        if higher_price >= lower_price:
            errors.append(
                f'The price at {higher_qty}+ (₱{higher_price}) must be lower than '
                f'the price at {lower_qty}+ (₱{lower_price}) — buying more should '
                f'never cost more per unit.'
            )
            break

    return tiers, errors


def _edit_form_values(product, form_data=None):
    """Field values for the Edit Product page: posted values win (so a
    validation error redisplays what the user typed), otherwise fall back
    to the product's current values (fresh GET of the edit page)."""
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
        'description': pick('description', product.description),
        # Not pick()-able: the ladder is a list of rows, and a redisplay has
        # to show exactly the rows that were posted — including ones the user
        # cleared, which a fall-back to the stored tiers would resurrect.
        'price_tiers': (
            _posted_tier_rows(form_data) if form_data is not None
            else [
                {'min_quantity': tier.min_quantity, 'unit_price': tier.unit_price}
                for tier in product.price_tiers.all()
            ]
        ),
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

    import_step = request.GET.get('import')
    if import_step and context['can_add']:
        context.update(_import_modal_context(request, import_step))

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
        'price_tiers': [
            {'min_quantity': tier.min_quantity, 'unit_price': str(tier.unit_price)}
            for tier in product.price_tiers.all()
        ],
        'edit_url': reverse('product_edit', args=[product.id]),
    })


@login_required(login_url='accounts:login')
def product_add(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to add products.')
        return redirect('product_list')

    if request.method != 'POST':
        context = {
            'mode': 'add',
            'categories': cached_categories(),
            'branches': cached_branches(),
            'unit_choices': Product.UnitOfMeasure.choices,
            'next_sku': Product.next_sku_code(),
            'add_selected_branches': None,  # None = default all branches checked
            'add_tier_rows': [],  # empty = fresh form; the page JS opens three blank rows
        }
        return render(request, 'inventory/product_form.html', context)

    product_name = request.POST.get('product_name', '').strip()
    barcode = request.POST.get('barcode', '').strip() or None
    unit_price = request.POST.get('unit_price', '').strip()
    description = request.POST.get('description', '').strip()
    branch_ids = request.POST.getlist('branches')

    errors = []
    if not product_name:
        errors.append('Product name is required.')
    if not unit_price:
        errors.append('Retail price is required.')
    if barcode and Product.objects.filter(barcode__iexact=barcode).exists():
        errors.append(f'Barcode "{barcode}" is already assigned to another product.')

    # Tiers are validated against the retail price, so they can only be checked
    # once it parses. A bad retail price is already reported above; skipping the
    # comparison here avoids stacking a confusing second complaint on top of it.
    try:
        retail_for_tiers = Decimal(unit_price) if unit_price else None
    except DecimalException:
        retail_for_tiers = None
        if unit_price:
            errors.append('Retail price must be a valid number.')
    if retail_for_tiers is not None and retail_for_tiers <= 0:
        errors.append('Retail price must be more than ₱0.')
    price_tiers, tier_errors = _parse_price_tiers(request.POST, retail_for_tiers)
    errors.extend(tier_errors)

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
                    description=description,
                    # Tax/discount flags. All three boxes start unticked, so
                    # an absent value simply means "not ticked" — a product is
                    # only discount-eligible when someone says so.
                    is_vat_exempt=bool(request.POST.get('is_vat_exempt')),
                    is_pwd_eligible=bool(request.POST.get('is_pwd_eligible')),
                    is_senior_eligible=bool(request.POST.get('is_senior_eligible')),
                )
                # Row by row rather than bulk_create: bulk_create emits no signals, so the
                # audit log would record the ladder being cleared but never what replaced it.
                for quantity, price in price_tiers:
                    ProductPriceTier.objects.create(
                        product=product, min_quantity=quantity, unit_price=price
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

    context = {
        'mode': 'add',
        'categories': cached_categories(),
        'branches': cached_branches(),
        'unit_choices': Product.UnitOfMeasure.choices,
        'next_sku': Product.next_sku_code(),
        'add_errors': errors,
        'add_form_data': request.POST,
        'add_selected_branches': branch_ids,
        'add_tier_rows': _posted_tier_rows(request.POST),
    }
    return render(request, 'inventory/product_form.html', context)


@login_required(login_url='accounts:login')
def product_edit(request, pk):
    product = get_object_or_404(Product, pk=pk)

    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to edit products.')
        return redirect('product_list')

    if request.method != 'POST':
        context = {
            'mode': 'edit',
            'categories': cached_categories(),
            'unit_choices': Product.UnitOfMeasure.choices,
            'edit_product': product,
            'edit_values': _edit_form_values(product),
        }
        return render(request, 'inventory/product_form.html', context)

    product_name = request.POST.get('product_name', '').strip()
    barcode = request.POST.get('barcode', '').strip() or None
    unit_price = request.POST.get('unit_price', '').strip()
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

    try:
        retail_for_tiers = Decimal(unit_price) if unit_price else None
    except DecimalException:
        retail_for_tiers = None
        if unit_price:
            errors.append('Retail price must be a valid number.')
    if retail_for_tiers is not None and retail_for_tiers <= 0:
        errors.append('Retail price must be more than ₱0.')
    price_tiers, tier_errors = _parse_price_tiers(request.POST, retail_for_tiers)
    errors.extend(tier_errors)

    if not errors:
        try:
            with transaction.atomic():
                product.category_id = request.POST.get('category') or None
                # sku_code is locked on the edit form — never trust a posted
                # value for it, so it can't be tampered with client-side.
                product.product_name = product_name
                product.barcode = barcode
                product.unit_of_measure = request.POST.get('unit_of_measure')
                product.unit_price = unit_price
                product.description = description
                product.is_vat_exempt = bool(request.POST.get('is_vat_exempt'))
                product.is_pwd_eligible = bool(request.POST.get('is_pwd_eligible'))
                product.is_senior_eligible = bool(request.POST.get('is_senior_eligible'))
                product.save()

                # The posted rows ARE the ladder — replaced wholesale rather
                # than diffed, so a row the user cleared actually disappears.
                # Sales already hold their own frozen unit_price, so rewriting
                # tiers never reprices a sale that has already happened.
                product.price_tiers.all().delete()
                # Row by row rather than bulk_create: bulk_create emits no signals, so the
                # audit log would record the ladder being cleared but never what replaced it.
                for quantity, price in price_tiers:
                    ProductPriceTier.objects.create(
                        product=product, min_quantity=quantity, unit_price=price
                    )

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

    context = {
        'mode': 'edit',
        'categories': cached_categories(),
        'unit_choices': Product.UnitOfMeasure.choices,
        'edit_product': product,
        'edit_errors': errors,
        'edit_values': _edit_form_values(product, request.POST),
    }
    return render(request, 'inventory/product_form.html', context)


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

    # The confirm step is a modal on the product list now (confirm-dialog.js
    # off the row's Delete button). Anyone landing here directly — bookmark,
    # back button, typed URL — just goes to the list; same as product_edit.
    return redirect('product_list')


# ─────────────────────────────────────────────
# PRODUCT IMPORT / EXPORT (Excel)
#
# Export downloads the catalog as .xlsx with one column per field below;
# Import reads a .xlsx back in through a 3-step wizard (upload -> map
# columns -> preview & confirm), matching by Barcode to decide whether a
# row updates an existing product or creates a new one. A saved
# ProductImportMappingTemplate lets a repeat import skip re-mapping.
#
# The uploaded file lives on local disk for the lifetime of the wizard
# (session holds only its path + the chosen mapping, never the parsed
# rows) — _parse_import_rows() re-reads and re-validates it fresh at both
# the preview and the commit step, so the two can never disagree about
# what a row means.
# ─────────────────────────────────────────────

# (field_key, column header — used for both the export file and the
# mapping form's label, required)
_MAPPABLE_FIELDS = [
    ('product_name', 'Product Name', True),
    ('barcode', 'Barcode', False),
    ('category', 'Category', False),
    ('unit_of_measure', 'Unit of Measure', False),
    ('unit_price', 'Retail Price', True),
    # A spreadsheet column cannot hold a variable-length ladder, so the import
    # carries a fixed three rungs — the depth a supplier price list actually
    # prints. Products needing a fourth get it in the product form; import
    # never deletes tiers it has no column for.
    ('tier1_min_quantity', 'Bulk Qty 1', False),
    ('tier1_price', 'Bulk Price 1', False),
    ('tier2_min_quantity', 'Bulk Qty 2', False),
    ('tier2_price', 'Bulk Price 2', False),
    ('tier3_min_quantity', 'Bulk Qty 3', False),
    ('tier3_price', 'Bulk Price 3', False),
    ('description', 'Description', False),
    ('is_vat_exempt', 'VAT Exempt', False),
    ('is_pwd_eligible', 'PWD Eligible', False),
    ('is_senior_eligible', 'Senior Eligible', False),
]

# How many bulk rungs the spreadsheet carries — see _MAPPABLE_FIELDS.
_IMPORT_TIER_SLOTS = 3

_IMPORT_TMP_DIR = os.path.join(settings.BASE_DIR, 'tmp_uploads', 'product_imports')

# Common spellings for "yes" across a hand-built spreadsheet — anything else
# (including blank) reads as False. Matches what product_export_xlsx writes
# ("Yes"/"No"), so an exported file round-trips through Import unchanged.
_TRUTHY_CELL_VALUES = {'yes', 'y', 'true', '1', 'x'}

_PLAIN_NUMBER_RE = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$')
_COMMA_NUMBER_RE = re.compile(
    r'^[+-]?(?:\d{1,3}(?:,\d{3})+)(?:\.\d*)?$'
)


def _clear_import_session(request):
    """Drop the in-progress import's session state and delete its temp file,
    if any. Safe to call when there is nothing to clear."""
    state = request.session.pop('product_import', None)
    request.session.modified = True
    if state and state.get('path') and os.path.exists(state['path']):
        try:
            os.remove(state['path'])
        except OSError:
            pass  # already gone or locked — not worth failing the request over


def _parse_bool_cell(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    return str(value).strip().lower() in _TRUTHY_CELL_VALUES


def _normalize_empty_cell(value):
    """Return None for every representation of an empty spreadsheet cell."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _parse_decimal_cell(value, *, allow_currency=False):
    """Convert an Excel number or a conventional numeric string to Decimal.

    Commas are accepted only as thousands separators, rather than silently
    turning a malformed value such as ``12,34`` into a different number.
    """
    value = _normalize_empty_cell(value)
    if value is None or isinstance(value, bool):
        raise ValueError('not a populated number')

    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        number = Decimal(str(value))
    else:
        text = str(value).strip()
        if allow_currency and text.startswith('₱'):
            text = text[1:].strip()
        pattern = _COMMA_NUMBER_RE if ',' in text else _PLAIN_NUMBER_RE
        if not pattern.fullmatch(text):
            raise ValueError('not a number')
        number = Decimal(text.replace(',', ''))

    if not number.is_finite():
        raise ValueError('not a finite number')
    return number


def _normalize_barcode_cell(value):
    """Keep barcodes as identifiers while dropping an Excel-added .0."""
    value = _normalize_empty_cell(value)
    if value is None:
        return ''

    if not isinstance(value, bool) and isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except DecimalException:
            return str(value).strip()
        if number.is_finite():
            formatted = format(number, 'f')
            return (
                formatted.split('.')[0]
                if number == number.to_integral_value()
                else formatted
            )

    text = str(value).strip()
    whole_number_with_decimal = re.fullmatch(r'([+-]?\d+)\.0+', text)
    return whole_number_with_decimal.group(1) if whole_number_with_decimal else text


def _match_unit_of_measure(value):
    """Case-insensitive match against Product.UnitOfMeasure (by either the
    stored value or its display label) — None if blank or unrecognized;
    the caller decides whether that's an error or a default."""
    text = str(value).strip().lower()
    if not text:
        return None
    for choice_value, choice_label in Product.UnitOfMeasure.choices:
        if text == choice_value.lower() or text == choice_label.lower():
            return choice_value
    return None


def _guess_mapping(headers):
    """Best-effort default mapping by matching an uploaded header's text to
    a field, ignoring case/spacing/punctuation — so a sensibly-titled sheet
    needs little or no manual remapping. Never trusted blindly: the mapping
    form always renders (and stays fully editable) regardless of the guess."""
    def norm(s):
        return ''.join(ch for ch in s.lower() if ch.isalnum())

    header_by_norm = {norm(h): h for h in headers}
    aliases = {
        'product_name': ['productname', 'name', 'product', 'item', 'itemname'],
        'barcode': ['barcode', 'upc', 'ean', 'code'],
        'category': ['category', 'categoryname', 'cat'],
        'unit_of_measure': ['unitofmeasure', 'unit', 'uom', 'units'],
        'unit_price': ['retailprice', 'unitprice', 'price', 'srp'],
        'tier1_min_quantity': ['bulkqty1', 'bulkquantity1', 'wholesaleqty', 'bulkqty'],
        'tier1_price': ['bulkprice1', 'wholesaleprice', 'wholesale', 'bulkprice'],
        'tier2_min_quantity': ['bulkqty2', 'bulkquantity2'],
        'tier2_price': ['bulkprice2'],
        'tier3_min_quantity': ['bulkqty3', 'bulkquantity3'],
        'tier3_price': ['bulkprice3'],
        'description': ['description', 'desc', 'notes'],
        'is_vat_exempt': ['vatexempt', 'vat'],
        'is_pwd_eligible': ['pwdeligible', 'pwd'],
        'is_senior_eligible': ['senioreligible', 'senior', 'seniorcitizen'],
    }
    guess = {}
    for field_key, names in aliases.items():
        for name in names:
            if name in header_by_norm:
                guess[field_key] = header_by_norm[name]
                break
    return guess


def _parse_import_rows(path, mapping, *, allow_updates=True):
    """Read the sheet at `path`, apply `mapping` ({field_key: column header
    text}), and return one dict per non-blank data row:

        row_number, product_name, barcode, category_name, unit_of_measure,
        unit_price, price_tiers, description, is_vat_exempt,
        is_pwd_eligible, is_senior_eligible, action, errors, existing_id

    `action` is 'error' (skip on commit), 'update' (barcode matched an
    existing product — existing_id is set), or 'create'. Nothing is written
    to the database here — this is the one place both the preview and the
    commit step compute row meaning, so they can never disagree.

    Matched by column HEADER TEXT, not position: a supplier re-exporting
    the same kind of sheet next month may reorder columns, and a saved
    mapping template should still apply correctly.
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header_row = next(rows_iter, None)
    if header_row is None:
        return []
    headers = [str(h).strip() if h is not None else '' for h in header_row]
    col_index = {h: i for i, h in enumerate(headers) if h}

    def cell(raw_row, field_key):
        header = mapping.get(field_key)
        if not header or header not in col_index:
            return None
        idx = col_index[header]
        return raw_row[idx] if idx < len(raw_row) else None

    seen_barcodes = {}  # barcode -> the first row_number that claimed it
    results = []
    for row_number, raw_row in enumerate(rows_iter, start=2):
        if raw_row is None or all(c is None or str(c).strip() == '' for c in raw_row):
            continue  # a fully blank row is not a product to skip loudly over

        errors = []

        product_name = str(cell(raw_row, 'product_name') or '').strip()
        if not product_name:
            errors.append('Product Name is required.')

        unit_price_raw = _normalize_empty_cell(cell(raw_row, 'unit_price'))
        unit_price = None
        if unit_price_raw is None:
            errors.append('Retail Price is required.')
        else:
            try:
                unit_price = _parse_decimal_cell(unit_price_raw, allow_currency=True)
                if unit_price <= 0:
                    errors.append('Retail Price must be greater than ₱0.')
            except (ValueError, TypeError, DecimalException):
                errors.append(f'Retail Price "{unit_price_raw}" is not a number.')

        # Bulk rungs. Same rules the product form applies (see
        # _parse_price_tiers) so a sheet cannot write a ladder the form would
        # have refused: both cells or neither, quantity 2+, price below retail
        # and falling as quantity rises.
        price_tiers = []
        seen_tier_quantities = set()
        for slot in range(1, _IMPORT_TIER_SLOTS + 1):
            quantity_raw = _normalize_empty_cell(
                cell(raw_row, f'tier{slot}_min_quantity')
            )
            price_raw = _normalize_empty_cell(cell(raw_row, f'tier{slot}_price'))
            quantity_supplied = quantity_raw is not None
            price_supplied = price_raw is not None

            if not quantity_supplied and not price_supplied:
                continue

            tier_quantity = None
            tier_price = None
            if quantity_supplied:
                try:
                    parsed_quantity = _parse_decimal_cell(quantity_raw)
                    if parsed_quantity != parsed_quantity.to_integral_value():
                        errors.append(f'Bulk Qty {slot} must be a whole number.')
                    else:
                        tier_quantity = int(parsed_quantity)
                except (ValueError, TypeError, DecimalException):
                    errors.append(f'Bulk Qty {slot} must be a number.')
            if price_supplied:
                try:
                    tier_price = _parse_decimal_cell(price_raw, allow_currency=True)
                except (ValueError, TypeError, DecimalException):
                    errors.append(f'Bulk Price {slot} must be a number.')

            if quantity_supplied and not price_supplied:
                errors.append(
                    f'Bulk Price {slot} is required when Bulk Qty {slot} is provided.'
                )
            elif price_supplied and not quantity_supplied:
                errors.append(
                    f'Bulk Qty {slot} is required when Bulk Price {slot} is provided.'
                )

            if tier_quantity is None or tier_price is None:
                continue
            if tier_quantity < 2:
                errors.append(f'Bulk Qty {slot} must be 2 or more.')
                continue
            if tier_price <= 0:
                errors.append(f'Bulk Price {slot} must be more than 0.')
                continue
            if tier_quantity in seen_tier_quantities:
                errors.append(f'Bulk Qty {slot} repeats a quantity already priced on this row.')
                continue
            if unit_price is not None and tier_price >= unit_price:
                errors.append(f'Bulk Price {slot} must be below the Retail Price.')
                continue
            seen_tier_quantities.add(tier_quantity)
            price_tiers.append((tier_quantity, tier_price))

        price_tiers.sort()
        for (lower_qty, lower_price), (higher_qty, higher_price) in zip(price_tiers, price_tiers[1:]):
            if higher_price >= lower_price:
                errors.append(
                    f'The price at {higher_qty}+ must be lower than the price at {lower_qty}+.'
                )
                break

        barcode = _normalize_barcode_cell(cell(raw_row, 'barcode'))
        if barcode:
            if barcode in seen_barcodes:
                errors.append(f'Duplicate barcode in this file (also row {seen_barcodes[barcode]}).')
            else:
                seen_barcodes[barcode] = row_number

        uom_raw = _normalize_empty_cell(cell(raw_row, 'unit_of_measure'))
        if uom_raw is None:
            unit_of_measure = Product.UnitOfMeasure.PIECE
        else:
            unit_of_measure = _match_unit_of_measure(uom_raw)
            if unit_of_measure is None:
                errors.append(f'Unrecognized Unit of Measure "{uom_raw}".')
                unit_of_measure = Product.UnitOfMeasure.PIECE

        existing = Product.objects.filter(barcode__iexact=barcode).first() if barcode else None
        if existing and not allow_updates:
            errors.append(
                'This barcode already belongs to a product; Edit permission '
                'is required to update it through an import.'
            )
        action = 'error' if errors else ('update' if existing else 'create')

        results.append({
            'row_number': row_number,
            'product_name': product_name,
            'barcode': barcode,
            'category_name': str(cell(raw_row, 'category') or '').strip(),
            'unit_of_measure': unit_of_measure,
            'unit_price': unit_price,
            'price_tiers': price_tiers,
            'description': str(cell(raw_row, 'description') or '').strip(),
            'is_vat_exempt': _parse_bool_cell(cell(raw_row, 'is_vat_exempt')),
            'is_pwd_eligible': _parse_bool_cell(cell(raw_row, 'is_pwd_eligible')),
            'is_senior_eligible': _parse_bool_cell(cell(raw_row, 'is_senior_eligible')),
            'action': action,
            'errors': errors,
            'existing_id': existing.pk if existing else None,
        })
    return results


@login_required(login_url='accounts:login')
def product_export_xlsx(request):
    """Download the whole catalog as .xlsx — one column per _MAPPABLE_FIELDS
    entry, plus a leading reference-only SKU column, so the same file can be
    edited and fed straight back through Import with little to no remapping."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to export the catalog.')
        return redirect('product_list')

    import openpyxl
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Products'

    headers = ['SKU Code'] + [label for _key, label, _required in _MAPPABLE_FIELDS]
    ws.append(headers)
    for c in ws[1]:
        c.font = openpyxl.styles.Font(bold=True)

    for p in Product.objects.select_related('category').prefetch_related(
        'price_tiers'
    ).order_by('product_name'):
        # Padded to the fixed slot count so every row lines up under the same
        # headers; a product with deeper tiers than the sheet carries exports
        # its first three and keeps the rest (import never deletes unmapped ones).
        tier_cells = []
        for tier in list(p.price_tiers.all())[:_IMPORT_TIER_SLOTS]:
            tier_cells.extend([tier.min_quantity, float(tier.unit_price)])
        tier_cells.extend([''] * (_IMPORT_TIER_SLOTS * 2 - len(tier_cells)))
        ws.append([
            p.sku_code,
            p.product_name,
            p.barcode or '',
            p.category.category_name if p.category else '',
            p.unit_of_measure,
            float(p.unit_price),
            *tier_cells,
            p.description,
            'Yes' if p.is_vat_exempt else 'No',
            'Yes' if p.is_pwd_eligible else 'No',
            'Yes' if p.is_senior_eligible else 'No',
        ])

    for i in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 20

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f'gerlyn-products-{timezone.localdate().isoformat()}.xlsx'
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


def _import_map_fields_context(request, headers, current_mapping):
    """Shared by product_list (?import=map, a fresh GET) and
    product_import_map's own rejected-submit redisplay — same modal, same
    shape of context either way."""
    templates = ProductImportMappingTemplate.objects.filter(
        models.Q(created_by=request.user) | models.Q(is_shared=True)
    ).select_related('created_by')
    fields = [
        {'key': key, 'label': label, 'required': required, 'selected': current_mapping.get(key, '')}
        for key, label, required in _MAPPABLE_FIELDS
    ]
    return {
        'show_import_map_modal': True,
        'import_headers': headers,
        'import_fields': fields,
        'import_templates': templates,
    }


def _import_modal_context(request, step):
    """Which Import Products modal (if any) product_list should open,
    driven by ?import=upload|map|preview — the wizard now lives entirely in
    modals on top of the product list, so a redirect here is what used to be
    a full page render of product_import_upload/map/preview.html."""
    if step == 'upload':
        return {'show_import_upload_modal': True}

    state = request.session.get('product_import')
    if not state:
        messages.error(request, 'Upload a file to start an import.')
        return {}

    if step == 'map':
        headers = state['headers']
        template_id = request.GET.get('template')
        if template_id:
            templates = ProductImportMappingTemplate.objects.filter(
                models.Q(created_by=request.user) | models.Q(is_shared=True)
            )
            tmpl = templates.filter(pk=template_id).first()
            current_mapping = tmpl.mapping if tmpl else {}
        else:
            current_mapping = state.get('mapping') or _guess_mapping(headers)
        return _import_map_fields_context(request, headers, current_mapping)

    if step == 'preview':
        if not state.get('mapping'):
            messages.error(request, 'Map the columns first.')
            return {}
        path = state['path']
        if not os.path.exists(path):
            _clear_import_session(request)
            messages.error(request, 'Your uploaded file is no longer available — please upload it again.')
            return {}
        rows = _parse_import_rows(
            path,
            state['mapping'],
            allow_updates=user_can_perform_module_action(
                request.user, PRODUCTS, EDIT
            ),
        )
        counts = {
            'total': len(rows),
            'create': sum(1 for r in rows if r['action'] == 'create'),
            'update': sum(1 for r in rows if r['action'] == 'update'),
            'error': sum(1 for r in rows if r['action'] == 'error'),
        }
        return {
            'show_import_preview_modal': True,
            'import_rows': rows[:200],
            'import_counts': counts,
            'import_truncated': len(rows) > 200,
        }

    return {}


@login_required(login_url='accounts:login')
def product_import_upload(request):
    """Step 1: upload the .xlsx file and read its header row. The file is
    saved to a private temp directory (never under MEDIA_ROOT — it's a
    working file for this wizard, not a record the app keeps); only its
    path and headers go into the session.

    The wizard lives in a modal on the product list now, so a GET here
    (bookmark, back button) just sends whoever lands here there with a flag
    so the page JS opens the modal on load — same as product_add/_edit."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to import products.')
        return redirect('product_list')

    if request.method != 'POST':
        return redirect(f"{reverse('product_list')}?import=upload")

    def fail(message):
        messages.error(request, message)
        context = _product_list_context(request)
        context['show_import_upload_modal'] = True
        return render(request, 'inventory/product_list.html', context)

    upload = request.FILES.get('file')
    if not upload or not upload.name.lower().endswith('.xlsx'):
        return fail('Choose a .xlsx file to import.')

    _clear_import_session(request)  # drop any abandoned previous upload

    os.makedirs(_IMPORT_TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(_IMPORT_TMP_DIR, f'{uuid.uuid4().hex}.xlsx')
    with open(tmp_path, 'wb') as f:
        for chunk in upload.chunks():
            f.write(chunk)

    try:
        import openpyxl
        wb = openpyxl.load_workbook(tmp_path, read_only=True, data_only=True)
        header_row = next(wb.active.iter_rows(values_only=True), None)
    except Exception:
        os.remove(tmp_path)
        return fail('Could not read that file — make sure it is a valid .xlsx spreadsheet.')

    headers = [str(h).strip() for h in (header_row or []) if h is not None and str(h).strip()]
    if not headers:
        os.remove(tmp_path)
        return fail('The first row of the sheet must contain column headers.')

    request.session['product_import'] = {'path': tmp_path, 'headers': headers, 'mapping': None}
    return redirect(f"{reverse('product_list')}?import=map")


@login_required(login_url='accounts:login')
def product_import_map(request):
    """Step 2: map each uploaded column to a system field, optionally
    starting from a saved template (?template=<id>, a plain GET so loading
    one is just a link/select — never a state change) and optionally saving
    the mapping just built under a new name before moving on.

    GET only ever reaches here from a bookmark/back button now — the modal
    reads through product_list's ?import=map instead — so it just forwards
    there, template param and all."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to import products.')
        return redirect('product_list')

    state = request.session.get('product_import')
    if not state:
        messages.error(request, 'Upload a file to start an import.')
        return redirect('product_import_upload')

    if request.method != 'POST':
        query = urlencode({'import': 'map', **({'template': request.GET['template']} if request.GET.get('template') else {})})
        return redirect(f"{reverse('product_list')}?{query}")

    headers = state['headers']
    mapping = {}
    for field_key, _label, _required in _MAPPABLE_FIELDS:
        chosen = request.POST.get(f'map_{field_key}', '')
        if chosen and chosen in headers:
            mapping[field_key] = chosen

    errors = [
        f'Map a column to "{label}" — it is required.'
        for field_key, label, required in _MAPPABLE_FIELDS
        if required and field_key not in mapping
    ]

    if errors:
        for e in errors:
            messages.error(request, e)
        context = _product_list_context(request)
        context.update(_import_map_fields_context(request, headers, mapping))
        return render(request, 'inventory/product_list.html', context)

    save_name = request.POST.get('save_template_name', '').strip()
    if save_name:
        ProductImportMappingTemplate.objects.update_or_create(
            created_by=request.user, name=save_name,
            defaults={
                'mapping': mapping,
                'is_shared': request.POST.get('template_visibility') == 'shared',
            },
        )
        messages.success(request, f'Saved mapping template "{save_name}".')

    state['mapping'] = mapping
    request.session['product_import'] = state
    request.session.modified = True
    return redirect(f"{reverse('product_list')}?import=preview")


@login_required(login_url='accounts:login')
def product_import_preview(request):
    """Step 3: POST commits the mapped file — reading the same
    _parse_import_rows() output the preview modal was just shown (computed
    by _import_modal_context via product_list's ?import=preview), so what
    was previewed is exactly what gets applied. A GET here (bookmark, back
    button) just forwards to that modal instead of redoing its work."""
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to import products.')
        return redirect('product_list')

    state = request.session.get('product_import')
    if not state or not state.get('mapping'):
        messages.error(request, 'Map the columns first.')
        return redirect('product_import_upload')

    path = state['path']
    if not os.path.exists(path):
        _clear_import_session(request)
        messages.error(request, 'Your uploaded file is no longer available — please upload it again.')
        return redirect('product_import_upload')

    if request.method != 'POST':
        return redirect(f"{reverse('product_list')}?import=preview")

    rows = _parse_import_rows(
        path,
        state['mapping'],
        allow_updates=user_can_perform_module_action(
            request.user, PRODUCTS, EDIT
        ),
    )
    created = updated = skipped = 0
    any_new_category = False
    with transaction.atomic():
        for row in rows:
            if row['action'] == 'error':
                skipped += 1
                continue

            category = None
            if row['category_name']:
                category = ProductCategory.objects.filter(
                    category_name__iexact=row['category_name']
                ).first()
                if category is None:
                    category = ProductCategory.objects.create(category_name=row['category_name'])
                    any_new_category = True

            if row['action'] == 'update':
                product = Product.objects.get(pk=row['existing_id'])
                updated += 1
            else:
                product = Product(sku_code=Product.next_sku_code())
                created += 1

            product.product_name = row['product_name']
            if row['barcode']:
                product.barcode = row['barcode']
            product.category = category
            product.unit_of_measure = row['unit_of_measure']
            product.unit_price = row['unit_price']
            product.description = row['description']
            product.is_vat_exempt = row['is_vat_exempt']
            product.is_pwd_eligible = row['is_pwd_eligible']
            product.is_senior_eligible = row['is_senior_eligible']
            product.save()

            # A row that priced any bulk rung replaces the whole ladder; a
            # row that priced none leaves it alone. Silence in a sheet that
            # simply has no bulk columns must not wipe tiers the Owner set
            # in the product form — clearing a ladder is done there, where
            # it is a visible act rather than an omission.
            if row['price_tiers']:
                product.price_tiers.all().delete()
                # Row by row rather than bulk_create: bulk_create emits no signals, so the
                # audit log would record the ladder being cleared but never what replaced it.
                for quantity, price in row['price_tiers']:
                    ProductPriceTier.objects.create(
                        product=product, min_quantity=quantity, unit_price=price
                    )

    if any_new_category:
        bust_categories_cache()

    _clear_import_session(request)
    messages.success(
        request,
        f'Import complete — {created} product{"s" if created != 1 else ""} added, '
        f'{updated} updated, {skipped} skipped due to errors.'
    )
    return redirect('product_list')


@login_required(login_url='accounts:login')
@require_POST
def product_import_cancel(request):
    """A real (POST) action, not a plain link — this deletes the temp file
    and its session state, so it must not be something a stray GET (a
    prefetch, a bookmark) can trigger by accident."""
    _clear_import_session(request)
    messages.info(request, 'Import cancelled.')
    return redirect('product_list')


# ─────────────────────────────────────────────
# CATEGORIES
# Product categories drive the catalog filters and the storefront's
# "Shop by Category" section, so they need a real management screen.
# ─────────────────────────────────────────────

def _category_list_context(request):
    """The category table: one flat, alphabetical list."""
    categories = list(ProductCategory.objects.annotate(
        product_count=models.Count('products')
    ).order_by('category_name'))

    return {
        'categories': categories,
        'can_add': (
            request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
            and user_can_perform_module_action(request.user, PRODUCTS, ADD)
        ),
        'can_edit': (
            request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
            and user_can_perform_module_action(request.user, PRODUCTS, EDIT)
        ),
        'can_delete': (
            request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
            and user_can_perform_module_action(request.user, PRODUCTS, DELETE)
        ),
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
            context.update({
                'show_edit_modal': True,
                'edit_category': category,
                'edit_category_products': category.products.order_by('product_name'),
            })

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
    image = request.FILES.get('image')
    remove_image = bool(request.POST.get('remove_image'))

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
        target = category or ProductCategory()
        target.category_name = name
        target.description = description
        if image:
            target.image = image
        elif remove_image and target.pk:
            target.image.delete(save=False)
            target.image = None
        target.save()
        verb = 'updated' if category else 'created'
        messages.success(request, f'Category "{name}" {verb}.')
        bust_categories_cache()
        return redirect('category_list')

    context = _category_list_context(request)
    if category:
        context.update({
            'show_edit_modal': True,
            'edit_category': category,
            'edit_category_products': category.products.order_by('product_name'),
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

    # The confirm step is a modal on the category list now (confirm-dialog.js
    # off the row's Delete button). A direct GET here just goes to the list.
    return redirect('category_list')


# ─────────────────────────────────────────────
# ALERTS CENTER
# Low/critical stock + expiry alerts, branch-scoped. Powered by the shared
# inventory.alerts engine so these numbers match the Dashboard exactly.
# ─────────────────────────────────────────────

@role_required(OWNER, BRANCH_MANAGER, STAFF)
def alerts_center(request):
    # Owner-only branch filter — Manager/Staff are already pinned to their
    # own branch's alerts, so a selector would do nothing for them. Same
    # pattern as Stock Withdrawals (transfer_list).
    user_branch = get_user_branch(request.user)
    branch_filter = request.GET.get('branch', '') if not user_branch else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''

    branch_ids = [int(branch_filter)] if branch_filter else branch_ids_for_user(request.user)
    summary = alert_summary(branch_ids)

    # Purchase orders past their delivery date (P18). Imported here because
    # procurement already depends on inventory, not the other way round.
    from procurement.services import overdue_orders
    overdue_deliveries = list(overdue_orders(branch_ids))
    today = timezone.localdate()
    for po in overdue_deliveries:
        po.late_days = (today - po.delivery_date).days

    return render(request, 'inventory/alerts_center.html', {
        'overdue_deliveries': overdue_deliveries,
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
        'branches': cached_branches() if not user_branch else [],
        'branch_filter': branch_filter,
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Warehouse Stock and Stock Withdrawals' filter_branch.
        'filter_branch': next((b for b in cached_branches() if str(b.id) == branch_filter), None),
        'user_branch': user_branch,
        # No other filters live on this page (the tabs are client-side panel
        # toggles, not query params), so nothing else needs to ride along.
        'branch_switch_carry': [],
    })


@role_required(OWNER, BRANCH_MANAGER, STAFF)
@require_POST
def expiry_write_off(request, pk):
    """Manual fallback for one expired Floor OR Warehouse Stock batch,
    reached from the Expired tab of Alerts Center. Zeroes just that lot —
    not the whole product/branch row, which may still have other,
    unexpired batches — and logs it as a write-off. The daily sweep
    normally handles this automatically; this action uses the same
    service when the scheduler has not run yet.

    Warehouse stock gets this action directly, the same as Floor — there is
    no reason an expired warehouse lot should have to be transferred to the
    floor first just to become eligible for write-off; that would only add
    a pointless extra step (and briefly show it as available on the floor,
    which it never actually was).

    Distinct from inventory_edit (Owner/Branch Manager only, for the
    reorder level): this only ever zeroes a lot that is ALREADY expired, so
    it is open to Staff too as a safe operational fallback.
    """
    batch = get_object_or_404(
        Batch.objects.select_related('product', 'branch'), pk=pk,
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

    try:
        result = expire_due_batches(
            actor=request.user,
            batch_ids=[batch.pk],
            locations=[batch.location],
        )
    except ExpiryProcessingError as exc:
        messages.error(request, str(exc))
        return redirect('alerts_center')

    messages.success(
        request,
        f'{batch.product.product_name} at {batch.branch.branch_name} written off '
        f'({result.unit_count} unit{"s" if result.unit_count != 1 else ""}).'
    )
    return redirect('alerts_center')


# ─────────────────────────────────────────────
# STORE FLOOR INVENTORY VIEW
# ─────────────────────────────────────────────

def _inventory_list_context(request):
    # .sellable() annotates available_qty (quantity_on_hand with any expired
    # batch's quantity already netted out) — used below for Status/counts
    # instead of the raw column, so an expired lot reads as gone from this
    # screen the moment its date passes, not only after the daily sweep
    # actually removes it from quantity_on_hand.
    records = Inventory.objects.select_related(
        'product', 'product__category', 'branch'
    ).sellable()

    # Managers/staff see only their own branch (server-side, not just the UI).
    # scope_queryset, not `if user_branch:`: user_branch is None for an Owner
    # and for a Manager/Staff with a NULL profile branch alike, so the second
    # case fell through unfiltered and saw every branch's stock. user_branch
    # itself is still read below — but only to decide whether to offer the
    # Owner's branch picker, never to scope the data.
    user_branch = get_user_branch(request.user)
    records = scope_queryset(records, request.user, 'branch')

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
    if category_filter:
        records = records.filter(product__category_id=category_filter)
    # The branch filter is deliberately NOT applied to the queryset — the scope
    # bar has to show a row count per branch, and a branch it filtered out has
    # nothing left to count. Applied per-row in the loop below instead, over the
    # already branch-SCOPED queryset, so this stays a UI filter on top of the
    # security boundary rather than a replacement for it. One pass, one query.

    records = list(records)

    # Keys are template-safe (no spaces) so they can be used with dot lookup.
    counts = {'in_stock': 0, 'low': 0, 'critical': 0, 'no_stock': 0}
    key_for = {
        'Critical': 'critical', 'Low Stock': 'low',
        'In Stock': 'in_stock', 'No Stock': 'no_stock',
    }
    matching = []
    branch_totals, branch_alerts = {}, {}
    last_change = None
    for r in records:
        # Computed live from available_qty/reorder_level, not from the
        # stored Inventory.restock flag — that flag is only ever recomputed
        # inside Inventory.save(), so anything that bypasses save() (bulk
        # update, a data import) leaves it stale with nothing to catch it.
        # available_qty (not quantity_on_hand) so a product whose only stock
        # has expired reads as No Stock here immediately, not just once the
        # daily sweep has actually zeroed the batch.
        status = stock_status(r.available_qty, r.reorder_level)

        # Scope-bar tallies: counted before the branch filter, so every segment
        # shows what it would return. The search and category filters do apply
        # to them — they narrow the same question the bar is answering.
        branch_totals[r.branch_id] = branch_totals.get(r.branch_id, 0) + 1
        if status in ('Critical', 'No Stock'):
            branch_alerts[r.branch_id] = branch_alerts.get(r.branch_id, 0) + 1

        if branch_filter and str(r.branch_id) != branch_filter:
            continue

        counts[key_for[status]] += 1
        if r.last_stock_update and (last_change is None or r.last_stock_update > last_change):
            last_change = r.last_stock_update
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
        today = timezone.localdate()
        # Excludes a batch whose expiry_date has arrived — it should not be
        # visible here as a lot to sell from any more, even though it is
        # only actually zeroed out by the daily sweep (or the "Write off"
        # action on Alerts Center's Expired tab). Mirrors sellable()'s own
        # non-expired filter.
        batch_qs = Batch.objects.filter(
            location=Batch.Location.FLOOR, quantity__gt=0
        ).filter(
            models.Q(expiry_date__isnull=True) | models.Q(expiry_date__gt=today)
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

    branches = cached_branches() if not user_branch else []

    # branch_ids_for_user, not user_branch: the latter is None both for an Owner
    # and for a Manager/Staff whose profile branch was nulled, and only the
    # first of those may be handed a picker listing every branch.
    can_pick_branch = branch_ids_for_user(request.user) is None
    all_branches = cached_branches()

    def scope_href(branch_id):
        """One segment's URL — carries the other three filters across, so
        switching branch doesn't silently clear the search you just typed."""
        params = [
            (key, value) for key, value in (
                ('q', search_query),
                ('category', category_filter),
                ('status', status_filter),
                ('branch', str(branch_id) if branch_id else ''),
            ) if value
        ]
        return '?' + urlencode(params) if params else '?'

    scope_branches = []
    if can_pick_branch:
        scope_branches.append({
            'id': '', 'name': 'All Branches', 'href': scope_href(''),
            'total': sum(branch_totals.values()),
            'alerts': sum(branch_alerts.values()),
            'active': not branch_filter,
        })
        for b in all_branches:
            scope_branches.append({
                'id': b.id, 'name': b.branch_name, 'href': scope_href(b.id),
                'total': branch_totals.get(b.id, 0),
                'alerts': branch_alerts.get(b.id, 0),
                'active': branch_filter == str(b.id),
            })

    active_branch = None
    if branch_filter:
        active_branch = next(
            (b for b in all_branches if str(b.id) == branch_filter), None
        )
    can_adjust = (
        request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
        and user_can_perform_module_action(request.user, FLOOR_STOCK, ADD)
    )
    can_edit_reorder = (
        request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
        and user_can_perform_module_action(request.user, FLOOR_STOCK, EDIT)
    )

    return {
        'rows': rows,
        'counts': counts,
        'branches': branches,
        'categories': cached_categories(),
        'search_query': search_query,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this, so an Owner picking a branch
        # in the toolbar moves the chip too instead of leaving it reading
        # "All Branches" over a list that is no longer all branches — same
        # pattern as the Product Master List's filter_branch.
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        'category_filter': category_filter,
        'status_filter': status_filter,
        'user_branch': user_branch,
        # Scope bar
        'scope_branches': scope_branches,
        'can_pick_branch': can_pick_branch,
        'active_branch': active_branch,
        'shelf_total': len(matching),
        'last_change': last_change,
        # The Branch column only earns its width when more than one branch can
        # appear in the table.
        'show_branch_column': can_pick_branch and not branch_filter,
        'table_colspan': (
            7 + int(can_pick_branch and not branch_filter) + int(can_edit_reorder)
        ),
        # Every other active filter, for the topbar branch switcher
        # (partials/_branch_switch.html) to carry along — same idea as
        # _daterange.html's daterange_carry.
        'branch_switch_carry': [
            ('q', search_query), ('category', category_filter), ('status', status_filter),
        ],
        'can_adjust': can_adjust,
        'can_edit_reorder': can_edit_reorder,
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

    context = _inventory_list_context(request)

    if request.user.role == User.Role.OWNER:
        # The Import Opening Stock upload modal is always in the page (see
        # _opening_stock_upload_modal.html), even when it isn't the one
        # being opened, so its date field needs a value in every Owner
        # response — not just the ?opening=upload one.
        context['opening_as_of_date'] = timezone.localdate()
        opening_step = request.GET.get('opening')
        if opening_step:
            context.update(_opening_modal_context(request, opening_step))

    return render(request, 'inventory/inventory_list.html', context)


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
    """Direct single-item floor-stock correction.

    Opening migration has a dedicated audited import flow; supplier stock
    still follows Warehouse -> StockTransfer. This endpoint remains for
    ordinary corrections and spot-counts, with the same StockMovement trail.
    """
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'Only the Owner or a Branch Manager can make a stock adjustment.')
        return redirect('inventory_list')

    if request.method != 'POST':
        return redirect('inventory_list')

    user_branch = get_user_branch(request.user)
    product_id = request.POST.get('product_id', '').strip()
    # Only the Owner may name the branch in the POST. A Branch Manager is
    # pinned to their own, and if they have none (profile.branch is SET_NULL,
    # so deleting a branch leaves exactly that) the adjustment is refused
    # rather than allowed to name any branch — this is a stock WRITE.
    if user_branch:
        branch_id = user_branch.id
    elif request.user.role == User.Role.OWNER:
        branch_id = request.POST.get('branch_id', '').strip()
    else:
        messages.error(
            request,
            'No branch is assigned to your account, so this adjustment cannot '
            'be recorded. Ask the Owner to assign you to a branch.'
        )
        return redirect('inventory_list')
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
    parsed_expiry = None
    if expiry_date:
        try:
            parsed_expiry = parse_date(expiry_date)
        except ValueError:
            parsed_expiry = None
        if parsed_expiry is None:
            errors.append('Enter a valid expiry date.')

    if errors:
        for e in errors:
            messages.error(request, e)
        context = _inventory_list_context(request)
        context.update({
            'show_adjust_modal': True,
            'adjust_form_data': request.POST,
        })
        return render(request, 'inventory/inventory_list.html', context)

    credit_floor_stock(
        product=product,
        branch=branch,
        quantity=int(quantity),
        actor=request.user,
        movement_type=StockMovement.MovementType.ADJUSTMENT,
        notes=f'Manual stock adjustment by {request.user.get_full_name() or request.user.username}',
        expiry_date=parsed_expiry,
        received_date=timezone.localdate(),
    )

    messages.success(request, f'Added {quantity} to {product.product_name} at {branch.branch_name}.')
    return redirect('inventory_list')


# -----------------------------------------------------------------------------
# OPENING STOCK IMPORT
# -----------------------------------------------------------------------------

_OPENING_TMP_DIR = os.path.join(settings.BASE_DIR, 'tmp_uploads', 'opening_stock')
_OPENING_MAX_BYTES = 10 * 1024 * 1024
_OPENING_MAX_ROWS = 5000


def _clear_opening_stock_session(request):
    state = request.session.pop('opening_stock_import', None)
    request.session.modified = True
    if state and state.get('path') and os.path.exists(state['path']):
        try:
            os.remove(state['path'])
        except OSError:
            pass


def _opening_owner_only(request):
    if request.user.role == User.Role.OWNER:
        return None
    messages.error(request, 'Only the Owner can import or reverse opening stock.')
    return redirect('inventory_list')


def _opening_preview_context(request, state):
    path = state.get('path')
    if not path or not os.path.exists(path):
        raise OpeningStockValidationError(
            'The uploaded file is no longer available. Upload it again.'
        )
    as_of_date = parse_date(state['as_of_date'])
    rows = parse_opening_stock_workbook(path, default_received_date=as_of_date)
    if len(rows) > _OPENING_MAX_ROWS:
        raise OpeningStockValidationError(
            f'The file has {len(rows)} rows; the maximum is {_OPENING_MAX_ROWS}.'
        )
    validate_opening_stock_rows(rows)
    canonical = canonical_opening_fingerprint(rows)
    summary = opening_preview_summary(rows, canonical)
    return {
        'preview_rows': rows,
        'preview_summary': summary,
        'opening_state': state,
        'as_of_date': as_of_date,
    }


def _opening_modal_context(request, step):
    """Which Import Opening Stock modal (if any) Floor Stock should open,
    driven by ?opening=upload|preview — the wizard now lives entirely in
    modals on top of inventory_list, so a redirect here is what used to be a
    full page render of opening_stock_import.html. Mirrors
    _import_modal_context for Import Products."""
    if step == 'upload':
        return {'show_opening_upload_modal': True, 'opening_as_of_date': timezone.localdate()}

    state = request.session.get('opening_stock_import')
    if not state:
        messages.error(request, 'Upload a file to start an opening-stock import.')
        return {}
    try:
        context = _opening_preview_context(request, state)
    except OpeningStockValidationError as exc:
        _clear_opening_stock_session(request)
        messages.error(request, str(exc))
        return {}
    context['show_opening_preview_modal'] = True
    return context


@login_required(login_url='accounts:login')
def opening_stock_import(request):
    """Step 1: parse & validate an opening-stock spreadsheet.

    The wizard lives in a modal on Floor Stock now (inventory_list.html), so
    a GET here (bookmark, back button, History's own button) just forwards
    there with a flag so the page JS opens the right modal on load — same as
    product_import_upload. POST still renders its result directly (not a
    redirect) so the preview shown here is exactly what opening_stock_confirm
    commits."""
    denied = _opening_owner_only(request)
    if denied:
        return denied

    if request.method != 'POST':
        state = request.session.get('opening_stock_import')
        step = 'preview' if state else 'upload'
        return redirect(f"{reverse('inventory_list')}?opening={step}")

    def fail(message, as_of_value):
        messages.error(request, message)
        context = _inventory_list_context(request)
        context['show_opening_upload_modal'] = True
        context['opening_as_of_date'] = as_of_value
        return render(request, 'inventory/inventory_list.html', context)

    upload = request.FILES.get('file')
    as_of_text = request.POST.get('as_of_date', '').strip()
    try:
        as_of_date = parse_date(as_of_text) if as_of_text else timezone.localdate()
    except ValueError:
        as_of_date = None
    if as_of_date is None:
        # Not the raw text: the date field's |date filter needs a real date
        # (or None) to format, and a type="date" input only ever sends
        # ISO-format text or nothing anyway.
        return fail('Enter a valid inventory as-of date.', None)
    if not upload or not upload.name.lower().endswith('.xlsx'):
        return fail('Choose a valid .xlsx spreadsheet.', as_of_date)
    if upload.size > _OPENING_MAX_BYTES:
        return fail('The spreadsheet cannot exceed 10 MB.', as_of_date)

    _clear_opening_stock_session(request)
    os.makedirs(_OPENING_TMP_DIR, exist_ok=True)
    path = os.path.join(_OPENING_TMP_DIR, f'{uuid.uuid4().hex}.xlsx')
    digest = hashlib.sha256()
    with open(path, 'wb') as target:
        for chunk in upload.chunks():
            digest.update(chunk)
            target.write(chunk)

    state = {
        'path': path,
        'filename': os.path.basename(upload.name),
        'raw_sha256': digest.hexdigest(),
        'as_of_date': as_of_date.isoformat(),
    }
    try:
        preview_context = _opening_preview_context(request, state)
    except Exception as exc:
        try:
            os.remove(path)
        except OSError:
            pass
        message = str(exc) if isinstance(exc, OpeningStockValidationError) else (
            'Could not read that workbook. Confirm it is a valid .xlsx file.'
        )
        return fail(message, as_of_date)

    request.session['opening_stock_import'] = state
    request.session.modified = True
    context = _inventory_list_context(request)
    context['opening_as_of_date'] = timezone.localdate()
    context.update(preview_context)
    context['show_opening_preview_modal'] = True
    return render(request, 'inventory/inventory_list.html', context)


@login_required(login_url='accounts:login')
@require_POST
def opening_stock_confirm(request):
    denied = _opening_owner_only(request)
    if denied:
        return denied
    state = request.session.get('opening_stock_import')
    if not state:
        messages.error(request, 'Upload and preview an opening-stock file first.')
        return redirect(f"{reverse('inventory_list')}?opening=upload")
    try:
        preview_context = _opening_preview_context(request, state)
        summary = preview_context['preview_summary']
        if summary['duplicate_import']:
            raise OpeningStockDuplicateError(
                f'This opening stock was already imported as batch #{summary["duplicate_import"].pk}.'
            )
        import_batch = commit_opening_stock_import(
            rows=preview_context['preview_rows'],
            filename=state['filename'],
            raw_sha256=state['raw_sha256'],
            canonical_sha256=summary['canonical_sha256'],
            as_of_date=preview_context['as_of_date'],
            actor=request.user,
            warnings_acknowledged=request.POST.get('acknowledge_warnings') == '1',
        )
    except (OpeningStockValidationError, OpeningStockDuplicateError, IntegrityError) as exc:
        messages.error(request, str(exc) or 'The import could not be committed.')
        try:
            preview_context = _opening_preview_context(request, state)
        except OpeningStockValidationError:
            return redirect(f"{reverse('inventory_list')}?opening=upload")
        context = _inventory_list_context(request)
        context['opening_as_of_date'] = timezone.localdate()
        context.update(preview_context)
        context['show_opening_preview_modal'] = True
        return render(request, 'inventory/inventory_list.html', context)

    _clear_opening_stock_session(request)
    messages.success(
        request,
        f'Opening stock import #{import_batch.pk} committed: '
        f'{import_batch.total_quantity} units across {import_batch.row_count} rows.'
    )
    return redirect('opening_stock_import_detail', pk=import_batch.pk)


@login_required(login_url='accounts:login')
@require_POST
def opening_stock_cancel(request):
    denied = _opening_owner_only(request)
    if denied:
        return denied
    _clear_opening_stock_session(request)
    messages.info(request, 'Opening-stock import cancelled.')
    return redirect('inventory_list')


@login_required(login_url='accounts:login')
def opening_stock_import_history(request):
    denied = _opening_owner_only(request)
    if denied:
        return denied
    imports = OpeningStockImport.objects.select_related(
        'uploaded_by', 'reversed_by'
    ).all()
    return render(request, 'inventory/opening_stock_history.html', {
        'imports': imports,
    })


@login_required(login_url='accounts:login')
def opening_stock_import_detail(request, pk):
    denied = _opening_owner_only(request)
    if denied:
        return denied
    import_batch = get_object_or_404(
        OpeningStockImport.objects.select_related('uploaded_by', 'reversed_by'), pk=pk
    )
    rows = import_batch.rows.select_related('product', 'branch').prefetch_related(
        'batches', 'movements'
    )
    return render(request, 'inventory/opening_stock_detail.html', {
        'import_batch': import_batch,
        'import_rows': rows,
    })


@login_required(login_url='accounts:login')
@require_POST
def opening_stock_import_reverse(request, pk):
    denied = _opening_owner_only(request)
    if denied:
        return denied
    try:
        import_batch = reverse_opening_stock_import(
            import_id=pk,
            actor=request.user,
            reason=request.POST.get('reason', ''),
        )
    except (OpeningStockValidationError, OpeningStockImport.DoesNotExist) as exc:
        messages.error(request, str(exc) or 'That import does not exist.')
        return redirect('opening_stock_import_detail', pk=pk)
    messages.success(request, f'Opening stock import #{import_batch.pk} was reversed.')
    return redirect('opening_stock_import_detail', pk=pk)


@login_required(login_url='accounts:login')
def opening_stock_template(request):
    denied = _opening_owner_only(request)
    if denied:
        return denied
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Opening Stock'
    headers = [
        'Barcode', 'Branch', 'Opening Qty', 'Lot/Batch',
        'Expiry Date', 'Received Date',
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.append(['4800361000826', 'Variety Store', 20, 'LOT-001', '2027-01-31', ''])
    ws.append(['4800361390804', 'Mini Mart', 12, '', '', ''])
    for row in range(2, 1002):
        ws.cell(row=row, column=1).number_format = '@'
    for index, width in enumerate((22, 24, 14, 20, 16, 16), start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="gerlyn-opening-stock-template.xlsx"'
    wb.save(response)
    return response


# ─────────────────────────────────────────────
# WAREHOUSE STOCK VIEW
# ─────────────────────────────────────────────

def _branch_scoped_modal_target(request):
    """Which branch a branch-scoped create modal (Warehouse Stock's "Request
    Stock to Floor", Stock Adjustments' "New Stock Adjustment") is scoped to.
    Staff/Manager are pinned to their own branch as always; an Owner has
    none, so this reads branch_id off a resubmitted POST first (redisplaying
    the modal after a validation error) and falls back to the ?branch= GET
    param (the modal's own branch-picker step, or the toolbar's branch
    filter) — kept separate from the main table's branch_filter so a POST
    error redisplay doesn't need a query string to recover the branch.
    """
    user_branch = get_user_branch(request.user)
    if user_branch:
        return user_branch
    # Only the Owner picks a branch here. A Manager/Staff whose profile branch
    # is NULL also lands past the check above — they get no branch (the create
    # modal it feeds is a stock write), not a free choice from the POST/GET.
    if request.user.role != User.Role.OWNER:
        return None
    branch_id = request.POST.get('branch_id') or request.GET.get('branch')
    if branch_id and str(branch_id).isdigit():
        return Branch.objects.filter(pk=branch_id).first()
    return None


def _warehouse_stock_list_context(request):
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
            # .sellable() nets out any expired-lot quantity into available_qty
            # — see _inventory_list_context's identical treatment of Floor
            # Stock, and WarehouseStockQuerySet.sellable()'s own docstring.
            stock_records = WarehouseStock.objects.filter(
                warehouse=warehouse
            ).select_related(
                'product', 'product__category', 'warehouse', 'warehouse__branch'
            ).sellable()
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
        ).sellable()
        warehouse = None
        if branch_filter:
            stock_records = stock_records.filter(warehouse__branch_id=branch_filter)
        # This else is only meant for the Owner, but it is also where a
        # Manager/Staff whose profile branch is NULL lands (user_branch is
        # None for them too). No-op for the Owner; filters to nothing for an
        # unassigned account, so it can't read every branch's warehouse.
        stock_records = scope_queryset(
            stock_records, request.user, 'warehouse__branch'
        )

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
    # available_qty, not quantity_on_hand — see _inventory_list_context's
    # identical reasoning for Floor Stock.
    matching = [(s, stock_status(s.available_qty, s.reorder_level)) for s in stock_records]
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
        today = timezone.localdate()
        # Same expiry exclusion as _inventory_list_context's Floor Stock
        # batch_qs — a due lot stops appearing as its own row here too.
        batch_qs = Batch.objects.filter(
            location=Batch.Location.WAREHOUSE, quantity__gt=0
        ).filter(
            models.Q(expiry_date__isnull=True) | models.Q(expiry_date__gt=today)
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

    # "Request Stock to Floor" now opens as a modal on this page instead of
    # its own URL — see warehouse_stock_request. Staff/Manager already have a
    # target branch (user_branch); an Owner needs one picked first, via the
    # modal's own branch-picker step or the toolbar's branch filter, both of
    # which land here as ?branch=.
    request_target_branch = _branch_scoped_modal_target(request)
    request_warehouse_stock = None
    if request_target_branch:
        request_warehouse = warehouse if warehouse else Warehouse.objects.filter(
            branch=request_target_branch
        ).first()
        if request_warehouse:
            # available_qty (expired lots excluded), not raw quantity_on_hand
            # — this list is what staff may request onto the floor, and an
            # expired warehouse lot has nothing sellable behind it to send.
            request_warehouse_stock = [
                s for s in WarehouseStock.objects.filter(
                    warehouse=request_warehouse
                ).select_related('product').sellable()
                if s.available_qty > 0
            ]

    return {
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
        # Every other active filter, for the topbar branch switcher to carry
        # along — same idea as _daterange.html's daterange_carry.
        'branch_switch_carry': [('q', search_query), ('category', category_filter)],
        # Distinct products, not len(rows) — rows is now one-per-batch, so a
        # product with two lots would otherwise count itself twice.
        'total_products': len(matching),
        'can_edit_reorder': (
            request.user.role in [User.Role.OWNER, User.Role.BRANCH_MANAGER]
            and user_can_perform_module_action(request.user, WAREHOUSE_STOCK, EDIT)
        ),
        # Always available to any internal role — when there's no single
        # pinned branch (Owner), the modal shows a branch picker step first
        # rather than the button being hidden outright.
        'can_request_to_floor': user_can_perform_module_action(
            request.user, WAREHOUSE_STOCK, ADD
        ),
        'request_target_branch': request_target_branch,
        'request_warehouse_stock': request_warehouse_stock,
        'show_request_modal': request.GET.get('open_request') == '1',
    }


@login_required(login_url='accounts:login')
def warehouse_stock_list(request):
    if request.user.role not in [
        User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF
    ]:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    context = _warehouse_stock_list_context(request)
    if not isinstance(context, dict):
        # A Staff/Manager whose branch has no warehouse yet — the context
        # builder bails out with a redirect instead of a context dict.
        return context
    return render(request, 'inventory/warehouse_stock_list.html', context)


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
    if m.movement_type == mt.OPENING_BALANCE:
        return product_name, 'Opening balance', f'{branch_name} Floor'
    if m.movement_type == mt.OPENING_BALANCE_REVERSAL:
        return product_name, f'{branch_name} Floor', 'Opening balance reversal'
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

def _range_carry(active_range, range_start, range_end):
    """(name, value) pairs for the §2e range control, for the topbar branch
    switcher (partials/_branch_switch.html) to carry along — the reverse of
    _daterange.html's own daterange_carry, which carries branch INTO a range
    change. Same helper as reports.views._range_carry."""
    carry = [('range', active_range)]
    if active_range == 'day':
        carry.append(('date', range_end.strftime('%Y-%m-%d')))
    elif active_range == 'custom':
        carry += [
            ('date_from', range_start.strftime('%Y-%m-%d') if range_start else ''),
            ('date_to', range_end.strftime('%Y-%m-%d') if range_end else ''),
        ]
    return carry


def _transfer_list_context(request):
    transfers = StockTransfer.objects.select_related(
        'source_branch', 'destination_branch',
        'requested_by', 'warehouse'
    ).all()

    # Branch managers and staff only see their branch's transfers.
    # scope_queryset fails closed for an unassigned account; the old
    # `if user_branch:` form showed it every branch's withdrawal slips.
    # user_branch is still read below, only for the Owner's branch filter.
    user_branch = get_user_branch(request.user)
    transfers = scope_queryset(transfers, request.user, 'source_branch')

    # Filters from GET parameters
    search_query = request.GET.get('q', '')
    status_filter = request.GET.get('status', '')
    purpose_filter = request.GET.get('purpose', '')
    # The shared back-office range control (_daterange.html), same as the
    # Dashboard, Reports and the audit log — DESIGN.md's "All date filtering
    # should use the same date-range control pattern." Validates date_from/
    # date_to itself (via core.date_validation.parse_date_range) and stores
    # any errors on request.date_range_errors, which _daterange.html reads.
    range_start, range_end, active_range = range_from_request(request)
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

    # Summary stats for the top cards, computed BEFORE the range filter below
    # is applied to `transfers`. Released This Month is a FLOW already fixed
    # to its own window (the calendar month, via created_at); Pending
    # Acknowledgment and Flagged Discrepancies are BACKLOGS with no natural
    # window at all — see the kpi-row comment in transfer_list.html for why
    # neither may be scoped to the toolbar's date range. Half-open
    # aware-datetime bounds, NOT created_at__month=/__year= — `created_at` is
    # a DateTimeField, so __month= is compiled through CONVERT_TZ on MySQL
    # and silently matches zero rows here, pinning this tile to 0 forever.
    # See core.timebounds. localdate(), not now(): now() is UTC under
    # USE_TZ, so on the 1st or last day of a month it reports the wrong
    # month for Asia/Manila.
    today = timezone.localdate()
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

    # The range control narrows the Withdrawal Slips list only, applied here
    # so it never touches the backlog/flow KPIs above. transfer_date is a
    # plain DateField (no timezone conversion needed); range_start is None
    # for the 'all' preset.
    if range_start:
        transfers = transfers.filter(transfer_date__gte=range_start)
    transfers = transfers.filter(transfer_date__lte=range_end)

    # The Movement Log tab shares this page, so the same branch choice narrows
    # it too — [int(branch_filter)] when the Owner has picked one, otherwise
    # the pre-existing role-based scoping (None = every branch for the Owner).
    movement_branch_ids = [int(branch_filter)] if branch_filter else branch_ids_for_user(request.user)

    # "New Stock Adjustment" now opens as a modal on this page instead of
    # its own URL — see transfer_create. Staff/Manager already have a target
    # branch (user_branch); an Owner needs one picked first, via the modal's
    # own branch-picker step or the toolbar's branch filter, both of which
    # land here as ?branch=. Same pattern as Warehouse Stock's "Request
    # Stock to Floor" modal — see _branch_scoped_modal_target.
    create_target_branch = _branch_scoped_modal_target(request)
    create_floor_stock = None
    if create_target_branch:
        create_floor_stock = Inventory.objects.filter(
            branch=create_target_branch, quantity_on_hand__gt=0
        ).select_related('product')

    return {
        'transfers': transfers,
        'movement_log': _movement_log(movement_branch_ids),
        'search_query': search_query,
        'status_filter': status_filter,
        'purpose_filter': purpose_filter,
        # _daterange.html: a preset click must not drop the search/filters.
        'active_range': active_range,
        'range_start': range_start or range_end,
        'range_end': range_end,
        'daterange_carry': [
            ('q', search_query), ('status', status_filter), ('purpose', purpose_filter),
            ('branch', branch_filter),
        ],
        'branches': cached_branches() if not user_branch else [],
        'branch_filter': branch_filter,
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Warehouse Stock and Product Master List's filter_branch.
        'filter_branch': next((b for b in cached_branches() if str(b.id) == branch_filter), None),
        'user_branch': user_branch,
        # Every other active filter, for the topbar branch switcher to carry
        # along — same idea as _daterange.html's daterange_carry, in reverse.
        'branch_switch_carry': [
            ('q', search_query), ('status', status_filter), ('purpose', purpose_filter),
        ] + _range_carry(active_range, range_start, range_end),
        'status_choices': StockTransfer.Status.choices,
        'purpose_choices': StockTransfer.Purpose.choices,
        'total_released_this_month': total_released_this_month,
        'pending_acknowledgment': pending_acknowledgment,
        'flagged_discrepancies': flagged_discrepancies,
        # Names the window the card above counts. From `today`, which is
        # already localtime()-derived, so it cannot disagree with the
        # month_start/month_end bounds the count itself was filtered on.
        'month_label': today.strftime('%B'),
        'create_target_branch': create_target_branch,
        'create_floor_stock': create_floor_stock,
        # Write-offs only — Branch Restock has its own dedicated flow (the
        # Request Stock to Floor modal on Warehouse Stock).
        'write_off_purposes': [
            c for c in StockTransfer.Purpose.choices
            if c[0] != StockTransfer.Purpose.BRANCH_RESTOCK
        ],
        'show_create_modal': request.GET.get('open_create') == '1',
        'can_create': user_can_perform_module_action(
            request.user, WITHDRAWALS, ADD
        ),
    }


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def transfer_list(request):
    return render(request, 'inventory/transfer_list.html', _transfer_list_context(request))


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
    """POST handler for the "Request Stock to Floor" modal on the Warehouse
    Stock page — always a Branch Restock, no Purpose picker. Separate on
    purpose from New Stock Adjustment (transfer_create below), which now
    only handles floor-level write-offs, so the two actions can never be
    confused for each other.

    The modal itself (branch picker + item form) is rendered inline by
    warehouse_stock_list/_warehouse_stock_list_context — this view only ever
    handles the submit, redisplaying that same page with the modal reopened
    on a validation error so the picked branch and entered quantities aren't
    lost. See _branch_scoped_modal_target for how the target branch (pinned
    for Staff/Manager, picked by an Owner) is resolved.
    """
    if request.user.role not in [
        User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER
    ]:
        messages.error(request, 'You do not have permission to request stock.')
        return redirect('warehouse_stock_list')

    if request.method != 'POST':
        return redirect('warehouse_stock_list')

    target_branch = _branch_scoped_modal_target(request)
    if not target_branch:
        messages.error(request, 'Choose a branch first.')
        return redirect('warehouse_stock_list')

    try:
        warehouse = Warehouse.objects.get(branch=target_branch)
    except Warehouse.DoesNotExist:
        messages.error(request, f'No warehouse found for {target_branch.branch_name}.')
        return redirect('warehouse_stock_list')

    transfer_date = request.POST.get('transfer_date') or timezone.localdate()
    notes = request.POST.get('notes', '')
    product_ids = request.POST.getlist('product_id')
    quantities = request.POST.getlist('quantity_requested')

    valid_items = [
        (pid, _safe_positive_int(qty)) for pid, qty in zip(product_ids, quantities)
        if pid and _safe_positive_int(qty) is not None
    ]

    if not valid_items:
        messages.error(request, 'Please add at least one product.')
        context = _warehouse_stock_list_context(request)
        if not isinstance(context, dict):
            return context
        context['show_request_modal'] = True
        return render(request, 'inventory/warehouse_stock_list.html', context)

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


@login_required(login_url='accounts:login')
def transfer_create(request):
    """POST handler for the "New Stock Adjustment" modal on the Stock
    Adjustments page — floor-level write-offs only (damaged/spoiled,
    sample/display, other). Restocking the floor FROM the warehouse is its
    own dedicated flow (see warehouse_stock_request) — keeping the two entry
    points separate is the whole point: a write-off never touches the
    warehouse, and a restock request never touches the floor directly.

    The modal itself (branch picker + item form) is rendered inline by
    transfer_list/_transfer_list_context — this view only ever handles the
    submit, redisplaying that same page with the modal reopened on a
    validation error. See _branch_scoped_modal_target for how the target
    branch (pinned for Staff/Manager, picked by an Owner) is resolved.
    """
    if request.user.role not in [
        User.Role.STAFF, User.Role.BRANCH_MANAGER, User.Role.OWNER
    ]:
        messages.error(request, 'You do not have permission to create stock withdrawals.')
        return redirect('transfer_list')

    if request.method != 'POST':
        return redirect('transfer_list')

    target_branch = _branch_scoped_modal_target(request)
    if not target_branch:
        messages.error(request, 'Choose a branch first.')
        return redirect('transfer_list')

    # StockTransfer.warehouse is a required FK even for a floor write-off —
    # it's what identifies which branch this slip belongs to. The write-off
    # path below never reads or writes WarehouseStock through it.
    try:
        warehouse = Warehouse.objects.get(branch=target_branch)
    except Warehouse.DoesNotExist:
        messages.error(request, f'No warehouse found for {target_branch.branch_name}.')
        return redirect('transfer_list')

    write_off_purposes = [
        c for c in StockTransfer.Purpose.choices
        if c[0] != StockTransfer.Purpose.BRANCH_RESTOCK
    ]

    purpose = request.POST.get('purpose')
    if purpose not in dict(write_off_purposes):
        purpose = StockTransfer.Purpose.OTHER  # guards against a tampered POST
    notes = request.POST.get('notes', '')
    transfer_date = request.POST.get('transfer_date') or timezone.localdate()
    product_ids = request.POST.getlist('product_id')
    quantities = request.POST.getlist('quantity_requested')

    valid_items = [
        (pid, _safe_positive_int(qty)) for pid, qty in zip(product_ids, quantities)
        if pid and _safe_positive_int(qty) is not None
    ]

    if not valid_items:
        messages.error(request, 'Please add at least one product.')
        context = _transfer_list_context(request)
        context['show_create_modal'] = True
        return render(request, 'inventory/transfer_list.html', context)

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
                        batch_available = Batch.available_quantity(
                            item.product, transfer.source_branch,
                            Batch.Location.WAREHOUSE,
                        )
                        available = (
                            ws.quantity_on_hand if batch_available is None
                            else min(ws.quantity_on_hand, batch_available)
                        )
                        if qty_release > available:
                            raise _InsufficientStockError(
                                f'Not enough warehouse stock for {item.product.product_name} to '
                                f'release this transfer — available: {available}, '
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
                            include_expired=True,
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
