import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models as dj_models
from django.db import transaction
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from billing.models import BillingRecord
from billing.views import payment_methods_for_channel
from branches.models import Branch
from core.people import person_display_name
from core.permissions import BRANCH_MANAGER, INTERNAL_ROLES, OWNER, STAFF, role_required
from core.scoping import branch_for_user, branch_ids_for_user, scope_queryset, user_can_access_branch
from inventory.alerts import stock_status
from inventory.models import Batch, Inventory, Product, StockMovement

from . import refunds as refund_service
from .models import (
    OrderStatusChange, OrderStatusHistory, Refund, RefundItem, SalesItem,
    SalesItemBatchConsumption, SalesTransaction,
)
from .pricing import price_cart, PWD_SENIOR_DISCOUNT_RATE, VAT_RATE
from .pwd_requests import (
    DISCOUNT_BLOCKED_BILLING_STATUSES, DiscountError, apply_discount,
    approve_pwd_request, reject_pwd_request,
)


def _pos_discount_choices():
    """The two real discount claims — "None" is the absence of a claim, and
    rendering it as a third radio invites picking it as if it were one.
    Shared by _pos_context and transaction_detail so the two can't drift."""
    return [
        c for c in SalesTransaction.DiscountType.choices
        if c[0] != SalesTransaction.DiscountType.NONE
    ]


# ─────────────────────────────────────────────
# ORDER CONSOLE
# ─────────────────────────────────────────────

# Returned by _pos_user_branch for a Staff/Branch Manager account whose branch
# profile is missing (half-provisioned). It is deliberately NOT None: None means
# "unrestricted" — the Owner, who chooses a branch on the form — and letting an
# unassigned Staff/Manager collapse to that same None handed them the Owner's
# every-branch selector and skipped the posted-branch access check.
POS_BRANCH_UNASSIGNED = object()

# The only payment methods the Walk-in Sale form actually offers (see
# _pos_context.pos_payment_choices below). Shared with transaction_add's
# validation so the server never accepts an online-only method (Cash on
# Delivery, Pay at Store) that a counter sale has no form control for.
POS_PAYMENT_METHODS = ['Cash', 'GCash', 'Card']


def _pos_user_branch(request):
    """The branch a Walk-in Sale is pinned to for this user.

    Shared by transaction_add's GET (rendering the Walk-in Sale form) and its
    POST handler, so both resolve the branch the same way.

    Returns:
        Branch                 -- a provisioned Staff / Branch Manager
        POS_BRANCH_UNASSIGNED  -- a Staff / Branch Manager with no branch
                                  profile; they cannot record a sale
        None                   -- the Owner, unrestricted, picks a branch
    """
    if request.user.role == STAFF:
        branch = getattr(getattr(request.user, 'staff_profile', None), 'branch', None)
        return branch or POS_BRANCH_UNASSIGNED
    if request.user.role == BRANCH_MANAGER:
        branch = getattr(getattr(request.user, 'manager_profile', None), 'branch', None)
        return branch or POS_BRANCH_UNASSIGNED
    return None


def _pos_owner_branch(request):
    """The branch an Owner has chosen for this Walk-in Sale, if any.

    Read from the just-submitted form (a rejected POST redisplay) first,
    then the ?branch= query string (the branch picker reloading the page
    once a branch is chosen) — same two sources _branch_scoped_modal_target
    reads, and for the same reason: a validation-error redisplay must
    recover the branch without a query string.

    Meaningless for a Staff/Branch Manager, who is already pinned via
    _pos_user_branch — callers only need this for the Owner.
    """
    branch_id = request.POST.get('branch') or request.GET.get('branch')
    if branch_id and str(branch_id).isdigit():
        return Branch.objects.filter(pk=branch_id).first()
    return None


def _pos_stock_map(user_branch):
    """product_id -> branch_id -> {qty, status}, for every branch the
    cashier could possibly ring this sale under: just their own pinned
    branch for Staff/Branch Manager, or every branch for the Owner (who
    picks one via #pos-branch). Keyed by both ids as strings since that's
    how they round-trip through json_script/JS.

    Sourced from Inventory (floor stock) — Walk-in Sale sells off the
    shelf, never the warehouse — and status is inventory.alerts.stock_status(),
    the same tiering the Floor Stock list and Alerts Center already use, so
    the indicator can't drift out of sync with what those screens call
    "low stock".

    `qty` is the SELLABLE quantity (Inventory.sellable() -> available_qty),
    the same figure _deduct_stock enforces, not the raw quantity_on_hand.
    Expired stock stays in quantity_on_hand until the daily sweep writes it
    off, so counting it here let the cart accept a quantity the server would
    then reject with "Not enough stock".
    """
    qs = Inventory.objects.sellable()
    if user_branch:
        qs = qs.filter(branch_id=user_branch.id)
    stock_map = {}
    for inv in qs.values('product_id', 'branch_id', 'available_qty', 'reorder_level'):
        stock_map.setdefault(str(inv['product_id']), {})[str(inv['branch_id'])] = {
            'qty': inv['available_qty'],
            'status': stock_status(inv['available_qty'], inv['reorder_level']),
        }
    return stock_map


def _pos_products_for(user_branch):
    """The products offered on the Walk-in Sale row dropdown.

    P-2 — for a Staff/Branch Manager (a fixed, known branch) this is scoped
    to products that branch actually carries an Inventory row for, so a
    cashier can no longer ring up something the branch has never stocked —
    previously the dropdown listed every product in the system, the stock
    line simply stayed hidden for one with no Inventory row at all (reading
    as "nothing to worry about" rather than "not carried here"), and the
    sale only failed on submit. This also incidentally shrinks the page for
    the common case (P-4) — one branch's assortment instead of the whole
    catalogue embedded in every row's <select>.

    For the Owner, who has not chosen a branch yet at render time (#pos-branch
    is a client-side control), the list stays unfiltered — there is no single
    branch to filter to. posUpdateStockLine in the page's own JS covers that
    case instead, showing "Not carried at this branch" for any product with
    no entry in pos_stock_map for whichever branch is currently selected.
    """
    products = Product.objects.select_related('category')
    if user_branch:
        products = products.filter(inventory_records__branch_id=user_branch.id).distinct()
    return products


def _pos_context(request):
    """Everything the Walk-in Sale page needs. Prefixed pos_* to distinguish
    it from unrelated fields elsewhere (e.g. the Order Console's own
    `branches` for its filter toolbar — a different list answering a
    different question)."""
    user_branch = _pos_user_branch(request)
    # An Owner has no pinned branch, so the product list has nowhere to
    # scope to until they pick one on the form — previously this fell back
    # to _pos_products_for(None), embedding every product in the system
    # (unit price, tiers, eligibility flags — one <option> apiece) in every
    # item row, on a store whose catalog can run past a thousand SKUs. The
    # branch picker below now gates the whole Items card until one is
    # chosen, at which point this is exactly as scoped as a pinned
    # Staff/Manager's list always was.
    owner_branch = None if user_branch else _pos_owner_branch(request)
    effective_branch = user_branch or owner_branch
    # Tiers travel to the page as JSON per product so the row JS can show the
    # price the cashier is about to charge as the quantity is typed. It is a
    # preview only — transaction_add re-resolves every line server-side.
    # P-2 — scoped through _pos_products_for first, same as the branch-carried
    # filter below: a cashier should not even see a product their branch has
    # never stocked, tier preview or not.
    pos_products = list(
        # images — the menu grid shows each product's primary photo
        # (partials/product_thumb.html reads product.images.all).
        _pos_products_for(effective_branch).prefetch_related('price_tiers', 'images')
    ) if effective_branch else []
    for product in pos_products:
        product.tiers_json = json.dumps([
            [tier.min_quantity, float(tier.unit_price)]
            for tier in product.price_tiers.all()
        ])
    # The menu's category tabs — only categories this branch actually has
    # a product in, so a tab can never open onto an empty grid.
    pos_categories = sorted(
        {p.category for p in pos_products if p.category_id},
        key=lambda c: c.category_name.lower(),
    )
    return {
        'pos_products': pos_products,
        'pos_categories': pos_categories,
        'pos_has_uncategorized': any(not p.category_id for p in pos_products),
        'pos_branches': Branch.objects.all() if not user_branch else [],
        'pos_user_branch': user_branch,
        # The Owner's choice specifically — None until the branch picker
        # step is completed. Distinct from pos_effective_branch so the
        # template can tell "no branch chosen yet" from "pinned branch".
        'pos_owner_branch': owner_branch,
        'pos_effective_branch': effective_branch,
        'pos_stock_map': _pos_stock_map(effective_branch),
        'pos_payment_choices': [
            c for c in SalesTransaction.ModeOfPayment.choices
            if c[0] in POS_PAYMENT_METHODS
        ],
        'pos_discount_choices': _pos_discount_choices(),
        'pos_discount_rate_pct': int(PWD_SENIOR_DISCOUNT_RATE * 100),
        'pos_vat_rate_pct': int(VAT_RATE * 100),
        # "Replaces" dropdown — recent cancelled orders only, since line
        # items can't be edited and this is how a correction gets linked
        # back to the order it's standing in for. Pinned staff/manager see
        # only their own branch's; the Owner picks a branch on the form
        # itself, so this stays unfiltered for them and transaction_add's
        # own branch check on submit is what actually enforces the match.
        'pos_cancelled_orders': _pos_cancelled_orders(user_branch),
    }


def _pos_cancelled_orders(user_branch):
    orders = SalesTransaction.objects.filter(order_status=SalesTransaction.OrderStatus.CANCELLED)
    if user_branch:
        orders = orders.filter(branch=user_branch)
    return orders.order_by('-transaction_date')[:20]


