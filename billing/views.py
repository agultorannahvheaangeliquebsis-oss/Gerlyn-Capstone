from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.http import FileResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from .models import BillingRecord
from .api_views import user_can_access_billing_record
from .validators import clean_reference_number
from sales.models import Refund, SalesTransaction
from accounts.models import User
from branches.models import Branch
from core.pagination import paginate
from core.querystrings import list_filter_querystring
from core.scoping import branch_for_user, scope_queryset, user_can_access_branch
from core.timebounds import local_range_bounds, range_from_request
from core.permissions import OWNER, BRANCH_MANAGER, STAFF, role_required


def payment_methods_for_channel(transaction_type):
    """Which BillingRecord.PaymentMethod choices make sense for this
    transaction's channel (B-3) — billing_create used to offer, and accept,
    all six choices regardless of channel: a Physical (walk-in) sale could
    be "settled" as Cash on Delivery, and an Online order as a counter Cash
    or Card swipe, neither of which the counter can act on.

    GCash and Bank Transfer are remote settlements that make sense either
    way, so both channels keep them. Cash and Card are a counter
    transaction only a Physical sale has; Cash on Delivery and Pay at Store
    only ever describe an Online order.
    """
    common = [BillingRecord.PaymentMethod.GCASH, BillingRecord.PaymentMethod.BANK_TRANSFER]
    if transaction_type == SalesTransaction.TransactionType.ONLINE:
        return common + [
            BillingRecord.PaymentMethod.COD, BillingRecord.PaymentMethod.PAY_AT_STORE,
        ]
    return common + [BillingRecord.PaymentMethod.CASH, BillingRecord.PaymentMethod.CARD]


def _validate_proof_of_payment(file):
    """Same real-image check as accounts._validate_avatar — BillingRecord's
    proof_of_payment is a model ImageField, but that only validates through
    Django's form/serializer layer (billing/serializers.py's DRF ImageField
    does this correctly). billing_create assigns the uploaded file directly,
    which skips it entirely, so a renamed .html or .svg saves and serves
    with its own content-type — the actual vulnerability, independent of
    the /media/ access-control fix (F-06) already in billing_proof."""
    from PIL import Image
    max_bytes = settings.PROOF_OF_PAYMENT_MAX_MB * 1024 * 1024
    if file.size > max_bytes:
        raise ValidationError(f"File must be under {settings.PROOF_OF_PAYMENT_MAX_MB} MB.")
    try:
        file.seek(0)
        img = Image.open(file)
        img.verify()
        file.seek(0)
        if img.format.lower() not in ('jpeg', 'png', 'webp'):
            raise ValidationError("Only JPEG, PNG, and WEBP images are allowed.")
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("The uploaded file is not a valid image.")
    finally:
        file.seek(0)


