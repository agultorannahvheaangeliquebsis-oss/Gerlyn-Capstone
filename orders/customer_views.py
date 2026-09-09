from decimal import Decimal
from functools import wraps
from urllib.parse import quote

from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from branches.models import Branch
from inventory.models import Inventory, Product, ProductCategory
from inventory.stats import (
    attach_storefront_counts, in_stock_product_ids, units_sold_by_product,
)
from sales.models import SalesItem, SalesTransaction
from sales.pricing import price_cart
from sales.views import price_for

# Cap on how many rows the live-search dropdown shows at once.
SEARCH_SUGGESTION_LIMIT = 8

# Free delivery threshold shown on the cart meter.
FREE_DELIVERY_OVER = Decimal('500')

# Catalog sort orders. The dict is the single source of truth for both the
# <select> the template renders and the validation of ?sort= — an unknown or
# hand-edited value falls back to DEFAULT_SORT rather than 500ing on a
# missing key.
#
# "Popular" is the reference's "Featured" slot filled with a real figure —
# units actually sold — rather than a curation flag no one in this system can
# set. There is no featured/pinned column on Product, so a literal "Featured"
# order would just be an arbitrary one wearing an editorial label.
SORT_OPTIONS = {
    'popular': {
        'label': 'Popular',
        'key': lambda r: (-r['product'].units_sold, r['product'].product_name),
    },
    'price_asc': {
        'label': 'Price: Low to High',
        'key': lambda r: (r['product'].unit_price, r['product'].product_name),
    },
    'price_desc': {
        'label': 'Price: High to Low',
        'key': lambda r: (-r['product'].unit_price, r['product'].product_name),
    },
    'name': {
        'label': 'Name A–Z',
        'key': lambda r: r['product'].product_name.lower(),
    },
}
DEFAULT_SORT = 'popular'


def is_customer(user):
    return user.is_authenticated and user.role == 'CUSTOMER'


def customer_required(view):
    """Gate the cart and checkout on a CUSTOMER account, without looping.

    @customer_required sent BOTH kinds of refusal to the login
    page, and for a signed-in staff or owner account that is an infinite
    redirect: login_view honours ?next= for a user who is already
    authenticated, so it hands them straight back to the view that just
    refused them, which sends them to login again. The browser gives up with
    ERR_TOO_MANY_REDIRECTS; from the shopper's side, "Add to cart" simply
    does nothing. It is reproducible in three lines with the test client,
    which raises RedirectCycleError.

    The two refusals are not the same thing and no longer get the same
    answer. Someone with no session is asked to sign in, with their
    destination preserved so the button they pressed still happens
    afterwards. Someone signed in to a non-customer account is TOLD why —
    the storefront cart belongs to customer accounts; staff sell through the
    POS — and left on the catalog, which they can still read.

    The AJAX branch matters because add_to_cart has one: catalog-live.js
    expects JSON, and a 302 to an HTML page would surface as a silent
    failure rather than a message.
    """
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

        if not user.is_authenticated:
            if is_ajax:
                return JsonResponse(
                    {'ok': False, 'error': 'Please sign in to continue.'}, status=401
                )
            return redirect(f"{reverse('accounts:login')}?next={quote(request.get_full_path())}")

        if not is_customer(user):
            error = (
                'The shop cart is for customer accounts. '
                'Staff sales go through the POS instead.'
            )
            if is_ajax:
                return JsonResponse({'ok': False, 'error': error}, status=403)
            messages.error(request, error)
            return redirect('orders:catalog')

        return view(request, *args, **kwargs)

    return _wrapped


def _safe_next(request):
    """The POSTed `next`, but only if it points back into this site.

    `next` is attacker-controllable, and redirect() will happily send the
    browser to another origin, so it is validated here rather than trusted.
    Returns None when absent or off-site, and the caller falls back to its
    own default.
    """
    nxt = request.POST.get('next')
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return None


def order_type_for(user):
    """The price tier this shopper buys at online.

    Read from the account and never from a request parameter: a shopper who
    could name their own tier would be choosing their own prices. Wholesale is
    a standing property of the account that only the Owner or a Branch Manager
    sets (CustomerProfile.is_wholesale_eligible); everyone else — including a
    guest with no profile at all — is Retail.

    One function rather than the check written out at each call site, because
    the cart preview and the sale that gets written have to agree. When the
    preview priced itself off unit_price directly, a wholesale customer read
    retail figures on the cart page and then got wholesale totals at checkout.
    """
    profile = getattr(user, 'customer_profile', None)
    if profile and profile.is_wholesale_eligible:
        return SalesTransaction.OrderType.WHOLESALE
    return SalesTransaction.OrderType.RETAIL