def _transaction_list_context(request):
    transactions = (
        SalesTransaction.objects
        # billing_record is a OneToOneField (one query, joined) — not a
        # prefetch_related, which is the one-to-many/many-to-many technique
        # and issues a second query for it that a plain select_related join
        # would have folded into the first.
        .select_related('branch', 'staff', 'recorded_by', 'customer', 'billing_record')
        .order_by('-transaction_date')
    )
    # Server-side branch scoping (never trust a UI dropdown alone).
    transactions = scope_queryset(transactions, request.user)

    search_query = request.GET.get('q', '')
    status_filter = request.GET.get('status', '')
    channel_filter = request.GET.get('channel', '')
    branch_filter = request.GET.get('branch', '')
    fulfillment_filter = request.GET.get('fulfillment', '')

    if search_query:
        transactions = transactions.filter(
            dj_models.Q(order_number__icontains=search_query) |
            dj_models.Q(customer__first_name__icontains=search_query) |
            dj_models.Q(customer__last_name__icontains=search_query) |
            dj_models.Q(customer__username__icontains=search_query)
        )
    if channel_filter:
        transactions = transactions.filter(transaction_type=channel_filter)
    if branch_filter:
        transactions = transactions.filter(branch_id=branch_filter)
    if fulfillment_filter:
        transactions = transactions.filter(fulfillment_type=fulfillment_filter)

    # Counts per tab are computed BEFORE the status filter is applied.
    base = transactions
    counts = {
        'all': base.count(),
        'pending': base.filter(order_status=SalesTransaction.OrderStatus.PENDING).count(),
        'approved': base.filter(order_status=SalesTransaction.OrderStatus.APPROVED).count(),
        'preparing': base.filter(order_status=SalesTransaction.OrderStatus.PREPARING).count(),
        'completed': base.filter(order_status=SalesTransaction.OrderStatus.COMPLETED).count(),
        'cancelled': base.filter(order_status=SalesTransaction.OrderStatus.CANCELLED).count(),
    }
    if status_filter:
        transactions = transactions.filter(order_status=status_filter)

    show_branch_filter = branch_ids_for_user(request.user) is None
    can_pos = request.user.role in [OWNER, BRANCH_MANAGER, STAFF]
    branches = Branch.objects.all() if show_branch_filter else []

    over_committed_ids, over_committed_count = _over_committed_order_ids(request.user)

    context = {
        'transactions': transactions,
        'counts': counts,
        'search_query': search_query,
        'status_filter': status_filter,
        'channel_filter': channel_filter,
        'branch_filter': branch_filter,
        'fulfillment_filter': fulfillment_filter,
        'branches': branches,
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Warehouse Stock, Product Master List, Stock Adjustments and
        # Receive Delivery.
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        'user_branch': branch_for_user(request.user),
        'can_pos': can_pos,
        'over_committed_ids': over_committed_ids,
        'over_committed_count': over_committed_count,
        # Every active filter except `status` — the status tabs below build
        # their href off this (O-3), so adding a new filter to the toolbar
        # (fulfillment, here) can't quietly fall out of the tab links. Same
        # pattern as orders.customer_views' _filter_querystring for the
        # storefront catalog rail. `type` (Retail/Wholesale) is gone along
        # with SalesTransaction.order_type — a quantity tier is a per-line
        # fact now, not a per-sale one, so there is no single type left for a
        # sale to be filtered by.
        'filter_qs': _order_filter_querystring(
            search_query, channel_filter, branch_filter, fulfillment_filter
        ),
        # Every other active filter, for the topbar branch switcher
        # (partials/_branch_switch.html) to carry along — same idea as
        # _daterange.html's daterange_carry.
        'branch_switch_carry': [
            ('q', search_query), ('status', status_filter),
            ('channel', channel_filter), ('fulfillment', fulfillment_filter),
        ],
    }
    return context


# S-11 — over-committed orders. An open order (Pending/Approved/Preparing)
# is "over-committed" when, added to every other open order for the same
# branch/product, it asks for more than the branch's current floor stock —
# i.e. the branch has promised more of that item than it can actually pull.
# Reporting only, per the audit's own scope call for this finding: nothing
# here reserves or holds stock, so a completed transfer or a cancelled order
# elsewhere can clear the flag before Preparing without any code path having
# to release a hold.
_OPEN_ORDER_STATUSES = [
    SalesTransaction.OrderStatus.PENDING,
    SalesTransaction.OrderStatus.APPROVED,
    SalesTransaction.OrderStatus.PREPARING,
]


def _over_committed_order_ids(user):
    open_orders = scope_queryset(
        SalesTransaction.objects.filter(order_status__in=_OPEN_ORDER_STATUSES),
        user,
    )

    committed = (
        SalesItem.objects
        .filter(transaction__in=open_orders)
        .values('transaction__branch_id', 'product_id')
        .annotate(committed_qty=dj_models.Sum('quantity_sold'))
    )
    if not committed:
        return set(), 0

    available = {
        (row['branch_id'], row['product_id']): row['available']
        for row in (
            Inventory.objects.sellable()
            .values('branch_id', 'product_id')
            .annotate(available=dj_models.Sum('available_qty'))
        )
    }

    over_committed_keys = {
        (row['transaction__branch_id'], row['product_id'])
        for row in committed
        if row['committed_qty'] > available.get((row['transaction__branch_id'], row['product_id']), 0)
    }
    if not over_committed_keys:
        return set(), 0

    key_filter = dj_models.Q()
    for branch_id, product_id in over_committed_keys:
        key_filter |= dj_models.Q(transaction__branch_id=branch_id, product_id=product_id)

    order_ids = set(
        SalesItem.objects
        .filter(transaction__in=open_orders)
        .filter(key_filter)
        .values_list('transaction_id', flat=True)
    )
    return order_ids, len(order_ids)


def _order_filter_querystring(search_query, channel_filter, branch_filter, fulfillment_filter):
    """`q`, `channel`, `branch` and `fulfillment`, urlencoded, without
    `status` — the Order Console's status tabs append their own `status=`."""
    params = QueryDict(mutable=True)
    if search_query:
        params['q'] = search_query
    if channel_filter:
        params['channel'] = channel_filter
    if branch_filter:
        params['branch'] = branch_filter
    if fulfillment_filter:
        params['fulfillment'] = fulfillment_filter
    return params.urlencode()


@login_required(login_url='accounts:login')
def transaction_list(request):
    """Order Console — the queue of every order, filterable by status."""
    if request.user.role not in INTERNAL_ROLES:
        messages.error(request, 'Access denied.')
        return redirect('accounts:home')

    return render(
        request, 'sales/transaction_list.html', _transaction_list_context(request)
    )


@login_required(login_url='accounts:login')
def transaction_detail(request, pk):
    # O-7 — this is the Order Console's own detail page, not a general
    # "view my order" page: a Customer or Supplier following a stale link
    # here has no branch at all, so the branch check below always refused
    # them with "That order belongs to another branch" (untrue — they have
    # none) and then bounced them into transaction_list, which refused them
    # a SECOND time with "Access denied." One accurate message, and sent
    # to somewhere that is actually theirs — a Customer has their own
    # My Orders / Track Order pages for exactly this.
    if request.user.role not in INTERNAL_ROLES:
        messages.error(request, "That page is part of the internal Order Console — it isn't available to your account.")
        return redirect('accounts:home')

    txn = get_object_or_404(
        SalesTransaction.objects.select_related('branch', 'staff', 'recorded_by', 'customer'), pk=pk
    )
    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    items = list(txn.items.select_related('product'))
    billing = getattr(txn, 'billing_record', None)
    next_actions = _next_actions(txn, request.user)
    confirmations = _status_confirmations(txn, billing, items, next_actions)

    return render(request, 'sales/transaction_detail.html', {
        'transaction': txn,
        'items': items,
        'billing': billing,
        # Set by the billing record's "View Order" link. The billing record is
        # already open behind that overlay, so the order shows its items in
        # place of the payment summary.
        'from_billing': request.GET.get('from') == 'billing',
        'next_actions': next_actions,
        # The same actions, each paired with what its confirmation dialog
        # shows (None for the routine steps that don't stop for one).
        'next_action_items': [
            {'status': a, 'confirm': confirmations.get(a)} for a in next_actions
        ],
        # Every step the order has taken, with its reason, as one trail.
        'status_timeline': _status_timeline(txn),
        # The Mark Completed button is only live once the payment is Paid
        # (transaction_set_status enforces it; this just keeps the UI honest).
        'payment_is_paid': bool(billing and billing.status == BillingRecord.PaymentStatus.PAID),
        # The Record Payment modal. Offered while nothing is verified yet — no
        # billing record, or a Pending one — and never from the billing record's
        # own overlay, which already has its own payment controls.
        # billing_create re-checks all of it (status, branch, channel, evidence)
        # on POST; the lists below only decide which fields the modal draws.
        'can_record_payment': (
            request.GET.get('from') != 'billing'
            and (billing is None or billing.status == BillingRecord.PaymentStatus.PENDING)
        ),
        'payment_choices': [
            (m.value, m.label) for m in payment_methods_for_channel(txn.transaction_type)
        ],
        'reference_required_methods': sorted(BillingRecord.REFERENCE_REQUIRED_METHODS),
        'proof_required_methods': sorted(BillingRecord.PROOF_REQUIRED_METHODS),
        'proof_max_mb': settings.PROOF_OF_PAYMENT_MAX_MB,
        'progress': _order_progress(txn),
        # Whether to offer the after-the-fact PWD/Senior discount form.
        # transaction_apply_discount re-checks every one of these conditions
        # itself - this only decides what to draw, and asks the same model
        # method the view enforces so the two cannot drift apart.
        'can_apply_discount': _can_offer_discount(txn, billing),
        'discount_choices': _pos_discount_choices(),
        # Refunds (P14). Asked here so the form is only drawn when
        # sales.refunds would accept it — request_refund re-checks against
        # locked rows, so a forged POST is refused server-side regardless.
        'can_request_refund': refund_service.can_request_refund(txn, billing, request.user),
        # The customer's own PWD request, awaiting a look at their ID.
        # transaction_pwd_request_review re-checks this against locked rows.
        'can_review_pwd_request': _can_review_pwd_request(txn, billing),
        'refundable_items': (
            refund_service.refundable_lines(txn)
            if refund_service.can_request_refund(txn, billing, request.user) else []
        ),
        'can_decide_refunds': refund_service.can_decide_refund(request.user, txn),
        'refund_method_choices': Refund.RefundMethod.choices,
        'refund_condition_choices': RefundItem.Condition.choices,
        'refund_reason_choices': Refund.ReasonCode.choices,
        'refunds': txn.refunds.select_related('requested_by', 'refunded_by').prefetch_related(
            'items__sales_item__product', 'events__actor'
        ),
    })