@login_required(login_url='accounts:login')
@require_GET
def payment_proof_download(request, pk):
    """Stream a payment proof only to users allowed to see its billing row."""
    from .api_views import user_can_access_billing_record

    record = get_object_or_404(
        BillingRecord.objects.select_related(
            'transaction', 'transaction__branch'
        ),
        pk=pk,
    )
    if not user_can_access_billing_record(request.user, record):
        raise Http404
    if not record.proof_of_payment:
        raise Http404

    try:
        proof = record.proof_of_payment.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404

    return FileResponse(
        proof,
        content_type='application/octet-stream',
        as_attachment=False,
        filename=record.proof_of_payment.name.rsplit('/', 1)[-1],
    )


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def billing_list(request):
    """The staff Billing section. Internal roles only.

    X-1 — this used to also allow CUSTOMER and SUPPLIER, which dropped a
    shopper straight into the back-office shell with a table of billing
    records and, from there, a Pending record's live "Mark as Paid / Reject"
    card on billing_detail (POSTing it was already refused by
    billing_confirm's own role check, but the controls were drawn and
    clickable — the refusal should never have been the only guard). A
    customer already has the same information, scoped to just their own
    order and with no staff controls anywhere on it, on their own Track
    Order page (orders.customer_views.track_order) — nothing is lost by
    closing this door. billing_detail, billing_create and billing_proof
    below get the same restriction, for the same reason.
    """
    records = BillingRecord.objects.select_related('transaction', 'transaction__branch').all()

    # X-1 removed CUSTOMER/SUPPLIER access to this view entirely (role_required
    # above), so every caller who reaches here is OWNER/BRANCH_MANAGER/STAFF.
    # X-4 — the shared, fail-closed scoping helper (core.scoping), not a
    # fourth hand-rolled copy of "which branch may this user see". The old
    # try/except here caught a missing profile and fell back to .none() —
    # correct, but its own reimplementation of exactly what
    # branch_ids_for_user already guarantees for an unassigned Manager/Staff
    # account, and a `filter(branch=None)` mistake anywhere near this shape
    # would have matched every branch-deleted order instead of none of them.
    records = scope_queryset(records, request.user, 'transaction__branch')

    search_query = request.GET.get('q', '')
    if search_query:
        records = records.filter(
            Q(transaction__order_number__icontains=search_query) |
            Q(reference_number__icontains=search_query)
        )

    # Owner-only branch filter — Manager/Staff are already pinned to their
    # own branch's records above, so a selector would do nothing for them.
    user_branch = branch_for_user(request.user)
    branch_filter = request.GET.get('branch', '') if request.user.role == User.Role.OWNER else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''
    if branch_filter:
        records = records.filter(transaction__branch_id=branch_filter)

    # Channel and fulfilment dropdowns — the same two as the Order Console
    # (sales.views.transaction_list), filtering the bill's own order. Checked
    # against the model's choices so a hand-typed value falls back to "all"
    # instead of rendering an empty list.
    channel_filter = request.GET.get('channel', '')
    if channel_filter not in SalesTransaction.TransactionType.values:
        channel_filter = ''
    if channel_filter:
        records = records.filter(transaction__transaction_type=channel_filter)

    fulfillment_filter = request.GET.get('fulfillment', '')
    if fulfillment_filter not in SalesTransaction.FulfillmentType.values:
        fulfillment_filter = ''
    if fulfillment_filter:
        records = records.filter(transaction__fulfillment_type=fulfillment_filter)

    records = records.order_by('-date_recorded')

    # The shared back-office range control (_daterange.html), same as the
    # Dashboard, Reports, the audit log and Stock Adjustments. `range_start`
    # is None for "All time". date_recorded is a DateTimeField, so the local
    # calendar days are turned into aware bounds (inclusive of the whole end
    # day) rather than compared with __date.
    range_start, range_end, active_range = range_from_request(request)
    range_dt_start, range_dt_end = local_range_bounds(range_start or range_end, range_end)
    if range_start:
        records = records.filter(date_recorded__gte=range_dt_start)
    records = records.filter(date_recorded__lt=range_dt_end)

    status_filter = request.GET.get('status', '')
    counts = {
        'all': records.count(),
        'pending': records.filter(status=BillingRecord.PaymentStatus.PENDING).count(),
        'paid': records.filter(status=BillingRecord.PaymentStatus.PAID).count(),
        'rejected': records.filter(status=BillingRecord.PaymentStatus.REJECTED).count(),
        'refunded': records.filter(status=BillingRecord.PaymentStatus.REFUNDED).count(),
        'partially_refunded': records.filter(
            status=BillingRecord.PaymentStatus.PARTIALLY_REFUNDED
        ).count(),
        'cancelled': records.filter(status=BillingRecord.PaymentStatus.CANCELLED).count(),
    }

    # Outstanding totals the Pending bills in *the list being shown*, so it
    # is taken after every narrowing filter above — including the date range
    # — and only before the status filter, which is the one control that must
    # not change it (computing it after would zero the tile out on every tab
    # except All and Pending).
    #
    # The date range has to count. It is the same queryset the tab counts
    # beside it are taken from, so excusing Outstanding from the range made
    # one header disagree with itself: "Pending 3" under a total summing
    # thirty bills. A reader narrowing to a month is asking what that month
    # owes; the all-time backlog is one click away on "All time".
    outstanding = records.filter(
        status=BillingRecord.PaymentStatus.PENDING
    ).aggregate(total=Sum('amount_due'))['total'] or 0

    if status_filter:
        records = records.filter(status=status_filter)

    branches = Branch.objects.all() if request.user.role == User.Role.OWNER else []

    # Counts and Outstanding above describe the whole filtered list; only
    # the table is cut into pages. `-pk` makes the order total.
    page = paginate(request, records.order_by('-date_recorded', '-pk'))

    return render(request, 'billing/billing_list.html', {
        'records': page.object_list,
        'page_obj': page,
        'counts': counts,
        'status_filter': status_filter,
        'outstanding': outstanding,
        'search_query': search_query,
        'channel_filter': channel_filter,
        'fulfillment_filter': fulfillment_filter,
        'branches': branches,
        'branch_filter': branch_filter,
        # _daterange.html: a preset click must not drop the search/filters.
        'active_range': active_range,
        'range_start': range_start or range_end,
        'range_end': range_end,
        'daterange_carry': [
            ('q', search_query), ('status', status_filter),
            ('channel', channel_filter), ('fulfillment', fulfillment_filter),
            ('branch', branch_filter),
        ],
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Warehouse Stock, Product Master List, Stock Adjustments, Receive
        # Delivery and the Order Console.
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        'user_branch': user_branch,
        # O-3 — one urlencoded filter string for the status tabs, the same
        # fix and the same reasoning as sales.views._list_filter_querystring
        # (the Order Console's own copy of this problem): each tab used to
        # be a hand-built query string that silently broke on a search term
        # containing "&" or "#". Every status tab appends its own
        # &status=... to this.
        'filter_qs': list_filter_querystring(
            q=search_query, branch=branch_filter,
            channel=channel_filter, fulfillment=fulfillment_filter,
        ),
        # Every other active filter, for the topbar branch switcher
        # (partials/_branch_switch.html) to carry along — same idea as
        # _daterange.html's daterange_carry. The range itself rides along
        # via range_from_request's session fallback, as on the audit log.
        'branch_switch_carry': [
            ('q', search_query), ('status', status_filter),
            ('channel', channel_filter), ('fulfillment', fulfillment_filter),
        ],
    })