def _cart_items(request):
    """Resolve the session cart into rows + total. Skips stale entries.

    Priced at the shopper's own tier — the same one checkout() will use — so
    the figures here are what they will actually be billed. The PWD/Senior
    discount is deliberately NOT previewed, and since F-04 it cannot be
    claimed here at all: it is granted by staff, against a sighted ID, after
    the order exists. Showing it on the cart would promise a price the order
    is not entitled to and may never get.
    """
    branch_id = request.session.get('branch_id')
    cart = request.session.get('cart', {})
    order_type = order_type_for(request.user)
    items, total = [], Decimal('0')

    for product_id, qty in cart.items():
        inv = Inventory.objects.sellable().select_related('product').filter(
            product_id=product_id, branch_id=branch_id
        ).first()
        if not inv:
            continue
        unit_price = price_for(inv.product, order_type)
        subtotal = unit_price * qty
        total += subtotal
        items.append({
            'product': inv.product,
            'quantity': qty,
            # The tier price, not product.unit_price — the template shows this
            # as the "each" figure, so it must not read the retail price back
            # off the product and undo the tier.
            'unit_price': unit_price,
            'subtotal': subtotal,
            'in_stock': inv.available_qty,
            'short': qty > inv.available_qty,
        })
    return items, total


# ─────────────────────────────────────────────
# BROWSING — open to guests (log in only to buy)
# ─────────────────────────────────────────────