@login_required(login_url='accounts:login')
def transaction_receipt(request, pk):
    """The printable sales receipt for a completed order.

    Read-only and server-rendered, so the browser's own print-to-PDF does
    the output — the same approach po_print.html and the Reports pages take,
    and it keeps a PDF library out of the dependency list. Reached from the
    "Print Receipt" button on transaction_detail as its own page, with its
    own back link and a Print button that calls window.print(). The template
    still extends base_template (rather than internal_base.html by name) so
    a ?modal=1 fetch still returns the bare document, for parity with the
    rest of the system's detail pages — nothing in the UI currently opens it
    that way.

    The store identity on the letterhead — logo, TIN, the VAT line, the
    header and footer text, the closing thank-you line — is the
    Owner-controlled StoreSetting row, the same one the printed purchase
    order reads, exposed on every page by
    core.context_processors.store_settings.
    """
    # O-7 — same accurate refusal as transaction_detail above, for the same
    # reason: this is an internal document, not something a Customer or
    # Supplier following a stale link should be told belongs to "another
    # branch" when they have none at all.
    if request.user.role not in INTERNAL_ROLES:
        messages.error(request, "That page is part of the internal Order Console — it isn't available to your account.")
        return redirect('accounts:home')

    txn = get_object_or_404(
        SalesTransaction.objects.select_related('branch', 'staff', 'recorded_by', 'customer'), pk=pk
    )
    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    items = txn.items.select_related('product').all()

    # total_amount is the stored, authoritative figure and stays so on the
    # document. The line sum is computed alongside it only so the template
    # can flag a mismatch rather than print a total that doesn't add up.
    line_sum = sum((i.subtotal for i in items), Decimal('0'))

    # Gross before the statutory discount. unit_price on a discounted line
    # was already reduced by sales.pricing, so the line subtotals (and
    # total_amount) are net; adding the recorded discount back gives the
    # figure to print on the "Subtotal" line above "Less: discount".
    gross_subtotal = txn.total_amount + (txn.total_discount_amount or Decimal('0'))

    return render(request, 'sales/transaction_receipt.html', {
        'transaction': txn,
        'items': items,
        'billing': getattr(txn, 'billing_record', None),
        'line_sum': line_sum,
        'totals_disagree': line_sum != txn.total_amount,
        'gross_subtotal': gross_subtotal,
        'served_by_name': person_display_name(txn.recorded_by),
        'printed_on': timezone.localdate(),
        'printed_by': person_display_name(request.user),
    })


# ─────────────────────────────────────────────
# ORDER LIFECYCLE
# Pending → Approved → Preparing → Completed, or → Cancelled.
# Stock is deducted when an ONLINE order is COMPLETED (walk-in POS deducts at
# the point of sale instead), so stock never leaves twice.
# ─────────────────────────────────────────────

_FLOW = {
    SalesTransaction.OrderStatus.PENDING: ['Approved', 'Cancelled'],
    SalesTransaction.OrderStatus.APPROVED: ['Preparing', 'Cancelled'],
    SalesTransaction.OrderStatus.PREPARING: ['Completed', 'Cancelled'],
    # Completed -> Cancelled is the void path: unlike cancelling from any
    # earlier stage (stock was never touched), this has to reverse the stock
    # _deduct_stock already applied. Gated to Owner/Branch Manager only
    # inside transaction_set_status — Staff can cancel an unfulfilled order
    # but not undo a completed sale.
    SalesTransaction.OrderStatus.COMPLETED: ['Cancelled'],
    SalesTransaction.OrderStatus.CANCELLED: [],
}


def _next_actions(txn, user=None):
    actions = _FLOW.get(txn.order_status, [])
    if (txn.transaction_type == SalesTransaction.TransactionType.PHYSICAL
            and txn.order_status == SalesTransaction.OrderStatus.PENDING):
        # A walk-in that is still Pending is one whose payment hasn't cleared
        # yet (GCash/Card — see transaction_add). The goods already left the
        # counter, so Approved / Preparing mean nothing here: the only real
        # steps are "payment verified, complete it" or "it fell through".
        actions = ['Completed', 'Cancelled']
    if (txn.order_status == SalesTransaction.OrderStatus.COMPLETED
            and user is not None and user.role not in [OWNER, BRANCH_MANAGER]):
        # Staff can cancel an unfulfilled order but not void a completed
        # one — don't offer a button transaction_set_status would reject.
        actions = [a for a in actions if a != 'Cancelled']
    return actions


# The same four-stage lifecycle orders.customer_views.STATUS_STEPS draws on
# the customer-facing Track Order page — read here only to feed the equivalent
# "Order progress" widget on the staff-side Order Detail page. Purely a
# display aid: it never feeds _next_actions or transaction_set_status, so it
# cannot drift into gating anything.
ORDER_PROGRESS_STEPS = ['Pending', 'Approved', 'Preparing', 'Completed']


def _status_timeline(txn):
    """One chronological list of every step an order has taken, for Order Detail.

    Two tables record the same moves: OrderStatusHistory (the full trail,
    including creation, but with a note only for a cancellation) and
    OrderStatusChange (written beside it by transaction_set_status, and the
    only place the optional Completed note is kept). Each step here is the
    history row with the change's reason folded in. A change with no history
    row — an order that predates OrderStatusHistory — stands on its own.
    """
    unmatched = list(txn.status_changes.select_related('changed_by'))
    timeline = []
    for h in txn.status_history.select_related('actor'):
        note = h.note
        for change in unmatched:
            if change.from_status == h.from_status and change.to_status == h.to_status:
                unmatched.remove(change)
                note = note or change.reason
                break
        timeline.append({
            'when': h.created_at, 'from_status': h.from_status,
            'to_status': h.to_status, 'by': h.actor, 'note': note,
        })
    for change in unmatched:
        timeline.append({
            'when': change.changed_at, 'from_status': change.from_status,
            'to_status': change.to_status, 'by': change.changed_by, 'note': change.reason,
        })
    timeline.sort(key=lambda step: step['when'])
    return timeline


def _order_progress(txn):
    cancelled = txn.order_status == SalesTransaction.OrderStatus.CANCELLED
    current = 0 if cancelled else ORDER_PROGRESS_STEPS.index(txn.order_status)
    return {
        'cancelled': cancelled,
        'steps': [
            {
                'name': name,
                'done': (not cancelled) and i < current,
                'current': (not cancelled) and i == current,
            }
            for i, name in enumerate(ORDER_PROGRESS_STEPS)
        ],
    }


# The steps that stop for the confirmation dialog on Order Detail. Approved and
# Preparing are the routine forward path a cashier walks all day and stay one
# click; these two either move stock and start the refund window (Completed)
# or close the order for good and reverse what it did (Cancelled).
HIGH_IMPACT_STATUSES = (
    SalesTransaction.OrderStatus.COMPLETED,
    SalesTransaction.OrderStatus.CANCELLED,
)

# The optional note (and Cancelled's required reason) is stored on
# OrderStatusChange.reason and, for Cancelled, SalesTransaction.cancel_reason —
# both 255. Checked here rather than left to the database, which would raise
# instead of truncating.
STATUS_REASON_MAX_LENGTH = OrderStatusChange._meta.get_field('reason').max_length