def _billing_activity(record, refunds):
    """The billing events this record can actually prove, oldest first.

    M4 limitation, stated here because it decides everything this function
    may return: there is NO BillingStatusHistory table. BillingRecord keeps
    only its current decision — confirmed_by / date_confirmed, plus
    reject_reason while it is Rejected — and billing_confirm CLEARS all
    three when a rejected payment is re-opened, so an earlier rejection
    leaves nothing behind to render. Nothing below is inferred: every entry
    is one timestamp persisted on this row (or a RefundEvent, which is
    append-only and never edited), and a transition the schema cannot date
    or attribute is simply not listed. This is deliberately not presented
    as a complete history — see _billing_activity.html. A real
    BillingStatusHistory model is a separate task.
    """
    S = BillingRecord.PaymentStatus
    txn = record.transaction

    events = [{
        'when': record.date_recorded,
        'what': 'Bill created',
        # No actor: billing.signals.auto_create_billing_record writes this
        # row when the order is created, so there is no "recorded by" on it
        # to show. Inventing one from the sale's own staff would be a guess.
        'who': '',
        'note': f'{txn.order_number} — ₱{record.amount_due} due.',
    }]

    # date_confirmed is the single decision the row remembers. What that
    # decision WAS comes from the row's own state, not from a guess:
    #   - Rejected           -> it was a rejection (reject_reason says why)
    #   - Paid / refunded    -> it was a verification (a refund only moves
    #                           `status`; sales.refunds never touches the
    #                           confirmer, see _apply_approval)
    #   - Cancelled          -> a reject_reason proves a rejection (only a
    #                           rejection ever writes one). Its ABSENCE proves
    #                           nothing: a rejection from before reject_reason
    #                           existed, then voided, leaves the same row as a
    #                           verification that was voided. So no reason ->
    #                           the decision is not listed at all, rather than
    #                           guessed to be "verified".
    #   - Pending            -> cleared on re-open; there is nothing to show
    #                           and nothing is invented in its place.
    if record.date_confirmed:
        rejected = record.status == S.REJECTED or (
            record.status == S.CANCELLED and record.reject_reason
        )
        verified = record.status in (S.PAID, S.PARTIALLY_REFUNDED, S.REFUNDED)
        if rejected:
            events.append({
                'when': record.date_confirmed,
                'what': 'Payment rejected',
                'who': record.confirmed_by,
                'note': record.reject_reason,
            })
        elif verified:
            events.append({
                'when': record.date_confirmed,
                'what': 'Payment verified',
                'who': record.confirmed_by,
                'note': '',
            })

    if record.date_voided:
        events.append({
            'when': record.date_voided,
            'what': 'Bill voided',
            'who': record.voided_by,
            'note': txn.cancel_reason or '',
        })

    # RefundEvent — the one append-only trail Billing can read honestly.
    # Requested / approved / rejected, each with the actor and note
    # sales.refunds itself wrote. Decisions stay on Sales & Orders; this
    # only reports them.
    # to_status as the verb it stands for — a Pending RefundEvent is the
    # request being raised, not a refund that is "pending". Nothing is added
    # or reinterpreted: one word per persisted status.
    refund_verbs = {
        Refund.Status.PENDING: 'requested',
        Refund.Status.APPROVED: 'approved',
        Refund.Status.REJECTED: 'rejected',
    }
    for refund in refunds:
        for event in refund.events.all():
            verb = refund_verbs.get(event.to_status, event.to_status.lower())
            events.append({
                'when': event.created_at,
                'what': f'Refund {verb} — ₱{refund.total_amount}',
                'who': event.actor,
                'note': event.note,
            })

    events.sort(key=lambda e: e['when'])
    return events


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def billing_detail(request, pk):
    record = get_object_or_404(
        BillingRecord.objects.select_related(
            'transaction', 'transaction__branch', 'transaction__customer',
            'confirmed_by', 'voided_by',
        ),
        pk=pk,
    )

    # The list is scoped, so the detail must be too — otherwise another
    # branch's billing record is readable just by changing the URL. X-1: no
    # CUSTOMER branch any more — role_required above already turned that
    # role away.
    if not user_can_access_branch(request.user, record.transaction.branch_id):
        messages.error(request, 'That billing record is not available to you.')
        return redirect('billing_list')

    # Local: sales.views imports this module, so a top-level import would
    # be circular.
    from sales.views import _next_actions as _order_next_actions

    txn = record.transaction
    allowed_payment_methods = payment_methods_for_channel(txn.transaction_type)
    refunds = list(
        txn.refunds.select_related('requested_by', 'refunded_by').prefetch_related(
            'items__sales_item__product', 'events__actor'
        )
    )

    return render(request, 'billing/billing_detail.html', {
        'record': record,
        # The Record Payment modal, included below so "Edit payment
        # details" opens it in place instead of navigating to a blank
        # page — same fields transaction_detail's own copy passes it.
        'payment_choices': [(m.value, m.label) for m in allowed_payment_methods],
        'reference_required_methods': sorted(BillingRecord.REFERENCE_REQUIRED_METHODS),
        'proof_required_methods': sorted(BillingRecord.PROOF_REQUIRED_METHODS),
        'proof_max_mb': settings.PROOF_OF_PAYMENT_MAX_MB,
        # Display-only: billing_confirm already refuses Paid server-side
        # when this is non-empty. Lets the template grey out Verify Payment
        # instead of letting the click round-trip into a refusal message.
        'missing_evidence': BillingRecord.missing_payment_evidence(
            record.payment_method, record.reference_number,
            bool(record.proof_of_payment),
        ),
        # A-1 maker/checker, resolved here because the rule takes the request
        # user as an argument and a template cannot pass one to a method
        # call. Same context transaction_detail already builds for its own
        # copy of these controls (billing_self_recorded there) — nothing new
        # is being decided, an existing server rule is simply said out loud
        # instead of only surfacing as a refusal after the click.
        'is_self_recorded': record.is_self_confirmation(request.user),
        # Read-only order context. Billing may write to BillingRecord;
        # everything it shows from SalesTransaction is read, never written,
        # and every order mutation is reached by navigating to the order.
        # Hence order_status as a value to display and no stepper, no status
        # control, no refund control on this page.
        'order_status': txn.order_status,
        # Whether "the order can now be marked Completed" is true for this
        # reader — asked of sales.views._next_actions, the rule
        # transaction_set_status itself enforces, rather than re-deriving
        # the order state machine here. Display only.
        'order_can_complete': (
            SalesTransaction.OrderStatus.COMPLETED in _order_next_actions(txn, request.user)
        ),
        'items': list(txn.items.select_related('product')),
        # Refunds are Sales & Orders' to decide — read here only so a Paid
        # record can say a request is waiting, and so the Activity block can
        # list the refund decisions that actually happened (RefundEvent is
        # append-only, the one real audit trail this page can draw on).
        'refunds': refunds,
        'pending_refunds': [
            r for r in refunds if r.status == Refund.Status.PENDING
        ],
        # Same arithmetic reports.views already uses for a refunded bill
        # (amount_due minus the order's approved refunds) — display only.
        'remaining_after_refunds': record.amount_due - txn.refunded_amount,
        'activity': _billing_activity(record, refunds),
    })


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def billing_proof(request, pk):
    """Stream a billing record's GCash payment proof, gated by the same
    scoping as the rest of billing.

    F-06 — payment proofs are the billing audit trail's evidence (customer
    name, reference number, amount paid). They were served straight off
    django.views.static.serve via the /media/ route with no auth check at
    all, so anyone who had or guessed a path under /media/payment_proofs/
    could read them. This view is now the ONLY way in: the /media/ route no
    longer serves payment_proofs/ (see gerlyn_is/urls.py), and callers who
    can't reach the record itself get a 404 that doesn't confirm it exists —
    the same helper and convention BillingRecordDetailAPIView already uses.

    X-1: internal roles only, same as the rest of this module — a customer's
    own proof of payment is not shown back to them anywhere in this system,
    and this endpoint never needs to.
    """
    record = get_object_or_404(
        BillingRecord.objects.select_related('transaction'), pk=pk
    )
    if not user_can_access_billing_record(request.user, record):
        raise Http404
    if not record.proof_of_payment:
        raise Http404
    # FileResponse streams the file and sets Content-Type/Content-Length from
    # the storage backend, so this works the same whether the file is on the
    # local disk or Cloudinary. Cache-Control is handled project-wide by
    # accounts.middleware.NoCacheForAuthenticatedMiddleware for every
    # authenticated response, this one included — nothing extra needed here.
    return FileResponse(record.proof_of_payment.open('rb'))