def product_catalog(request):
    branches = Branch.objects.all()

    # Branch is two things at once, and the rail has to serve both.
    #
    # As a FILTER it is multi-select: tick several and the grid lists what
    # any of them has on the shelf. As the SHOPPING branch it stays single,
    # because the cart is per-branch — stock and prices differ between
    # branches, and a line item has to come from one of them.
    #
    # The two are reconciled by the rule that is true of both: an order needs
    # exactly one branch. Tick one and it becomes the shopping branch. Tick
    # none or several and there is no single answer, so the session branch is
    # dropped and the cards fall back to "Choose a branch" — which is the
    # browsing state the storefront already had, not a new one.
    #
    # Only when `branch` is actually in the query string. A shopper who chose
    # a branch and then clicked Shop in the nav arrives with no parameters at
    # all, and forgetting their branch on a plain navigation would be a bug,
    # not a filter.
    branch_id = request.session.get('branch_id')
    if 'branch' in request.GET:
        wanted = [b for b in request.GET.getlist('branch') if b.isdigit()]
        # Round-tripped through the database rather than trusted: an id that
        # names no branch would otherwise sit in the URL, tick nothing in the
        # rail, and quietly filter the grid down to zero rows.
        valid = list(branches.filter(pk__in=wanted).values_list('pk', flat=True))
        if len(valid) == 1 and valid[0] != branch_id:
            _start_shopping_at(request, branches.get(pk=valid[0]))
            branch_id = valid[0]
        elif len(valid) != 1 and branch_id is not None:
            request.session.pop('branch_id', None)
            messages.info(
                request,
                'Browsing more than one branch, so no branch is set for your cart. '
                'Choose a single branch when you are ready to order.'
            )
            branch_id = None
        branch_filters = [str(pk) for pk in valid]
    else:
        branch_filters = [str(branch_id)] if branch_id else []

    # sellable() annotates available_qty — the non-expired portion of each
    # row's stock — so a product with an expired lot at one branch and a
    # fresh one at another (or even a fresh AND an expired lot in the same
    # row) is never hidden entirely, just correctly sized.
    stock = Inventory.objects.sellable().select_related(
        'product', 'product__category'
    ).prefetch_related('product__images')
    if branch_filters:
        stock = stock.filter(branch_id__in=branch_filters)

    search_query = request.GET.get('q', '')
    # getlist, not get: the rail is a checklist now, so "Rice AND Canned
    # goods" is a question a shopper can ask. Non-numeric values are dropped
    # rather than passed to the ORM, which raises on a bad pk instead of
    # simply matching nothing.
    category_filters = [c for c in request.GET.getlist('category') if c.isdigit()]
    if search_query:
        stock = stock.filter(product__product_name__icontains=search_query)
    if category_filters:
        stock = stock.filter(product__category_id__in=category_filters)

    # Collapse to one row per product when no branch is chosen; keep the
    # best availability so stock signals stay honest.
    rows = {}
    for inv in stock:
        key = inv.product_id
        if key not in rows or inv.available_qty > rows[key]['qty']:
            rows[key] = {'product': inv.product, 'qty': inv.available_qty}

    # The storefront does not list what it cannot sell. Filtered AFTER the
    # collapse above, not before it: with no branch chosen a product can be
    # empty at one branch and stocked at another, and dropping the empty row
    # early would have hidden a product that is genuinely available.
    rows = {pid: row for pid, row in rows.items() if row['qty'] > 0}

    ordered_rows = list(rows.values())

    # The card shows a "N sold" line where the design reference shows a star
    # rating — there is no review model, and this is a real figure. One
    # aggregate query for the whole page, not one per card.
    sold = units_sold_by_product([r['product'].id for r in ordered_rows])
    for row in ordered_rows:
        row['product'].units_sold = sold.get(row['product'].id, 0)

    # Sorted in Python, not in the queryset: `rows` above already collapsed
    # multiple branch Inventory rows down to one entry per product, and
    # units_sold was only just attached, so there is no queryset left to
    # order by either of them. The list is one page of products, not a table
    # scan, so the cost is nil.
    sort = request.GET.get('sort', '')
    if sort not in SORT_OPTIONS:
        sort = DEFAULT_SORT
    ordered_rows.sort(key=SORT_OPTIONS[sort]['key'])

    context = {
        'rows': ordered_rows,
        'branches': branches,
        'selected_branch_id': branch_id,
        'categories': ProductCategory.objects.all(),
        'search_query': search_query,
        'category_filters': category_filters,
        'branch_filters': branch_filters,
        'sort': sort,
        # Everything except `sort`, pre-encoded, so the sort links can carry
        # the live filters without the template having to loop the category
        # list into a query string by hand — which it cannot do correctly now
        # that there can be several of them.
        'filter_qs': _filter_querystring(search_query, category_filters, branch_filters),
        # The unfiltered figure behind "N of M items". Counted from Product
        # rather than from the grid so it does not move when a category or a
        # branch is picked — "12 of 12" and "3 of 12" are the two readings
        # that make the pair worth printing at all.
        'total_count': Product.objects.count(),
        'sort_options': [
            {'value': value, 'label': option['label']}
            for value, option in SORT_OPTIONS.items()
        ],
        'cart_count': len(request.session.get('cart', {})),
    }

    # Live search (catalog-live.js) asks for just the two regions that
    # change — avoids re-fetching/re-rendering the whole page (branch tabs,
    # category list, chrome) on every keystroke.
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'summary_html': render_to_string('partials/catalog_result_summary.html', context, request=request),
            'grid_html': render_to_string('partials/catalog_grid.html', context, request=request),
        })

    return render(request, 'orders/catalog.html', context)