def _status_confirmations(txn, billing, items, actions):
    """What the confirmation dialog shows for each high-impact step in
    `actions`: the order summary, the impact notice, and whether a reason is
    required.

    Text lives here, next to _FLOW and transaction_set_status, so the notice
    describing what a step does is written beside the code that does it. It is
    display only — transaction_set_status re-derives and enforces every rule
    itself, so a stale or forged dialog changes nothing.
    """
    Status = SalesTransaction.OrderStatus
    branch = txn.branch.branch_name
    is_online = txn.transaction_type == SalesTransaction.TransactionType.ONLINE
    was_completed = txn.order_status == Status.COMPLETED
    paid = bool(billing and billing.status == BillingRecord.PaymentStatus.PAID)

    summary = {
        'order_number': txn.order_number,
        'branch': branch,
        'channel': 'Online' if is_online else 'In-Store',
        'fulfillment': txn.get_fulfillment_type_display() if is_online else '',
        'customer': (
            (txn.customer.get_full_name() or txn.customer.username)
            if txn.customer_id else 'Walk-in'
        ),
        'lines': len(items),
        'units': sum(i.quantity_sold for i in items),
        'total': txn.total_amount,
        'payment': billing.status if billing else 'No billing record',
        'current': txn.order_status,
    }

    result = {}
    for target in actions:
        if target not in HIGH_IMPACT_STATUSES:
            continue
        notes = []

        if target == Status.CANCELLED:
            if was_completed:
                notes.append(f"This voids a completed sale — the stock it took from {branch} is put back.")
            elif txn.transaction_type == SalesTransaction.TransactionType.PHYSICAL:
                notes.append(f"This walk-in sale already took its stock from {branch} — it is put back.")
            else:
                notes.append("No stock has been deducted for this order yet, so there is nothing to put back.")
            if billing and billing.can_transition_to(BillingRecord.PaymentStatus.CANCELLED):
                notes.append(
                    f"Its payment record ({billing.status}) is marked Cancelled. Nothing is refunded "
                    f"through the system — any money already collected is handled outside it."
                )
            if txn.customer_id:
                notes.append("The customer is notified.")
            notes.append("A cancelled order cannot be moved to another status.")
            config = {
                'title': 'Void this completed sale?' if was_completed else 'Cancel this order?',
                'ok': 'Void sale' if was_completed else 'Cancel order',
                'dismiss': 'Keep order',
                'variant': 'danger',
                'reason_required': True,
                'reason_label': 'Reason for cancelling',
                'reason_help': 'Required. Kept on the order as its cancellation record.',
            }
        else:
            if not paid:
                notes.append("Payment must be verified as Paid before this order can be completed.")
            if is_online:
                notes.append(f"Stock for this order is deducted from {branch}.")
            else:
                notes.append("Stock was already deducted when this walk-in sale was rung up — nothing more is deducted.")
            notes.append(f"Starts the {txn.REFUND_WINDOW.days}-day refund window.")
            if txn.customer_id:
                notes.append("The customer is notified.")
            notes.append("Only an Owner or Branch Manager can undo a completed order, by voiding it.")
            config = {
                'title': 'Mark this order as Completed?',
                'ok': 'Mark completed',
                'dismiss': 'Not yet',
                'variant': 'warn',
                'reason_required': False,
                'reason_label': 'Note',
                'reason_help': 'Optional. Recorded in the order\'s status history.',
            }

        result[target] = {
            **config,
            'target': target,
            'summary': summary,
            'notes': notes,
            'reason_max': STATUS_REASON_MAX_LENGTH,
        }
    return result


class _OrderActionError(Exception):
    """Raised inside an atomic block (invalid transition, or _deduct_stock
    hitting insufficient stock) so the whole block rolls back — including
    any earlier line items already deducted in the same loop — instead of
    partially applying a rejected action."""


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def transaction_set_status(request, pk, new_status):
    """Advance an order through its lifecycle."""
    txn = get_object_or_404(SalesTransaction, pk=pk)

    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    if request.method != 'POST':
        return redirect('transaction_detail', pk=pk)

    try:
        with transaction.atomic():
            # Re-fetch and lock so a second, concurrent POST advancing the
            # same order (double-click, retry) can't also pass this guard
            # against the same stale, pre-lock read — and can't both reach
            # _deduct_stock for the same order.
            locked_txn = SalesTransaction.objects.select_for_update().get(pk=pk)

            # The confirmation dialog sends the status the order was in when
            # the summary was drawn. If someone else moved it since, what the
            # person just confirmed is no longer what would happen — refuse
            # and let them re-read the order. Optional: a request without it
            # (no JavaScript, the routine one-click steps) is judged on the
            # transition check below alone.
            expected_status = request.POST.get('expected_status', '').strip()
            if expected_status and expected_status != locked_txn.order_status:
                raise _OrderActionError(
                    f'{locked_txn.order_number} is now {locked_txn.order_status}, not '
                    f'{expected_status} — it changed while you were reviewing it. '
                    f'Check the order and try again.'
                )

            # Without the user, _next_actions falls back to its role-blind
            # default set and would let a Staff request through the "void a
            # completed sale" transition here — the check a few lines below
            # (Owner/Branch Manager only) happens to catch that today, but
            # the two checks can drift apart with nobody noticing, since this
            # one — meant to be the authoritative source of "what may this
            # role do next" — was never actually being asked the role.
            if new_status not in _next_actions(locked_txn, request.user):
                raise _OrderActionError(
                    f'Cannot move {locked_txn.order_number} from '
                    f'{locked_txn.order_status} to {new_status}.'
                )

            # Line items are immutable once an order exists — correcting a
            # mistake means cancelling this one and ringing up a new order,
            # never editing this one's items. A reason is what makes that
            # cancellation an actual record of why, not just a status flip;
            # required here, not left to convention, since a blank reason on
            # a voided or unfulfilled order is exactly the gap an audit asks
            # about later.
            #
            # `reason` is what the confirmation dialog posts; `cancel_reason`
            # is the field name the inline input used before it, still read so
            # an older form or client keeps working. Only Cancelled requires
            # one — for the other steps it is an optional note.
            reason = (
                request.POST.get('reason', '') or request.POST.get('cancel_reason', '')
            ).strip()
            if new_status == SalesTransaction.OrderStatus.CANCELLED and not reason:
                raise _OrderActionError('Enter a reason for cancelling this order.')
            if len(reason) > STATUS_REASON_MAX_LENGTH:
                raise _OrderActionError(
                    f'Keep the reason to {STATUS_REASON_MAX_LENGTH} characters or fewer '
                    f'(it is {len(reason)}).'
                )

            # An order is only Completed once its payment is verified Paid.
            # The billing row is read fresh and locked, same as the
            # cancellation path below — billing_confirm locks it too, so a
            # payment being rejected in the gap can't slip a completion through.
            if new_status == SalesTransaction.OrderStatus.COMPLETED:
                paying = BillingRecord.objects.select_for_update().filter(
                    transaction=locked_txn
                ).first()
                if not paying or paying.status != BillingRecord.PaymentStatus.PAID:
                    raise _OrderActionError(
                        f'{locked_txn.order_number} can only be completed once its '
                        f'payment is verified as Paid — record and verify the payment first.'
                    )

            was_completed = locked_txn.order_status == SalesTransaction.OrderStatus.COMPLETED
            # Lines whose product was hard-deleted from the catalog since the
            # sale — _reverse_stock skips them (nothing to credit) rather than
            # crashing; collected here so the person voiding/cancelling is
            # told, instead of the order just quietly restocking short.
            skipped_products = []

            if (not was_completed
                    and new_status == SalesTransaction.OrderStatus.CANCELLED
                    and locked_txn.transaction_type == SalesTransaction.TransactionType.PHYSICAL):
                # A walk-in's stock leaves the shelf when it is rung up, even
                # while its payment is still pending — so cancelling it before
                # completion has to put that stock back, unlike an online
                # order, which hasn't deducted anything until it completes.
                skipped_products += _reverse_stock(locked_txn, request.user)

            if was_completed and new_status == SalesTransaction.OrderStatus.CANCELLED:
                # Voiding a completed sale — undo what _deduct_stock already
                # applied. Distinct from cancelling an earlier-stage order,
                # which never touched stock in the first place. Reversing a
                # completed sale is a materially bigger action (real stock,
                # possibly real money, already moved), so it's restricted
                # below to Owner/Branch Manager even though Staff can cancel
                # unfulfilled orders.
                if request.user.role not in [OWNER, BRANCH_MANAGER]:
                    raise _OrderActionError(
                        'Only the Owner or a Branch Manager can void a completed sale.'
                    )
                # P14 — a refunded order cannot also be voided. _reverse_stock
                # credits back the FULL quantity sold, so voiding after a
                # Sellable refund would restock those units a second time;
                # and the money side (refunded_amount, Partially Refunded)
                # would have nothing coherent to reverse to. Refund whatever
                # is left instead. Requests still waiting for a decision are
                # closed here — the goods are being reversed wholesale, so
                # there is nothing left for anyone to approve.
                if refund_service.has_approved_refunds(locked_txn):
                    raise _OrderActionError(
                        f'{locked_txn.order_number} has already been refunded, so it cannot '
                        f'be voided — refund the remaining items instead.'
                    )
                refund_service.reject_pending_refunds(
                    locked_txn, request.user, 'Order was voided before this refund was decided.'
                )
                skipped_products += _reverse_stock(locked_txn, request.user)

            # Deduct stock once, when an online order is completed.
            if (new_status == SalesTransaction.OrderStatus.COMPLETED
                    and locked_txn.transaction_type == SalesTransaction.TransactionType.ONLINE):
                _deduct_stock(locked_txn, request.user)

            previous_status = locked_txn.order_status
            locked_txn.order_status = new_status
            OrderStatusHistory.objects.create(
                transaction=locked_txn, from_status=previous_status, to_status=new_status,
                actor=request.user,
                note=reason if new_status == SalesTransaction.OrderStatus.CANCELLED else '',
            )
            if new_status == SalesTransaction.OrderStatus.CANCELLED:
                locked_txn.cancel_reason = reason
            if new_status == SalesTransaction.OrderStatus.COMPLETED:
                # The refund window (SalesTransaction.refund_window_expired)
                # counts from here, not from when the order was placed.
                locked_txn.completed_at = timezone.now()
            locked_txn.save()

            OrderStatusChange.objects.create(
                transaction=locked_txn, from_status=previous_status,
                to_status=new_status, reason=reason, changed_by=request.user,
            )

            if (
                new_status == SalesTransaction.OrderStatus.COMPLETED
                and locked_txn.transaction_type == SalesTransaction.TransactionType.ONLINE
                and locked_txn.fulfillment_type == SalesTransaction.FulfillmentType.DELIVERY
            ):
                from .notifications import notify_order_delivered

                notify_order_delivered(locked_txn, request.user)

            # UIUX-001 — the customer-facing counterpart to the internal
            # notify() call below. Fires for every transition this view can
            # reach (Approved/Preparing/Completed/Cancelled), not just
            # Cancelled, since a customer cares about all of them. Reuses
            # orders.customer_views' own STATUS_BLURB text so this
            # notification says the same thing Track Order already tells
            # the customer for the same status (deferred import — mirrors
            # the notify() import a few lines down, and avoids a
            # module-load-time import between the sales and orders apps).
            #
            # Skipped for the exact Completed+Online+Delivery combination
            # above: notify_order_delivered already sends that customer a
            # more specific, delivery-framed notice for this same event.
            # Do NOT remove this guard to "restore" coverage here — doing so
            # would double-notify the customer for one transition instead of
            # closing a gap.
            if not (
                new_status == SalesTransaction.OrderStatus.COMPLETED
                and locked_txn.transaction_type == SalesTransaction.TransactionType.ONLINE
                and locked_txn.fulfillment_type == SalesTransaction.FulfillmentType.DELIVERY
            ):
                from orders.customer_views import STATUS_BLURB

                from core.notifications import notify_customer

                notify_customer(
                    locked_txn.customer,
                    f'Order {locked_txn.order_number}: {new_status}',
                    STATUS_BLURB.get(new_status, f'Your order is now {new_status}.'),
                    action_url=reverse(
                        'orders:track_order', kwargs={'transaction_id': locked_txn.pk}
                    ),
                    action_label='Track Order',
                )

            if new_status == SalesTransaction.OrderStatus.CANCELLED:
                # Keep billing in step — this is the one field
                # transaction_set_status doesn't already own, so it used to
                # sit at Pending indefinitely after a cancellation. A voided
                # completed sale had real money involved (Paid or Pending);
                # any actual refund happens outside the system (no payment
                # gateway integration — see billing.PaymentMethod.COD) and
                # this only reflects that in the record.
                #
                # F-10: record the void on its own voided_by / date_voided
                # pair. Do NOT touch confirmed_by / date_confirmed — writing
                # the voider into confirmed_by while date_confirmed kept the
                # original timestamp made the record read "confirmed by
                # [voider] on [original date]", a pairing that never happened,
                # and erased the real confirmer.
                # A fresh, LOCKED fetch — not the possibly-stale
                # locked_txn.billing_record relation. billing_confirm takes
                # out its own select_for_update on this same row (D-1), so
                # locking it here too is what actually serialises the two
                # against each other: without it, a payment confirmed in the
                # gap between this order-status check and this save() was
                # silently overwritten back to Cancelled, or the reverse —
                # either way the order and its bill could disagree about
                # whether there is still money to collect.
                billing = BillingRecord.objects.select_for_update().filter(
                    transaction=locked_txn
                ).first()
                # B-1 — route through the same transition map billing_confirm
                # and its API twin enforce, instead of writing the status
                # straight onto the field. ALLOWED_STATUS_TRANSITIONS now
                # names CANCELLED as reachable from every state precisely so
                # this call is not bypassing the rule the map documents —
                # this is the one caller allowed to actually take that route.
                if billing and billing.can_transition_to(BillingRecord.PaymentStatus.CANCELLED):
                    billing.status = BillingRecord.PaymentStatus.CANCELLED
                    billing.voided_by = request.user
                    billing.date_voided = timezone.now()
                    billing.save()

                from core.module_permissions import SALES_POS
                from core.notifications import CATEGORY_SALES, PRIORITY_MEDIUM, notify

                notify(
                    CATEGORY_SALES, PRIORITY_MEDIUM,
                    f'Order {locked_txn.order_number} cancelled',
                    f'{locked_txn.order_number} ({locked_txn.branch.branch_name}, '
                    f'₱{locked_txn.total_amount}) was cancelled by '
                    f'{request.user.username}.',
                    branch=locked_txn.branch,
                    action_url=reverse('transaction_detail', kwargs={'pk': locked_txn.pk}),
                    action_label='View Order',
                    required_module=SALES_POS,
                )
    except _OrderActionError as e:
        messages.error(request, str(e))
        return redirect('transaction_detail', pk=pk)

    messages.success(request, f'{txn.order_number} is now {new_status}.')
    if skipped_products:
        total_units = sum(i.quantity_sold for i in skipped_products)
        messages.warning(
            request,
            f'{len(skipped_products)} line item(s) ({total_units} unit(s) total) could '
            f'not have their stock restored — the product has since been removed from '
            f'the catalog.'
        )
    return redirect('transaction_detail', pk=pk)