@role_required(OWNER, BRANCH_MANAGER, STAFF)
def billing_create(request, transaction_id):
    txn = get_object_or_404(SalesTransaction, pk=transaction_id)

    # Same scoping rule as billing_detail — otherwise any internal account
    # could create or edit the payment record for another branch's
    # transaction just by guessing the transaction id in the URL. X-1: no
    # CUSTOMER branch any more — role_required above already turned that
    # role away.
    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That transaction is not available to you.')
        return redirect('billing_list')

    # The signal may have already created a pending record.
    # If it exists and is still Pending, let the user fill in payment details.
    # If it's already Paid/Rejected, just show the existing record.
    existing = getattr(txn, 'billing_record', None)

    if existing and existing.status != BillingRecord.PaymentStatus.PENDING:
        # A stale "create billing" link followed into a record that has
        # since moved past Pending. Cancelled/Rejected are not just "already
        # exists" — the generic message understated what actually happened
        # (the order behind it was voided or cancelled, or the payment was
        # specifically turned down), which reads as if nothing was wrong.
        if existing.status == BillingRecord.PaymentStatus.CANCELLED:
            messages.info(
                request,
                'This order was cancelled — its billing record is closed.'
            )
        elif existing.status == BillingRecord.PaymentStatus.REJECTED:
            messages.info(
                request,
                'This payment was rejected. Re-open it from the billing '
                'record if the customer is paying again.'
            )
        else:
            messages.info(request, 'A billing record already exists for this transaction.')
        return redirect('billing_detail', pk=existing.pk)

    # This page has two front doors — the order's own detail page, and the
    # billing record's "Edit payment details" / "Upload proof". It used to
    # carry one hardcoded back-link to the order, so anyone who arrived from
    # Billing was quietly moved into Sales & Orders on the way back, while
    # the page header still said "Billing".
    #
    # The entry links state where they came from. A `from` flag rather than a
    # `?next=<url>`: this only ever chooses between two known routes, and a
    # flag cannot be pointed at an arbitrary destination, so there is nothing
    # to validate. Anything unrecognised falls through to the order, which is
    # the older of the two paths and the safe default.
    #
    # Three levels deep with a record in the middle is the case DESIGN.md's
    # breadcrumb rule reserves the component for: the section and the record
    # are both invisible otherwise.
    if request.GET.get('from') == 'billing' and existing is not None:
        trail = [
            ('Billing', reverse('billing_list')),
            (txn.order_number, reverse('billing_detail', args=[existing.pk])),
        ]
    else:
        trail = [
            ('Sales & Orders', reverse('transaction_list')),
            (txn.order_number, reverse('transaction_detail', args=[txn.pk])),
        ]

    allowed_payment_methods = payment_methods_for_channel(txn.transaction_type)
    form_context = {
        'transaction': txn,
        'record': existing,  # Pass to template so it can pre-fill payment_method if desired
        'payment_choices': [(m.value, m.label) for m in allowed_payment_methods],
        'breadcrumb': trail,
        # The form shows each field only for the methods that need it; the
        # lists come from the model so the page and the server rule agree.
        'reference_required_methods': sorted(BillingRecord.REFERENCE_REQUIRED_METHODS),
        'proof_required_methods': sorted(BillingRecord.PROOF_REQUIRED_METHODS),
    }

    if request.method == 'POST':
        # The Record Payment modal posts here with return_to=order (from the
        # order's own page) or return_to=billing (from the billing record's
        # page) and expects to land back on whichever one it came from. A
        # flag rather than a URL, for the same reason as `from` above: it
        # only ever chooses between known destinations.
        return_to = request.POST.get('return_to')
        from_order = return_to == 'order'
        from_billing = return_to == 'billing' and existing is not None

        def refuse(message):
            messages.error(request, message)
            if from_order:
                # ?record_payment=1 reopens the modal over the message, so the
                # cashier corrects the form instead of hunting for the button.
                return redirect(
                    reverse('transaction_detail', args=[txn.pk]) + '?record_payment=1'
                )
            if from_billing:
                return redirect(
                    reverse('billing_detail', args=[existing.pk]) + '?record_payment=1'
                )
            return render(request, 'billing/billing_form.html', form_context)

        payment_method = request.POST.get('payment_method')
        reference_number = request.POST.get('reference_number', '').strip()
        proof_of_payment = request.FILES.get('proof_of_payment')

        # payment_method feeds a NOT NULL column. A POST that omits the field
        # (the <select> stripped out, a scripted request) used to reach the
        # model save and surface as an IntegrityError 500. Re-render the form
        # with the error instead — nothing is persisted. Beyond "present", it
        # must also be one of the real choices — otherwise a tampered POST
        # (or a typo'd value like "Paid") saves a payment method the counter
        # can't act on, exactly like an unpaid order reading "Paid". And
        # since B-3, "real" means valid for THIS transaction's channel — the
        # dropdown only ever offers those, but the check has to hold even
        # against a POST that bypasses it (e.g. Cash on Delivery for a
        # walk-in sale that was already paid at the counter).
        if not payment_method or payment_method not in {m.value for m in allowed_payment_methods}:
            return refuse('Choose a valid payment method to record this payment.')

        if proof_of_payment:
            try:
                _validate_proof_of_payment(proof_of_payment)
            except ValidationError as e:
                return refuse(e.message)

        # X-2 — with no payment gateway integrated, the reference number and
        # the screenshot ARE the record of a remote payment. What each method
        # needs is BillingRecord.missing_payment_evidence (one definition,
        # shared with the API and Mark as Paid). A proof already on file from
        # an earlier save counts — editing the reference shouldn't force a
        # re-upload. Anything not required is cleared: a reference typed
        # before switching methods must not ride along onto a record that has
        # no reference by definition.
        if payment_method not in BillingRecord.REFERENCE_REQUIRED_METHODS:
            reference_number = ''
        else:
            # Length and characters — the column is VARCHAR(100), and an
            # over-long value used to reach it and 500. Same rule as the API
            # and checkout (billing.validators).
            try:
                reference_number = clean_reference_number(
                    reference_number, payment_method
                )
            except ValidationError as e:
                return refuse(e.messages[0])
        missing = BillingRecord.missing_payment_evidence(
            payment_method, reference_number,
            bool(proof_of_payment or (existing and existing.proof_of_payment)),
        )
        if missing:
            return refuse(
                f'A {payment_method} payment needs its {" and ".join(missing)} '
                f'before it can be saved — we cannot match the payment to '
                f'this order without it.'
            )

        if existing:
            # Update the auto-created record with the actual payment details.
            # Locked and re-checked as Pending inside the write, so a
            # confirmation that landed since the read above isn't rolled back
            # (BillingRecord.record_payment_details also syncs the sale).
            try:
                existing.record_payment_details(
                    payment_method, reference_number, proof_of_payment
                )
            except ValidationError:
                messages.info(request, 'A billing record already exists for this transaction.')
                if from_order:
                    return redirect('transaction_detail', pk=txn.pk)
                return redirect('billing_detail', pk=existing.pk)
            record = existing
        else:
            # Fallback: create fresh if somehow the signal didn't fire
            with transaction.atomic():
                record = BillingRecord.objects.create(
                    transaction=txn,
                    amount_due=txn.total_amount,
                    payment_method=payment_method,
                    reference_number=reference_number,
                    proof_of_payment=proof_of_payment,
                    status=BillingRecord.PaymentStatus.PENDING,
                )
                record.sync_payment_method_to_sale()

        # Recording a payment is a save, not a settlement — Verify/Reject
        # (billing_confirm) is a separate step by a separate person (A-1
        # maker/checker), and a plain "saved" leaves that unstated. When the
        # user who just recorded it is also the one A-1 blocks from
        # confirming it (is_self_confirmation), say so explicitly rather
        # than let them discover it only after clicking Verify.
        if record.is_self_confirmation(request.user):
            messages.success(
                request,
                'Payment recorded. You can\'t verify this one yourself — '
                'ask another staff member, a manager, or the owner to '
                'confirm it.'
            )
        else:
            messages.success(
                request,
                'Payment recorded. It still needs verification by another '
                'staff member or manager.'
            )
        if from_order:
            return redirect('transaction_detail', pk=txn.pk)
        return redirect('billing_detail', pk=record.pk)

    return render(request, 'billing/billing_form.html', form_context)