def search_suggestions(request):
    """JSON endpoint backing the header's live-search dropdown."""
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'results': []})

    # Suggestions lead straight to a product page, so they pass the same
    # in-stock gate as the listings — otherwise the one place a shopper can
    # still reach an unavailable product is the search box.
    products = (
        Product.objects.select_related('category')
        .filter(Q(product_name__icontains=query) | Q(sku_code__icontains=query))
        .filter(id__in=in_stock_product_ids(request.session.get('branch_id')))
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
    # Same treatment the grid cards get, so the detail page can show the
    # "In stock · N sold" line instead of making the shopper infer both from
    # the per-branch table below it. Not branch-scoped: this line describes
    # the product across the store, and the table underneath is what breaks
    # it down per branch.
    attach_storefront_counts([product])
    branches = Branch.objects.all()

    branch_stock = []
    for branch in branches:
        inv = Inventory.objects.sellable().filter(product=product, branch=branch).first()
        branch_stock.append({
            'branch': branch,
            'available': bool(inv and inv.available_qty > 0),
            'qty': inv.available_qty if inv else 0,
        })

    return render(request, 'orders/product_detail.html', {
        'product': product,
        'branch_stock': branch_stock,
        # attach_storefront_counts so the related cards can offer the same
        # Add-to-cart CTA as the catalog and home grids instead of a "View"
        # link back into another product page. Scoped to the chosen branch
        # when there is one, so the stock line matches what the cart will
        # actually accept.
        # Only what is actually buyable — a "You might also like" rail is a
        # recommendation, and recommending something out of stock wastes the
        # click. Same gate the catalog and home grids use.
        'related': attach_storefront_counts(
            list(
                Product.objects.filter(category=product.category)
                .exclude(id=product.id)
                .filter(id__in=in_stock_product_ids(request.session.get('branch_id')))
                .select_related('category')
                .prefetch_related('images')[:6]
            ),
            branch_id=request.session.get('branch_id'),
        ),
        'selected_branch_id': request.session.get('branch_id'),
        'cart_count': len(request.session.get('cart', {})),
    })


def _filter_querystring(search_query, category_filters, branch_filters):
    """`q` and every ticked category and branch, urlencoded, without `sort`."""
    params = QueryDict(mutable=True)
    if search_query:
        params['q'] = search_query
    if category_filters:
        params.setlist('category', category_filters)
    if branch_filters:
        params.setlist('branch', branch_filters)
    return params.urlencode()


def _start_shopping_at(request, branch):
    """
    Point the session at a branch and start a fresh cart, saying so.

    Switching branches empties the cart — stock and prices are per-branch, so
    the old lines cannot be carried across. All three entry points below used
    to do that in silence, which from the shopper's side is indistinguishable
    from the cart having lost itself. The wording only mentions the clearing
    when there was actually something to clear.
    """
    had_items = bool(request.session.get('cart'))
    request.session['branch_id'] = branch.pk
    request.session['cart'] = {}
    if had_items:
        messages.info(
            request,
            f'Now shopping at {branch.branch_name}. Your cart was cleared — '
            f'stock and prices are set per branch.'
        )
    else:
        messages.success(request, f'Now shopping at {branch.branch_name}.')


def select_branch(request):
    branches = Branch.objects.all()
    if request.method == 'POST':
        # request.POST['branch_id'] with a bare int() was a KeyError or a
        # ValueError away from a 500 on a submit with nothing chosen — which
        # is a rejection the shopper should be told about, not a crash. The
        # int() has to happen before the query too: a non-numeric pk raises
        # out of the ORM rather than simply matching nothing.
        try:
            branch_id = int(request.POST.get('branch_id', ''))
        except (TypeError, ValueError):
            branch_id = None
        branch = Branch.objects.filter(pk=branch_id).first() if branch_id else None
        if branch is None:
            messages.error(request, 'Choose a branch to continue.')
            return redirect('orders:select_branch')
        _start_shopping_at(request, branch)
        return redirect('orders:catalog')
    return render(request, 'orders/select_branch.html', {'branches': branches})


def set_branch(request, branch_id):
    _start_shopping_at(request, get_object_or_404(Branch, pk=branch_id))
    return redirect('orders:catalog')


def clear_branch(request):
    # Leaves the cart alone, as it always has — only the silence is fixed here.
    request.session.pop('branch_id', None)
    messages.success(request, 'Branch cleared. Pick one to start shopping again.')
    return redirect('orders:catalog')


# ─────────────────────────────────────────────
# CART + CHECKOUT — customers only
# ─────────────────────────────────────────────

@customer_required
def add_to_cart(request, product_id):
    branch_id = request.session.get('branch_id')
    if not branch_id:
        messages.info(request, 'Choose a branch first so we can check stock.')
        return redirect('orders:select_branch')

    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    back = _safe_next(request)

    # The quantity is raw POST input. Two failure modes, handled here before
    # it can reach the stock comparison below:
    #   - non-numeric ("abc") used to raise ValueError out of int() and 500.
    #     Fall back to 1, the same default the field carries when omitted.
    #   - a negative (or zero) value parsed fine but then sailed through the
    #     `already + qty > available` guard trivially and *shrank* the cart
    #     line instead of erroring. Reject it at the cast site.
    try:
        qty = int(request.POST.get('quantity', 1) or 1)
    except (TypeError, ValueError):
        qty = 1
    if qty < 1:
        error = 'Enter a quantity of at least 1.'
        if is_ajax:
            return JsonResponse({'ok': False, 'error': error}, status=409)
        messages.error(request, error)
        return redirect(back or 'orders:catalog')

    # Not get_object_or_404: the home page's discover grid is not
    # branch-scoped, so it can legitimately offer a product the chosen branch
    # has never stocked (no Inventory row at all, which is different from a
    # row reading zero). That is a normal shopping outcome, not a broken URL —
    # a 404 page would also break the AJAX path, which expects JSON back.
    inv = Inventory.objects.sellable().filter(
        product_id=product_id, branch_id=branch_id
    ).select_related('product', 'branch').first()
    if inv is None:
        product = get_object_or_404(Product, pk=product_id)
        error = f'{product.product_name} is not carried at your selected branch.'
        if is_ajax:
            return JsonResponse({'ok': False, 'error': error}, status=409)
        messages.error(request, error)
        return redirect(back or 'orders:catalog')

    cart = request.session.get('cart', {})
    already = cart.get(str(product_id), 0)

    if already + qty > inv.available_qty:
        if inv.available_qty == 0:
            error = f'{inv.product.product_name} is out of stock at your selected branch.'
        else:
            error = f'Only {inv.available_qty} of {inv.product.product_name} left at this branch.'
        if is_ajax:
            return JsonResponse({'ok': False, 'error': error}, status=409)
        messages.error(request, error)
        return redirect(back or 'orders:catalog')

    cart[str(product_id)] = already + qty
    request.session['cart'] = cart
    # Remember exactly what we just added so the toast's "Undo" can reverse it.
    request.session['last_added'] = {'id': str(product_id), 'qty': qty}
    success = f'Added {inv.product.product_name} to your cart.'
    if is_ajax:
        # No redirect — the storefront JS confirms the add on the button
        # itself, so the customer keeps their scroll position instead of
        # jumping to the top of a re-rendered page.
        #
        # No undo_url/cart_url either: those fed the success toast's two
        # buttons, and there is no success toast any more. The Undo path
        # still exists for the message rendered on the no-JavaScript POST
        # below, which carries its own URL from the template.
        return JsonResponse({
            'ok': True,
            'message': success,
            'cart_count': len(cart),
        })
    messages.success(request, success, extra_tags='undoable')
    return redirect(back or 'orders:catalog')


@customer_required
def undo_add(request):
    """Reverse the most recent Add to Cart — backs the toast's Undo button.

    Called by fetch() from toast.js, so the happy path answers in JSON and the
    toast dismisses itself with no reload. Falls back to a plain redirect (with
    a confirmation message) when JavaScript is off or the fetch fails.
    """
    if request.method != 'POST':
        return redirect('orders:catalog')

    last = request.session.pop('last_added', None)
    cart = request.session.get('cart', {})
    if last and last['id'] in cart:
        cart[last['id']] -= last['qty']
        if cart[last['id']] <= 0:
            cart.pop(last['id'])
        request.session['cart'] = cart

    cart_count = len(cart)
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'cart_count': cart_count})

    messages.info(request, 'Removed from your cart.')
    return redirect(request.META.get('HTTP_REFERER') or 'orders:catalog')