# Once money has actually changed hands (Paid, or refunded from having been
# Paid) a NEW discount claim has nothing left to apply against — the price
# was already settled at the counter. The set lives in sales.pwd_requests so
# the pre-lock check here, the re-check against the locked row there, and
# what the Order Console draws all read one definition.
_DISCOUNT_BLOCKED_BILLING_STATUSES = DISCOUNT_BLOCKED_BILLING_STATUSES


def _can_offer_discount(txn, billing):
    """Whether the Order Console should show the apply-discount form.

    Kept beside the view that enforces the same conditions so the two are
    read together. `billing` is passed in rather than looked up again — the
    detail page already has it.

    Not while the customer's own PWD request is Pending: that has its own
    review panel, and two forms that both grant the discount would invite
    applying around the request.
    """
    return (
        txn.can_apply_pwd_senior_discount()
        and txn.discount_type == SalesTransaction.DiscountType.NONE
        and txn.order_status != SalesTransaction.OrderStatus.CANCELLED
        and not txn.has_pending_pwd_request
        and not (billing and billing.status in _DISCOUNT_BLOCKED_BILLING_STATUSES)
    )


def _can_review_pwd_request(txn, billing):
    """Whether the Order Console should show the approve / reject panel for
    the customer's PWD request. transaction_pwd_request_review re-checks all
    of it against locked rows; this only decides what to draw."""
    return (
        txn.has_pending_pwd_request
        and txn.order_status != SalesTransaction.OrderStatus.CANCELLED
        and not (billing and billing.status in _DISCOUNT_BLOCKED_BILLING_STATUSES)
    )


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def transaction_apply_discount(request, pk):
    """Apply the statutory PWD / Senior Citizen discount to an existing order.

    F-04 — this used to be claimable at customer checkout by typing an ID
    number into a web form, with no verification of any kind: 'MADE-UP-123'
    bought a permanent 20% reduction. The discount is conditioned on
    presenting a PWD or OSCA card, so the only place it can honestly be
    granted is where a staff member is looking at one. That is here.

    Two cases qualify, and `can_apply_pwd_senior_discount` is the single
    definition of them:

      * a walk-in (Physical) sale — the cashier saw the card at the counter
        but did not record the claim on the sale, and
      * an Online order collected in store (Pickup), at the moment the
        customer arrives.

    A Delivery order never qualifies: nobody at the branch meets that
    customer, so there is no verification moment to record.

    The re-pricing goes back through sales.pricing.price_cart — VAT stripped
    first, then 20% off the exclusive base, per RA 10754 / RA 9994. The
    arithmetic is not reimplemented here; that engine is what the POS, the
    storefront and the printed receipt all already agree on.
    """
    txn = get_object_or_404(SalesTransaction, pk=pk)

    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    if request.method != 'POST':
        return redirect('transaction_detail', pk=pk)

    # The gate, before anything about what is being claimed: whether this
    # user may act on this order at all is logically prior to what they are
    # asking for (the ordering billing_confirm uses for A-1 / C-6).
    if not txn.can_apply_pwd_senior_discount():
        messages.error(
            request,
            f'{txn.order_number} is for delivery — a PWD / Senior Citizen '
            f'discount can only be applied in person, at the counter for a '
            f'walk-in or when a pickup order is collected.'
        )
        return redirect('transaction_detail', pk=pk)

    if txn.order_status == SalesTransaction.OrderStatus.CANCELLED:
        messages.error(
            request,
            f'{txn.order_number} is cancelled — there is nothing to discount.'
        )
        return redirect('transaction_detail', pk=pk)

    if txn.discount_type != SalesTransaction.DiscountType.NONE:
        # Re-pricing already-discounted lines would take 20% off the
        # discounted price, compounding it. One claim per sale, as the
        # column comment on the model says.
        messages.error(
            request,
            f'{txn.order_number} already carries a '
            f'{txn.get_discount_type_display()} discount.'
        )
        return redirect('transaction_detail', pk=pk)

    billing = getattr(txn, 'billing_record', None)
    if billing and billing.status in _DISCOUNT_BLOCKED_BILLING_STATUSES:
        messages.error(
            request,
            f'{txn.order_number} is already paid — the amount due cannot be '
            f'changed. Reverse the payment first if this discount is owed.'
        )
        return redirect('transaction_detail', pk=pk)

    # NONE is not offerable: "apply no discount" is not an action, and
    # accepting it would write an audit trail for a no-op.
    discount_type = request.POST.get('discount_type') or ''
    if discount_type not in (SalesTransaction.DiscountType.PWD,
                             SalesTransaction.DiscountType.SENIOR):
        messages.error(request, 'Choose PWD or Senior Citizen.')
        return redirect('transaction_detail', pk=pk)

    # The ID details are still required. They are no longer the *only*
    # control — a person now vouches for having seen the card, and is named
    # in discount_applied_by for it — but they remain the record that makes
    # the discount defensible if it is ever questioned.
    id_number = request.POST.get('discount_id_number', '').strip()
    holder_name = request.POST.get('discount_holder_name', '').strip()
    errors = []
    if not id_number:
        errors.append(
            f'Enter the {discount_type} ID number — the discount is '
            f'conditioned on one being presented.'
        )
    if not holder_name:
        errors.append(f'Enter the name on the {discount_type} ID.')
    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect('transaction_detail', pk=pk)

    # The lock-and-recheck, the re-pricing (VAT stripped first, then 20% off
    # the exclusive base — RA 10754 / RA 9994) and the audit trail all live in
    # sales.pwd_requests.apply_discount, shared with the customer-request
    # approval below so the two grant a discount by the exact same code.
    try:
        priced = apply_discount(
            pk, request.user, discount_type, id_number, holder_name
        )
    except DiscountError as e:
        messages.error(request, str(e))
        return redirect('transaction_detail', pk=pk)

    if priced['total_discount_amount'] == Decimal('0'):
        # Recorded, but nothing moved: PWD and Senior are separate statutes
        # with separate exclusion lists, and no item on this order carries
        # the matching eligibility flag. Say so rather than reporting a
        # success the customer will not see on the total.
        messages.warning(
            request,
            f'{discount_type} recorded on {txn.order_number}, but no item on '
            f'it is eligible for that discount — the total is unchanged.'
        )
    else:
        messages.success(
            request,
            f'{discount_type} discount applied to {txn.order_number} — '
            f'₱{priced["total_discount_amount"]} off, now '
            f'₱{priced["total_amount"]}.'
        )
    return redirect('transaction_detail', pk=pk)


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def transaction_pwd_request_review(request, pk):
    """Approve or reject a customer's PWD discount request at pick-up.

    The customer asked for the discount at checkout (orders.customer_views.
    checkout) but nothing was granted — the order is still at full price. This
    is the verification step: a staff member looks at the card as the
    customer collects the order, then approves (the order is re-priced and the
    bill follows) or rejects with a reason (nothing changes but the record).
    Billing will not mark the order Paid while the request is Pending, so this
    always happens before payment is confirmed.

    The domain rules — and the API twin, SalesPwdRequestReviewAPIView — live in
    sales.pwd_requests. This view only maps the form onto them.
    """
    txn = get_object_or_404(SalesTransaction, pk=pk)

    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    if request.method != 'POST':
        return redirect('transaction_detail', pk=pk)

    decision = request.POST.get('decision', '')
    try:
        if decision == 'approve':
            priced = approve_pwd_request(
                pk, request.user,
                request.POST.get('discount_id_number', ''),
                request.POST.get('discount_holder_name', ''),
            )
        elif decision == 'reject':
            reject_pwd_request(pk, request.user, request.POST.get('reject_reason', ''))
        else:
            raise DiscountError('Choose whether to approve or reject the request.')
    except DiscountError as e:
        messages.error(request, str(e))
        return redirect('transaction_detail', pk=pk)

    if decision == 'reject':
        messages.success(
            request,
            f'PWD request on {txn.order_number} not approved — the order stays '
            f'at the full price. You can now confirm its payment.'
        )
    elif priced['total_discount_amount'] == Decimal('0'):
        # Approved, but PWD eligibility is per product and nothing on the
        # order carries it (a product can lose the flag after checkout).
        messages.warning(
            request,
            f'PWD request on {txn.order_number} approved, but no item on it is '
            f'eligible for the discount — the total is unchanged.'
        )
    else:
        messages.success(
            request,
            f'PWD discount approved on {txn.order_number} — '
            f'₱{priced["total_discount_amount"]} off, now ₱{priced["total_amount"]}. '
            f'You can now confirm its payment.'
        )
    return redirect('transaction_detail', pk=pk)