@login_required(login_url='accounts:login')
def billing_confirm(request, pk):
    record = get_object_or_404(
        BillingRecord.objects.select_related('transaction'), pk=pk
    )

    # Order Detail (sales.views.transaction_detail) now offers the same
    # Verify/Reject/Re-open controls billing_detail always has, posting to
    # this same view with return_to=order so staff never have to leave the
    # order to finish confirming its payment. Every redirect below honours
    # it — including the early refusals — so a user who arrived from the
    # order is bounced back to the order, not into Billing, whatever the
    # outcome. Defaults to billing_detail, same as before this existed.
    return_to = request.POST.get('return_to')

    def _back(pk=pk):
        if return_to == 'order':
            return redirect('transaction_detail', pk=record.transaction_id)
        return redirect('billing_detail', pk=pk)

    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'You do not have permission to confirm payments.')
        return _back()

    # Same scoping rule as billing_detail/billing_create — without this, any
    # Staff/Manager could confirm or reject a payment belonging to another
    # branch just by knowing the record id.
    if not user_can_access_branch(request.user, record.transaction.branch_id):
        messages.error(request, 'That billing record is not available to you.')
        return redirect('billing_list')

    if request.method == 'POST':
        new_status = request.POST.get('status')
        # The Pending card offers Mark as Paid / Reject; a Rejected record's
        # card offers Re-open (back to Pending). Anything else is a tampered
        # POST and is rejected outright rather than persisted as an arbitrary
        # string that would quietly break status-based filters.
        if new_status not in BillingRecord.HUMAN_CONFIRMABLE_STATUSES:
            messages.error(request, 'Invalid payment status.')
            return _back()

        with transaction.atomic():
            # Re-fetch and lock so two confirmations racing the same record
            # (double-click, retry) can't both pass the transition check
            # against the same stale, pre-lock status.
            locked = BillingRecord.objects.select_for_update().select_related(
                'transaction'
            ).get(pk=pk)

            # A-1 — maker/checker separation. Whether this user may act on
            # the record at all is logically prior to what they're moving it
            # to, so this runs before the transition check (same ordering as
            # C-6's branch-scope gate). Only a decision that records a
            # confirmer — Paid or Rejected — is gated; re-opening a rejected
            # record to Pending clears confirmed_by and has nothing to guard.
            if (
                new_status != BillingRecord.PaymentStatus.PENDING
                and locked.is_self_confirmation(request.user)
            ):
                messages.error(
                    request,
                    "You can't confirm a payment you recorded yourself — ask "
                    "another staff member, a manager, or the owner."
                )
                return _back()

            if not locked.can_transition_to(new_status):
                messages.error(
                    request,
                    f'{locked.transaction.order_number}: a {locked.status} '
                    f'payment cannot be changed to {new_status}.'
                )
                return _back()

            # A remote method has no counter moment where handing over money
            # and confirming it are the same event — Mark as Paid is the only
            # record that a GCash/Card/bank payment ever happened, and once
            # Paid it can never be undone (ALLOWED_STATUS_TRANSITIONS has no
            # outbound edge from it). What each method must have on file is
            # BillingRecord.missing_payment_evidence; a record saved before
            # that rule existed can still be short, which is why it is
            # re-checked here and not only when the payment is saved.
            if new_status == BillingRecord.PaymentStatus.PAID:
                # P15 — the amount due is not final while a PWD discount
                # request is unresolved. `locked.transaction` came from the
                # same locking query, so this is the current status.
                blocked = locked.pwd_review_block_message()
                if blocked:
                    messages.error(request, blocked)
                    return _back()

                missing = BillingRecord.missing_payment_evidence(
                    locked.payment_method, locked.reference_number,
                    bool(locked.proof_of_payment),
                )
                if missing:
                    messages.error(
                        request,
                        f'{locked.transaction.order_number}: a {locked.payment_method} '
                        f'payment needs its {" and ".join(missing)} before it can '
                        f'be marked Paid — add it from "Edit payment details" first.'
                    )
                    return _back()

            # A rejection needs a reason on file — it is the only record of
            # why a payment wasn't accepted, read by both staff (the
            # Payment Details panel) and the customer (the rejection
            # notification below).
            if new_status == BillingRecord.PaymentStatus.REJECTED:
                reason = request.POST.get('reason', '').strip()[:255]
                if not reason:
                    messages.error(
                        request,
                        f'{locked.transaction.order_number}: a reason is '
                        f'required to reject this payment.'
                    )
                    return _back()
                locked.reject_reason = reason

            locked.status = new_status
            if new_status == BillingRecord.PaymentStatus.PENDING:
                # Re-opening a rejected payment — clear the prior decision so
                # the record reads as genuinely awaiting a fresh check, not
                # as still "confirmed by / on" the rejection.
                locked.confirmed_by = None
                locked.date_confirmed = None
                locked.reject_reason = ''
            else:
                locked.confirmed_by = request.user
                locked.date_confirmed = timezone.now()
            locked.save()

        verb = 're-opened' if new_status == BillingRecord.PaymentStatus.PENDING else f'marked as {new_status}'
        # What changed, and what it unlocks — Order Detail's Mark Completed
        # button is gated on Paid (see sales.views._next_actions), and a
        # customer-facing rejection/re-open both leave the order itself
        # exactly where it was, so the next step is spelled out here rather
        # than left for whoever reads the record to work out.
        next_step = {
            BillingRecord.PaymentStatus.PAID: ' The order can now be marked Completed.',
            BillingRecord.PaymentStatus.REJECTED: ' The customer has been notified; record a new payment attempt when they pay again.',
            BillingRecord.PaymentStatus.PENDING: ' It is awaiting a fresh payment attempt.',
        }.get(new_status, '')
        messages.success(request, f'{record.transaction.order_number} payment {verb}.{next_step}')

        if new_status == BillingRecord.PaymentStatus.REJECTED:
            from core.module_permissions import BILLING as BILLING_MODULE
            from core.notifications import (
                CATEGORY_SALES, EVENT_PAYMENT_REJECTED, PRIORITY_HIGH, notify,
            )

            txn = record.transaction
            notify(
                CATEGORY_SALES, PRIORITY_HIGH,
                f'Payment rejected — {txn.order_number}',
                f'Payment for {txn.order_number} ({txn.branch.branch_name}, '
                f'₱{txn.total_amount}) was rejected by {request.user.username}: '
                f'{locked.reject_reason}',
                branch=txn.branch,
                action_url=reverse('billing_detail', kwargs={'pk': record.pk}),
                action_label='View Billing Record',
                required_module=BILLING_MODULE,
                event_type=EVENT_PAYMENT_REJECTED,
                reference_type='billing_record', reference_id=record.pk,
                exclude_user=request.user,
            )

        if new_status in {
            BillingRecord.PaymentStatus.PAID,
            BillingRecord.PaymentStatus.REJECTED,
        }:
            from .notifications import notify_payment_decision

            # Rejection already reached the internal audience immediately
            # above; this call adds the customer. Paid only has a customer
            # audience in the matrix.
            notify_payment_decision(
                locked, request.user, include_internal=False
            )

    return _back()