@customer_required
def update_cart(request, product_id):
    """Bump one line up or down. Floors at 1 and never exceeds branch stock."""
    if request.method != 'POST':
        return redirect('orders:cart')

    branch_id = request.session.get('branch_id')
    cart = request.session.get('cart', {})
    key = str(product_id)
    if key not in cart:
        messages.info(request, 'That item is no longer in your cart.')
        return redirect('orders:cart')

    inv = Inventory.objects.sellable().select_related('product').filter(
        product_id=product_id, branch_id=branch_id
    ).first()
    if not inv:
        # This one silently DELETED a line and redirected to a cart that had
        # quietly changed under the shopper — the worst of the three silent
        # exits, because they had asked for the opposite (one more of it).
        cart.pop(key, None)
        request.session['cart'] = cart
        messages.error(
            request,
            'That item is no longer stocked at this branch, so it came out of your cart.'
        )
        return redirect('orders:cart')

    action = request.POST.get('action')
    if action == 'increase':
        if cart[key] >= inv.available_qty:
            messages.error(
                request,
                f'Only {inv.available_qty} of {inv.product.product_name} left at this branch.'
            )
        else:
            cart[key] += 1
    elif action == 'decrease':
        # Minimum of 1 — use Remove to clear the line entirely.
        cart[key] = max(cart[key] - 1, 1)

    request.session['cart'] = cart
    return redirect('orders:cart')