# A SalesItem's product name, safe against a product hard-deleted from the
# catalog since the sale. One definition, in sales.refunds, shared with the
# refund workflow.
_item_label = refund_service.item_label


def _deduct_stock(txn, user):
    """Deduct each line item from the order's branch floor stock + audit it.

    Locks each Inventory row (select_for_update) and raises
    _OrderActionError instead of silently floor-ing at 0 (or skipping
    entirely when no Inventory row exists at all) when there isn't enough —
    the caller's atomic block rolls back the whole sale so a rejected sale
    never partially applies. A line whose product was since deleted from the
    catalog can never have stock to deduct (there is no Inventory row left to
    identify), so it always fails this same check rather than crashing on it.
    """
    for item in txn.items.select_related('product').all():
        # available_qty nets out anything past its expiry_date — stock that
        # expired after the order was placed but before it was fulfilled must
        # not be handed to the customer just because the order predates it.
        inv = Inventory.objects.select_for_update().sellable().filter(
            product=item.product, branch=txn.branch
        ).first()
        available = inv.available_qty if inv else 0
        if available < item.quantity_sold:
            raise _OrderActionError(
                f'Not enough stock for {_item_label(item)} — '
                f'available: {available}, needed: {item.quantity_sold}.'
            )
        before = inv.quantity_on_hand
        inv.quantity_on_hand -= item.quantity_sold
        inv.save()
        # FEFO: drain the nearest-expiry floor batches for this product/
        # branch. Batches now fully back quantity_on_hand (see Batch's own
        # docstring), so this should always find enough to consume — the
        # available_qty check above already confirmed non-expired batches
        # cover this sale.
        consumed = Batch.consume_fefo(item.product, txn.branch, Batch.Location.FLOOR, item.quantity_sold)
        # O-1 — record exactly which batch(es) this line drew from and how
        # much from each, straight off what consume_fefo already computed.
        # _reverse_stock reads this back to credit the SAME batches if the
        # sale is later voided, instead of only reversing the aggregate
        # Inventory row and leaving the batch-level FEFO breakdown short.
        SalesItemBatchConsumption.objects.bulk_create([
            SalesItemBatchConsumption(sales_item=item, batch=batch, quantity_taken=taken)
            for batch, taken in consumed
        ])
        StockMovement.objects.create(
            inventory=inv,
            location_type=StockMovement.LocationType.FLOOR,
            movement_type=StockMovement.MovementType.SALE,
            quantity_change=inv.quantity_on_hand - before,
            quantity_before=before,
            quantity_after=inv.quantity_on_hand,
            reference_id=txn.pk,
            notes=f'Order {txn.order_number}',
            created_by=user,
        )


def _reverse_stock(txn, user):
    """Undo a completed sale's stock deduction when it's voided — the
    inverse of _deduct_stock, called from transaction_set_status when a
    COMPLETED order moves to Cancelled. If the original Inventory row was
    deleted since the sale, get_or_create it rather than losing the
    reversal (mirrors how a fresh receiving credits a possibly-new row).

    O-1 — the aggregate Inventory row (source of truth for quantity_on_hand)
    is always fully reversed, exactly as before. What used to be lost is the
    batch-level FEFO breakdown: _deduct_stock now records, on each
    SalesItemBatchConsumption, exactly which batch(es) a line drew from and
    how much — this reads that back and credits those SAME batches, so the
    aggregate and the batch ledger agree again after a void instead of
    drifting apart forever. Two cases fall back to a fresh no-expiry batch,
    tagged as a void reversal so the missing expiry data reads as honestly
    unknown rather than invented: a sale recorded before this tracking
    existed (no consumption rows at all), and a consumption row whose batch
    was itself deleted since (SET_NULL leaves quantity_taken behind with no
    batch to credit).

    A line whose product was hard-deleted from the catalog since the sale
    (SalesItem.product is SET_NULL for exactly this) has no Inventory row to
    credit at all — product is a required field on it — so there is nothing
    to reverse; that line is skipped rather than raising IntegrityError
    trying to insert an Inventory row with no product. Returns the skipped
    items so the caller can tell the person voiding the order.
    """
    skipped = []
    for item in txn.items.select_related('product').all():
        if item.product_id is None:
            skipped.append(item)
            continue
        inv, _ = Inventory.objects.select_for_update().get_or_create(
            product=item.product, branch=txn.branch,
            defaults={'quantity_on_hand': 0, 'reorder_level': 0},
        )
        before = inv.quantity_on_hand
        inv.quantity_on_hand += item.quantity_sold
        inv.save()
        StockMovement.objects.create(
            inventory=inv,
            location_type=StockMovement.LocationType.FLOOR,
            movement_type=StockMovement.MovementType.ADJUSTMENT,
            quantity_change=inv.quantity_on_hand - before,
            quantity_before=before,
            quantity_after=inv.quantity_on_hand,
            reference_id=txn.pk,
            notes=f'Voided order {txn.order_number} — stock reversal',
            created_by=user,
        )
        _reverse_batches(item, txn, user)
    return skipped


def _reverse_batches(item, txn, user):
    """The batch-level half of _reverse_stock — credits back exactly the
    batch(es) item.batch_consumptions says the original sale drew from.
    Split out from _reverse_stock so the aggregate Inventory reversal above
    (which must always happen) can never be skipped by a batch-side problem.
    """
    consumptions = list(item.batch_consumptions.select_for_update().select_related('batch'))

    if not consumptions:
        # Legacy sale, recorded before SalesItemBatchConsumption existed —
        # there is nothing to look up. Same honest fallback as the missing-
        # batch case below: one fresh, no-expiry batch for the full quantity.
        _create_void_reversal_batch(item.product, txn, item.quantity_sold)
        return

    for consumption in consumptions:
        if consumption.batch is not None:
            batch = Batch.objects.select_for_update().get(pk=consumption.batch_id)
            batch.quantity += consumption.quantity_taken
            batch.save(update_fields=['quantity'])
        else:
            # The batch this consumption pointed at has since been deleted
            # (SET_NULL) — there is no specific lot left to credit, so the
            # quantity goes into a fresh, clearly-labelled one instead of
            # being silently lost.
            _create_void_reversal_batch(item.product, txn, consumption.quantity_taken)


def _create_void_reversal_batch(product, txn, quantity):
    """One fresh Floor batch, no expiry date, created to hold quantity that
    _reverse_batches could not credit back to a real batch. No-expiry reads
    as honestly unknown (Batch.expiry_date's own docstring: FEFO always
    treats it as expiring last, never first) rather than guessing a date the
    void has no way to know.
    """
    Batch.objects.create(
        product=product, branch=txn.branch, location=Batch.Location.FLOOR,
        quantity=quantity, received_date=timezone.localdate(), expiry_date=None,
        notes=f'Void reversal for {txn.order_number} — original batch unknown.',
    )


# ─────────────────────────────────────────────
# REFUNDS
# A financial correction on a Completed, Paid order — distinct from voiding
# a completed sale (which cancels the whole order). P14: a refund is a
# request that an Owner/Branch Manager approves or rejects; every rule lives
# in sales.refunds so these views and the DRF endpoints share it. No payment
# gateway is integrated, so an approved refund only ever records that money
# went back — see Refund's own docstring in sales/models.py.
# ─────────────────────────────────────────────

def _refund_request_from_post(post):
    """The refund form's untrusted fields, shaped for sales.refunds.

    Nothing is validated here beyond parsing — an unparsable quantity goes
    through as None and is refused by request_refund with the item's name."""
    lines = []
    for raw_id in post.getlist('item_id'):
        qty_raw = post.get(f'quantity_{raw_id}', '').strip()
        lines.append({
            'item_id': int(raw_id) if raw_id.isdigit() else None,
            'quantity': int(qty_raw) if qty_raw.isdigit() else None,
            'condition': post.get(f'condition_{raw_id}', ''),
        })
    conditions = {
        key[len('condition_'):]: value for key, value in post.items()
        if key.startswith('condition_')
    }
    return lines, post.get('refund_all') == '1', conditions


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def transaction_refund(request, pk):
    """Request a refund on some or all of a Completed, Paid order's items.
    Module-gated (SALES_POS) like every other order action. Any internal user
    with access to the branch may ask; an Owner/Branch Manager's own request
    is approved as it is submitted, while Staff's waits for one of them
    (transaction_refund_approve / transaction_refund_reject)."""
    txn = get_object_or_404(SalesTransaction, pk=pk)

    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    if request.method != 'POST':
        return redirect('transaction_detail', pk=pk)

    lines, refund_all, conditions = _refund_request_from_post(request.POST)
    try:
        outcome = refund_service.request_refund(
            pk, request.user,
            reason_code=request.POST.get('reason_code', ''),
            reason=request.POST.get('reason', ''),
            refund_method=request.POST.get('refund_method', ''),
            lines=lines, refund_all=refund_all, conditions=conditions,
        )
    except refund_service.RefundError as e:
        messages.error(request, str(e))
        return redirect('transaction_detail', pk=pk)

    refund = outcome.refund
    if outcome.approved:
        messages.success(
            request, f'Refund of ₱{refund.total_amount} recorded for {txn.order_number}.'
        )
        if outcome.unrestockable:
            messages.warning(
                request,
                f'{outcome.unrestockable} unit(s) marked Sellable could not be added back to '
                f'stock — the product has since been removed from the catalog.'
            )
    else:
        messages.success(
            request,
            f'Refund request of ₱{refund.total_amount} submitted for {txn.order_number}. '
            f'Nothing changes until the Owner or a Branch Manager approves it.'
        )
    return redirect('transaction_detail', pk=pk)


def _decide_refund(request, refund_pk, approve):
    refund = get_object_or_404(Refund.objects.select_related('transaction'), pk=refund_pk)
    txn = refund.transaction

    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')

    if request.user.role not in refund_service.DECIDER_ROLES:
        messages.error(request, 'Only the Owner or a Branch Manager can approve or reject a refund.')
        return redirect('transaction_detail', pk=txn.pk)

    if request.method != 'POST':
        return redirect('transaction_detail', pk=txn.pk)

    note = request.POST.get('decision_note', '')
    try:
        if approve:
            outcome = refund_service.approve_refund(refund.pk, request.user, note)
        else:
            outcome = refund_service.reject_refund(refund.pk, request.user, note)
    except refund_service.RefundError as e:
        messages.error(request, str(e))
        return redirect('transaction_detail', pk=txn.pk)

    if approve:
        messages.success(
            request,
            f'Refund #{refund.pk} approved — ₱{outcome.refund.total_amount} recorded for '
            f'{txn.order_number}.'
        )
        if outcome.unrestockable:
            messages.warning(
                request,
                f'{outcome.unrestockable} unit(s) marked Sellable could not be added back to '
                f'stock — the product has since been removed from the catalog.'
            )
    else:
        messages.success(request, f'Refund #{refund.pk} rejected. Nothing was changed on {txn.order_number}.')
    return redirect('transaction_detail', pk=txn.pk)


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def transaction_refund_approve(request, refund_pk):
    """Approve a Pending refund — restocks Sellable lines, adds to the order's
    refunded_amount and steps the billing record, all in one transaction."""
    return _decide_refund(request, refund_pk, approve=True)


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def transaction_refund_reject(request, refund_pk):
    """Reject a Pending refund. A reason is required; nothing else changes."""
    return _decide_refund(request, refund_pk, approve=False)


# ─────────────────────────────────────────────
# WALK-IN POS
# ─────────────────────────────────────────────