@customer_required
def remove_from_cart(request, product_id):
    """Drop a line from the cart completely."""
    if request.method != 'POST':
        return redirect('orders:cart')

    cart = request.session.get('cart', {})
    if cart.pop(str(product_id), None) is not None:
        request.session['cart'] = cart
        messages.info(request, 'Removed from your cart.')
    return redirect('orders:cart')


@customer_required
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
        'cart_count': len(request.session.get('cart', {})),
    })


@customer_required
def checkout(request):
    branch_id = request.session.get('branch_id')
    cart = request.session.get('cart', {})

    if not branch_id or not cart:
        messages.error(request, 'Your cart is empty.')
        return redirect('orders:catalog')

    if request.method != 'POST':
        return redirect('orders:cart')

    # --- price tier ---------------------------------------------------------
    # Same derivation the cart preview used, from the same function, so the
    # figures the shopper just read cannot disagree with what they are billed.
    # Never read from the POST — there is no order_type input on this form and
    # there must not be one.
    order_type = order_type_for(request.user)

    # --- payment method --------------------------------------------------------
    # Validated against the model's choices, the same fall-back-to-default
    # shape order_type uses at the counter: an unrecognised value — a
    # tampered form, a client bug — becomes the online default (Cash on
    # Delivery) rather than being written to the column and the billing
    # record verbatim, which broke the Billing report's payment breakdown.
    #
    # Falling back is right here and wrong for fulfillment_type just below;
    # the difference is that every payment method on this form is recorded
    # only, and settled in person either way, so the default cannot mislead
    # anyone about what happens next.
    mode_of_payment = (
        request.POST.get('mode_of_payment') or SalesTransaction.ModeOfPayment.COD
    )
    if mode_of_payment not in SalesTransaction.ModeOfPayment.values:
        mode_of_payment = SalesTransaction.ModeOfPayment.COD

    # --- delivery vs pickup -------------------------------------------------
    # F-11 — delivery_address used to be written straight from the POST with
    # no validation at all, and the form invited a blank one ("leave blank to
    # pick up"). A delivery order whose address field was lost — a client bug,
    # a mis-tap, a stripped form — was therefore indistinguishable from a
    # genuine pickup: same blank column, no way for the Order Console to tell
    # that anyone was ever meant to deliver it.
    #
    # So the choice is now explicit and required, and the address is validated
    # against it. Unlike mode_of_payment and order_type above, an unrecognised
    # value must NOT fall back to a default: silently picking one would either
    # promise a delivery nobody asked for or strand a customer waiting at home
    # for an order marked pickup. There is no safe guess, so this refuses.
    fulfillment_type = request.POST.get('fulfillment_type') or ''
    if fulfillment_type not in SalesTransaction.FulfillmentType.values:
        messages.error(
            request, 'Choose whether this order is for delivery or for pickup.'
        )
        return redirect('orders:cart')

    delivery_address = request.POST.get('delivery_address', '').strip()
    if (fulfillment_type == SalesTransaction.FulfillmentType.DELIVERY
            and not delivery_address):
        messages.error(
            request,
            'Enter a delivery address, or choose Pickup instead — we cannot '
            'deliver an order without one.'
        )
        return redirect('orders:cart')
    if fulfillment_type == SalesTransaction.FulfillmentType.PICKUP:
        # Drop anything typed into the address box before switching to
        # Pickup. A stray address on a pickup order reads as a delivery
        # everywhere downstream — the console, the receipt, the customer's
        # own tracking page all key off this field being non-blank.
        delivery_address = ''

    # --- PWD / Senior Citizen discount --------------------------------------
    # Deliberately NOT read from this form, and there is no UI offering it.
    #
    # F-04 — checkout used to take discount_type straight from the POST and
    # apply the full statutory 20% + VAT exemption on it. The only guard was
    # that the ID-number and holder-name fields be non-empty strings, so
    # 'MADE-UP-123' bought a permanent 20% discount; no staff member ever saw
    # a card. The discount is conditioned on presenting a PWD or OSCA ID, and
    # nothing typed into a web form is presenting anything.
    #
    # So every order this view writes is undiscounted, whatever the request
    # contains. Where a discount is genuinely owed, staff apply it afterwards
    # from the Order Console (sales.views.transaction_apply_discount), which
    # records who verified the ID — and which refuses outright on a Delivery
    # order, because nobody at the branch ever meets that customer.
    #
    # This matches the API write path next door, which has always passed
    # discount_type=None for the same reason (SalesTransactionWriteSerializer).

    with transaction.atomic():
        # Validate availability WITHOUT deducting: an online order only leaves
        # the shelf when staff mark it Completed in the Order Console.
        planned = []
        for product_id, qty in cart.items():
            # Not get_object_or_404: no Inventory row at all for this
            # product/branch must fall through to the same friendly
            # "not enough left" message as a genuine shortfall, not a 404
            # page mid-checkout.
            inv = Inventory.objects.select_for_update().sellable().select_related(
                'product'
            ).filter(product_id=product_id, branch_id=branch_id).first()
            # available_qty already nets out anything expired since this
            # was added to the cart — it may be less than the row's raw
            # quantity_on_hand, which still counts expired stock.
            available = inv.available_qty if inv else 0
            if available < qty:
                product = inv.product if inv else Product.objects.filter(pk=product_id).first()
                product_name = product.product_name if product else 'This item'
                messages.error(
                    request,
                    f'Sorry — only {available} of {product_name} left.'
                )
                transaction.set_rollback(True)
                return redirect('orders:cart')
            planned.append((inv.product, qty))

        # Same pricing engine as the counter. This used to be a raw
        # unit_price x qty sum, which meant an online order recorded no VAT
        # breakdown at all (total_vat_amount stayed 0 on every one of them)
        # and could not honour a wholesale tier. price_cart owns the VAT
        # arithmetic; the only decision made out here is the price TIER.
        #
        # No discount_type is passed — not None-by-omission but None on
        # purpose: customer checkout never discounts (F-04).
        priced = price_cart(
            [(product, qty, price_for(product, order_type)) for product, qty in planned],
            discount_type=None,
        )
        total = priced['total_amount']

        sale = SalesTransaction.objects.create(
            branch_id=branch_id,
            staff=None,
            customer=request.user,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            order_status=SalesTransaction.OrderStatus.PENDING,
            mode_of_payment=mode_of_payment,
            fulfillment_type=fulfillment_type,
            delivery_address=delivery_address,
            order_type=order_type,
        )

        for line in priced['lines']:
            SalesItem.objects.create(
                transaction=sale, product=line['product'],
                quantity_sold=line['quantity'],
                # Already-discounted unit price — SalesItem.save multiplies it
                # out, so the discount lives in the price and must not be
                # taken off the subtotal a second time.
                unit_price=line['unit_price'],
                is_vat_exempt_line=line['is_vat_exempt_line'],
                vat_amount=line['vat_amount'],
                discount_amount=line['discount_amount'],
            )

        sale.total_amount = total
        sale.total_vat_amount = priced['total_vat_amount']
        sale.total_discount_amount = priced['total_discount_amount']
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