@login_required(login_url='accounts:login')
def transaction_add(request):
    if request.user.role not in [OWNER, BRANCH_MANAGER, STAFF]:
        messages.error(request, 'You do not have permission to record sales.')
        return redirect('transaction_list')

    user_branch = _pos_user_branch(request)

    # A half-provisioned Staff / Branch Manager account (role set, branch
    # profile missing) has no branch to ring a sale under. Refuse here, for
    # both GET and POST. Falling through instead handed them the Owner's
    # every-branch selector on GET, and on POST took the branch straight from
    # the form with no user_can_access_branch check.
    if user_branch is POS_BRANCH_UNASSIGNED:
        messages.error(
            request,
            'Your account is not assigned to a branch, so it cannot record '
            'sales. Ask the Owner or an administrator to set your branch.'
        )
        return redirect('transaction_list')

    if request.method != 'POST':
        return render(request, 'sales/transaction_form.html', _pos_context(request))

    # user_branch is now either a Branch (Staff / Manager, pinned) or None
    # (the Owner, who chooses one on the form).
    branch_id = user_branch.id if user_branch else request.POST.get('branch')
    # Validated against the model's choices, same fall-back-to-default shape as
    # discount_type below — an unrecognised value (a typo, a
    # tampered form) becomes Cash rather than being written to the column
    # verbatim, which broke the Billing report's payment breakdown and, worse,
    # slipped the amount-tendered check below (only "Cash" triggers it).
    # Validated against POS_PAYMENT_METHODS, not the model's full choice list
    # — SalesTransaction.ModeOfPayment also carries Cash on Delivery / Pay at
    # Store, which exist for online checkout and have no control on this
    # form at all. Falling back against the wider list let a tampered POST
    # (or a bug) write an online-only method to a walk-in sale, where the
    # receipt would say one thing and billing.signals's auto-created record
    # would default to Cash — the two silently disagreeing about how the
    # customer paid.
    mode_of_payment = request.POST.get('mode_of_payment') or SalesTransaction.ModeOfPayment.CASH
    if mode_of_payment not in POS_PAYMENT_METHODS:
        mode_of_payment = SalesTransaction.ModeOfPayment.CASH
    product_ids = request.POST.getlist('product_id')
    quantities = request.POST.getlist('quantity')

    # --- replaces (optional) -------------------------------------------------
    # Line items are immutable once an order exists, so "fixing" one is
    # cancel-then-reorder — this is the lightweight version of that link:
    # staff picks the cancelled order being corrected from a dropdown here,
    # rather than a guided one-click "Reorder" flow. Restricted to this
    # branch's own cancelled orders and silently dropped if it doesn't
    # resolve to one — a stale dropdown value shouldn't block the sale
    # itself, only the cross-reference is optional.
    replaces_id = request.POST.get('replaces') or None
    if replaces_id and not SalesTransaction.objects.filter(
        pk=replaces_id, branch_id=branch_id,
        order_status=SalesTransaction.OrderStatus.CANCELLED,
    ).exists():
        replaces_id = None

    # --- cash tendered -------------------------------------------------------
    # Only meaningful for a Cash sale — GCash/Card never had cash handed over,
    # so the field is parsed but required (below) only when Cash is chosen.
    amount_tendered_raw = (request.POST.get('amount_tendered') or '').strip()
    amount_tendered = None
    if mode_of_payment == SalesTransaction.ModeOfPayment.CASH and amount_tendered_raw:
        try:
            amount_tendered = Decimal(amount_tendered_raw)
            # Decimal("NaN") and Decimal("Infinity") both parse without
            # raising InvalidOperation — they are valid IEEE-754-style
            # Decimal values, just not finite ones. Left unchecked, either
            # crashes the change-due arithmetic below with a 500 instead of
            # the plain "must be a number" validation error every other bad
            # value in this field already gets.
            if not amount_tendered.is_finite():
                amount_tendered = None
        except InvalidOperation:
            amount_tendered = None

    line_items = []
    # Rows with a product but a bad quantity (blank, non-digit, zero) used to
    # be dropped here with no word to the cashier about which row vanished —
    # the sale only complained when EVERY row failed this check. Tracked
    # separately so a per-row error can name what was dropped, the same way
    # the "no longer available" check just below already does per row.
    dropped_pids = []
    for pid, q in zip(product_ids, quantities):
        if not pid:
            continue
        if q and q.isdigit() and int(q) > 0:
            line_items.append((pid, int(q)))
        else:
            dropped_pids.append(pid)

    # --- PWD / Senior Citizen discount ------------------------------------
    # Claimed once for the sale. The ID number is not optional: the statutory
    # discount is conditioned on presenting one, and a claim with no ID on
    # record is exactly what an audit would ask about.
    discount_type = request.POST.get('discount_type') or SalesTransaction.DiscountType.NONE
    if discount_type not in SalesTransaction.DiscountType.values:
        discount_type = SalesTransaction.DiscountType.NONE
    discount_claimed = discount_type != SalesTransaction.DiscountType.NONE
    discount_id_number = request.POST.get('discount_id_number', '').strip() if discount_claimed else ''
    discount_holder_name = request.POST.get('discount_holder_name', '').strip() if discount_claimed else ''

    # The same two checks, collected instead of redirected. A redirect threw
    # away every item row the cashier had already rung up.
    errors = []
    if not branch_id:
        errors.append('Please choose a branch for this sale.')
    else:
        # Check the resolved branch against the user's scope for EVERY role,
        # not just when it came from the Owner's dropdown. For Staff/Manager
        # branch_id is their own pinned branch (so this passes), but a posted
        # branch id must never reach SalesTransaction.create unchecked.
        try:
            branch_id_int = int(branch_id)
        except (TypeError, ValueError):
            branch_id_int = None
        # For an Owner, branch_ids_for_user returns None ("unrestricted"),
        # so user_can_access_branch alone says yes to ANY id, real or not —
        # "no restriction" was being read as "no need to check it exists".
        # If the branch was deleted while the Owner still had this page open,
        # the create() below would hit the FK constraint and 500 instead of
        # the friendly message every other stale-input case on this form
        # already gets (a deleted product, a bad quantity).
        branch = Branch.objects.filter(pk=branch_id_int).first() if branch_id_int else None
        if branch is None:
            errors.append('That branch no longer exists. Please choose another branch.')
        elif not user_can_access_branch(request.user, branch_id_int):
            errors.append('That branch is not one your account can record sales for.')
    if not line_items:
        errors.append('Add at least one item with a quantity.')
    if dropped_pids:
        names = []
        for pid in dropped_pids:
            try:
                product = Product.objects.filter(pk=pid).first()
            except (ValueError, TypeError):
                product = None
            names.append(product.product_name if product else f'item #{pid}')
        errors.append(
            f"Enter a valid quantity for {', '.join(names)} — that row "
            f"couldn't be recorded and was dropped."
        )

    # Resolve each line's product now, up here with the other input checks.
    # A product deleted between the cashier loading the form and submitting
    # it (or a tampered id) used to raise an uncaught Product.DoesNotExist
    # when the cart was priced below — a 500. Collected as a form error and
    # re-rendered instead, the same way a missing branch or a bad quantity
    # is, so the rung-up rows survive.
    products_by_id = {}
    for pid, _qty in line_items:
        try:
            products_by_id[str(pid)] = Product.objects.get(pk=pid)
        except (Product.DoesNotExist, ValueError, TypeError):
            pass
    if line_items and any(str(pid) not in products_by_id for pid, _qty in line_items):
        errors.append(
            'One or more items are no longer available — they may have been '
            'removed since the page loaded. Please review the cart and try again.'
        )
    if discount_claimed and not discount_id_number:
        errors.append(
            f'Enter the {discount_type} ID number — a discount cannot be '
            f'recorded without it.'
        )
    if discount_claimed and not discount_holder_name:
        errors.append(f'Enter the name on the {discount_type} ID.')
    if mode_of_payment == SalesTransaction.ModeOfPayment.CASH:
        if not amount_tendered_raw:
            errors.append('Enter the amount tendered for this cash sale.')
        elif amount_tendered is None:
            errors.append('Amount tendered must be a number.')

    if not errors:
        # Prices resolved and the total computed up front — same order as the
        # DRF API's create() — instead of creating the transaction at
        # total_amount=0 first (which left billing.signals creating an
        # amount_due=0 BillingRecord) and patching both afterward.
        #
        # sales.pricing owns the VAT and PWD/Senior arithmetic; this only
        # decides the price TIER and hands the lines over. Keeping the two
        # apart is what stops a discounted bulk line from being computed off
        # the retail price by accident.
        #
        # Quantity is totalled per product before the tier is read, so ringing
        # the same item up on two rows earns the same break as ringing it up
        # once — otherwise 5 + 5 would pay retail where a single 10 pays bulk.
        qty_by_product = {}
        for product_id, qty in line_items:
            key = str(product_id)
            qty_by_product[key] = qty_by_product.get(key, 0) + qty

        cart_lines = []
        for product_id, qty in line_items:
            product = products_by_id[str(product_id)]
            cart_lines.append(
                (product, qty, product.price_at(qty_by_product[str(product_id)]))
            )

        priced = price_cart(cart_lines, discount_type=discount_type)
        total = priced['total_amount']

        # Now that the total is known: a Cash sale can't tender less than
        # what's due. Checked here rather than up front — total isn't known
        # until the cart is priced.
        change_amount = None
        if mode_of_payment == SalesTransaction.ModeOfPayment.CASH and amount_tendered is not None:
            if amount_tendered < total:
                errors.append(
                    f'Amount tendered (₱{amount_tendered}) is less than the amount due (₱{total}).'
                )
            else:
                change_amount = amount_tendered - total

        if not errors:
            try:
                with transaction.atomic():
                    txn = SalesTransaction.objects.create(
                        branch_id=branch_id,
                        staff=request.user if request.user.role == STAFF else None,
                        # X-3 — whoever actually rang this up, regardless of
                        # role. `staff` above stays STAFF-only (its own
                        # limit_choices_to), so an Owner or Branch Manager
                        # sale otherwise recorded nobody at all.
                        recorded_by=request.user,
                        transaction_type=SalesTransaction.TransactionType.PHYSICAL,
                        mode_of_payment=mode_of_payment,
                        # A Cash sale is settled at the counter, so it is done. GCash
                        # and Card have to clear as a separate, verified step first
                        # (below), so those stay Pending until the payment is Paid.
                        order_status=(
                            SalesTransaction.OrderStatus.COMPLETED
                            if mode_of_payment == SalesTransaction.ModeOfPayment.CASH
                            else SalesTransaction.OrderStatus.PENDING
                        ),
                        # A Cash walk-in is Completed the instant it's created
                        # here — it never passes through transaction_set_status,
                        # which is the only other place completed_at is set.
                        completed_at=(
                            timezone.now()
                            if mode_of_payment == SalesTransaction.ModeOfPayment.CASH
                            else None
                        ),
                        total_amount=total,
                        discount_type=discount_type,
                        discount_id_number=discount_id_number,
                        discount_holder_name=discount_holder_name,
                        total_discount_amount=priced['total_discount_amount'],
                        total_vat_amount=priced['total_vat_amount'],
                        amount_tendered=amount_tendered,
                        change_amount=change_amount,
                        replaces_id=replaces_id,
                    )

                    for line in priced['lines']:
                        SalesItem.objects.create(
                            transaction=txn, product=line['product'],
                            quantity_sold=line['quantity'],
                            # The already-discounted unit price — SalesItem.save
                            # multiplies it out, so the discount must be in the
                            # price, not applied again on top of the subtotal.
                            unit_price=line['unit_price'],
                            is_vat_exempt_line=line['is_vat_exempt_line'],
                            vat_amount=line['vat_amount'],
                            discount_amount=line['discount_amount'],
                        )

                    # Walk-in sales leave the shelf immediately. Raises
                    # _OrderActionError (rolling back this whole atomic block,
                    # including the transaction/items just created above) if any
                    # line item is oversold.
                    _deduct_stock(txn, request.user)

                    # billing.signals already created the record (Pending, no
                    # confirmer). A cash walk-in is settled the moment it's
                    # rung — the money crossed the counter — so it's marked
                    # Paid here with the ringing user as confirmer, exactly as
                    # before. Every other method (GCash, Card) has to clear as
                    # a separate step through billing_confirm, checked by
                    # someone who didn't record the sale (A-1 maker/checker
                    # separation), so the record is left Pending for that.
                    billing = getattr(txn, 'billing_record', None)
                    if billing:
                        billing.amount_due = total
                        billing.payment_method = (
                            mode_of_payment if mode_of_payment in POS_PAYMENT_METHODS else 'Cash'
                        )
                        if mode_of_payment == SalesTransaction.ModeOfPayment.CASH:
                            billing.status = 'Paid'
                            billing.confirmed_by = request.user
                        billing.save()
            except _OrderActionError as e:
                errors.append(str(e))
            else:
                # A claim was recorded but landed on nothing: PWD and Senior
                # are separate statutes with separate eligibility flags, and
                # no line on this sale carries the one that matches. Say so
                # here too — the Order Console's after-the-fact discount flow
                # (transaction_apply_discount) already warns for exactly this
                # case, and a plain success message would leave the cashier
                # not knowing the total didn't move.
                if discount_claimed and priced['total_discount_amount'] == Decimal('0'):
                    messages.warning(
                        request,
                        f'{discount_type} recorded on {txn.order_number}, but no '
                        f'item in the sale is eligible for that discount — the '
                        f'total is unchanged.'
                    )
                else:
                    if txn.order_status == SalesTransaction.OrderStatus.PENDING:
                        messages.success(
                            request,
                            f'Sale {txn.order_number} recorded — ₱{total}. It stays '
                            f'Pending until the {mode_of_payment} payment is recorded '
                            f'and verified.'
                        )
                    else:
                        messages.success(request, f'Sale {txn.order_number} recorded — ₱{total}.')
                return redirect('transaction_detail', pk=txn.pk)

    # Rejected. Re-render the form with the header selections re-chosen and
    # the item rows rebuilt from what was posted — every pid/qty pair,
    # including the ones that failed the digit check, so a mistyped quantity
    # comes back visible instead of vanishing.
    context = _pos_context(request)
    context.update({
        'pos_errors': errors,
        'pos_form_data': request.POST,
        'pos_restore_rows': [
            {'product_id': pid, 'quantity': qty}
            for pid, qty in zip(product_ids, quantities) if pid
        ],
    })
    return render(request, 'sales/transaction_form.html', context)