@customer_required
def my_orders(request):
    orders = (
        SalesTransaction.objects.filter(customer=request.user)
        .select_related('branch')
        .prefetch_related('billing_record')
        .order_by('-transaction_date')
    )

    # Which lifecycle statuses each tab stands for. One table, read twice —
    # once to count and once to filter — so a tab can never show a number it
    # then fails to list.
    #
    # 'active' used to mean everything that was neither Completed nor
    # Cancelled, which included Preparing. Now that Preparing has a tab of its
    # own ("To receive" — the order is packed and waiting to be handed over),
    # leaving it in both would have made the two counts overlap and the totals
    # across the bar exceed All. In progress is therefore the part of the
    # lifecycle the STORE is still working through, and To receive the part
    # the CUSTOMER is waiting on.
    tab_statuses = {
        'active': ['Pending', 'Approved'],
        'to_receive': ['Preparing'],
        'completed': ['Completed'],
        'cancelled': ['Cancelled'],
    }

    status_filter = request.GET.get('status', '')
    if status_filter not in tab_statuses:
        status_filter = ''          # an unknown ?status= falls back to All

    counts = {'all': orders.count()}
    counts.update({
        tab: orders.filter(order_status__in=statuses).count()
        for tab, statuses in tab_statuses.items()
    })

    if status_filter:
        orders = orders.filter(order_status__in=tab_statuses[status_filter])

    return render(request, 'orders/my_orders.html', {
        'orders': orders,
        'counts': counts,
        'status_filter': status_filter,
        'active_page': 'orders',
        'cart_count': len(request.session.get('cart', {})),
    })


@customer_required
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
        'cart_count': len(request.session.get('cart', {})),
    })


@customer_required
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
