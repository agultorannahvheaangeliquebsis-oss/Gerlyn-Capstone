"""The damage-report workflow's rules, in one place.

Both the staff/manager views (damage_reports.views) and the customer views
(orders.damage_views) call these functions, so validation, permissions, branch
scoping and the stock / refund side effects cannot drift between the two.

Lifecycle (see damage_reports.models.DamageReport):

    file (create_staff_report / create_customer_report)  -> Pending
    start_review                                          -> Under Review
    approve_report   -> Approved   the ONE place anything moves:
                                   write_off     takes the units out of floor or
                                                 warehouse stock, by batch, and
                                                 generates a withdrawal slip
                                   replacement   same, from the floor, for the
                                                 unit(s) handed to the customer
                                   refund        raises a refund through the
                                                 existing refund workflow
                                   return_to_supplier  only flags the goods; the
                                                 supplier return takes them out
                                                 of the warehouse when it ships
                                   none          records the damage only
    reject_report    -> Rejected   nothing moves; the photos stay for audit
    resolve_report   -> Resolved   an Approved report is closed out

No double deduction. A report is decided once: approval re-reads the report
under a row lock and refuses unless it is still Pending or Under Review, so a
second click, a second tab or a second manager gets "already approved" instead
of a second deduction. Underneath that, stock_deducted_at and the one-to-one
withdrawal slip / refund links make a second deduction impossible to record.

Locking order is always: the report, then the order (refunds), then the stock
row, then its batches. Every writer here takes them in that order.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.urls import reverse
from django.utils import timezone

from audit.capture import record_detail
from branches.models import Branch
from core.limits import (
    MAX_DESCRIPTION_LENGTH, MAX_NOTE_LENGTH, parse_quantity, parse_text,
)
from core.notifications import PRIORITY_MEDIUM
from core.permissions import BRANCH_MANAGER, CUSTOMER, OWNER, STAFF
from core.scoping import branch_for_user, scope_queryset, user_can_access_branch
from inventory.models import (
    Batch, Inventory, Product, StockMovement, StockTransfer, StockTransferItem,
    Warehouse, WarehouseStock,
)
from inventory.services import record_transfer_event
from procurement.access import procurements_visible_to
from procurement.models import Procurement
from sales.models import Refund, RefundItem, SalesTransaction

from .errors import DamageReportError, DamageReportForbidden
from .models import DamageReport, DamageReportEvent, DamageReportPhoto
from .photos import validate_photos

logger = logging.getLogger(__name__)

Status = DamageReport.Status
Resolution = DamageReport.Resolution
Location = DamageReport.StockLocation
DamageType = DamageReport.DamageType

REPORTER_ROLES = (OWNER, BRANCH_MANAGER, STAFF)
MANAGER_ROLES = (OWNER, BRANCH_MANAGER)

_LOCATION_WORD = {Location.FLOOR: 'floor', Location.WAREHOUSE: 'warehouse'}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _to_int(raw):
    """int from untrusted input, or None. A 5,000-digit string raises
    ValueError in int(), so anything that long is simply "not an id"."""
    text = ('' if raw is None else str(raw)).strip()
    if not text or len(text) > 12 or not (text.isascii() and text.isdigit()):
        return None
    return int(text)


def _where(location):
    return _LOCATION_WORD.get(location, 'stock')


def _log(report, from_status, to_status, actor, note=''):
    return DamageReportEvent.objects.create(
        report=report, from_status=from_status or '', to_status=to_status,
        actor=actor, note=(note or '').strip()[:500],
    )


def _unit_cost(product):
    return Decimal(product.bulk_floor_price or 0).quantize(Decimal('0.01'))


def _units(n):
    return f'{n} unit{"" if n == 1 else "s"}'


def reports_visible_to(user, queryset=None):
    """The reports `user` may read. Owner: every branch. Branch Manager: their
    branch. Staff: only the reports they filed, in their branch. Anyone else
    (customers use orders.damage_views, which looks reports up by their own
    order): nothing. scope_queryset fails closed for an unassigned account."""
    qs = queryset if queryset is not None else DamageReport.objects.all()
    role = getattr(user, 'role', None)
    if role == OWNER:
        return qs
    if role == BRANCH_MANAGER:
        return scope_queryset(qs, user, 'branch')
    if role == STAFF:
        return scope_queryset(qs, user, 'branch').filter(reported_by=user)
    return qs.none()


# ---------------------------------------------------------------------------
# What can still be reported
# ---------------------------------------------------------------------------

def _stock_row(branch_id, product_id, location, *, lock=False):
    """The aggregate Inventory (floor) or WarehouseStock row, or None."""
    if location == Location.FLOOR:
        qs = Inventory.objects.filter(branch_id=branch_id, product_id=product_id)
    elif location == Location.WAREHOUSE:
        warehouse = Warehouse.objects.filter(branch_id=branch_id).first()
        if warehouse is None:
            return None
        qs = WarehouseStock.objects.filter(warehouse=warehouse, product_id=product_id)
    else:
        return None
    return (qs.select_for_update() if lock else qs).first()


def available_stock(report):
    """Units a write-off of `report` could take right now (its named batch, or
    the whole stock row), or None when the report has no product/branch. The
    approve panel shows it so a manager sees a shortfall before pressing the
    button rather than after."""
    if report.product_id is None or report.branch_id is None:
        return None
    if report.inventory_batch_id:
        return report.inventory_batch.quantity
    location = report.stock_location or Location.FLOOR
    stock = _stock_row(report.branch_id, report.product_id, location)
    return stock.quantity_on_hand if stock else 0


def claimed_quantity(branch_id, product_id, location, batch_id=None, *, lock=False):
    """Units of this product/location already spoken for by another report
    (see DamageReportQuerySet.holding_stock). For a named batch only reports
    against that batch count; with no batch, every report on the product does.
    `lock` takes a locking read, which sees rows committed since this
    transaction's snapshot began - what the create path needs when it has just
    waited on the stock row's lock."""
    qs = DamageReport.objects.holding_stock().filter(
        branch_id=branch_id, product_id=product_id, stock_location=location,
    )
    if batch_id:
        qs = qs.filter(inventory_batch_id=batch_id)
    if lock:
        return sum(qs.select_for_update().values_list('quantity', flat=True))
    return qs.aggregate(n=Sum('quantity'))['n'] or 0


def reportable_quantity(branch_id, product_id, location, batch=None, *, lock=False):
    """How many units a new report could still name: what is on hand (the
    batch's own quantity when one is named, else the aggregate stock row) less
    what other open reports already hold."""
    if batch is not None:
        on_hand = batch.quantity
    else:
        stock = _stock_row(branch_id, product_id, location)
        on_hand = stock.quantity_on_hand if stock else 0
    claimed = claimed_quantity(
        branch_id, product_id, location, batch.pk if batch is not None else None,
        lock=lock,
    )
    return max(on_hand - claimed, 0)


def form_stock_options(branch, procurement=None):
    """What the staff report form offers, as JSON-ready dicts: for each
    location, the products with stock there and their batches, each with the
    most that can still be reported. Server-side validation is the authority;
    this only lets the form show sensible maximums and a batch picker.

    With `procurement` (the form opened from a delivery) only the lots that
    purchase order delivered are offered, wherever they sit now.
    """
    batches = Batch.objects.filter(branch=branch, quantity__gt=0).select_related('product')
    if procurement is not None:
        batches = batches.filter(procurement=procurement)
    batches = list(batches.order_by('product__product_name', F('expiry_date').asc(nulls_last=True), 'pk'))

    claims = {}
    for row in (
        DamageReport.objects.holding_stock().filter(branch=branch)
        .values('stock_location', 'product_id', 'inventory_batch_id')
        .annotate(n=Sum('quantity'))
    ):
        claims[(row['stock_location'], row['product_id'], row['inventory_batch_id'])] = row['n']

    def claimed_product(location, product_id):
        return sum(
            n for (loc, pid, _b), n in claims.items() if loc == location and pid == product_id
        )

    floor = {
        r.product_id: r.quantity_on_hand
        for r in Inventory.objects.filter(branch=branch, quantity_on_hand__gt=0)
    }
    warehouse = {
        r.product_id: r.quantity_on_hand
        for r in WarehouseStock.objects.filter(warehouse__branch=branch, quantity_on_hand__gt=0)
    }
    aggregate = {Location.FLOOR.value: floor, Location.WAREHOUSE.value: warehouse}

    grouped = {Location.FLOOR.value: {}, Location.WAREHOUSE.value: {}}
    products = {}
    for batch in batches:
        grouped.setdefault(batch.location, {}).setdefault(batch.product_id, []).append(batch)
        products[batch.product_id] = batch.product

    if procurement is None:
        # Legacy stock with no batch row still has an aggregate quantity.
        for product in Product.objects.filter(pk__in=set(floor) | set(warehouse)):
            products.setdefault(product.pk, product)
            for location in (Location.FLOOR.value, Location.WAREHOUSE.value):
                if product.pk in aggregate[location]:
                    grouped[location].setdefault(product.pk, [])

    result = []
    for location, label in (
        (Location.FLOOR.value, 'Store floor'), (Location.WAREHOUSE.value, 'Warehouse'),
    ):
        entries = []
        for product_id, lots in grouped[location].items():
            product = products[product_id]
            if procurement is not None:
                on_hand = sum(b.quantity for b in lots)
            else:
                on_hand = aggregate[location].get(product_id, 0)
            if on_hand <= 0:
                continue
            entries.append({
                'id': product.pk,
                # What the form posts to name this product in this location.
                'key': f'{location}|{product.pk}',
                'name': product.product_name,
                'sku': product.sku_code,
                'on_hand': on_hand,
                'reportable': max(on_hand - claimed_product(location, product_id), 0),
                'batches': [
                    {
                        'id': b.pk,
                        'number': b.batch_number,
                        'quantity': b.quantity,
                        'reportable': max(
                            b.quantity - claims.get((location, product_id, b.pk), 0), 0
                        ),
                        'expiry': b.expiry_date.isoformat() if b.expiry_date else '',
                        'procurement_id': b.procurement_id,
                    }
                    for b in lots
                ],
            })
        entries.sort(key=lambda e: e['name'].lower())
        result.append({'location': location, 'label': label, 'products': entries})
    return result


# ---------------------------------------------------------------------------
# Notifications. After the business transaction, and fail-open: a notice that
# cannot be written must never turn a decision that was recorded into a 500.
# ---------------------------------------------------------------------------

def _managers_for(branch):
    """Active Owners, plus the branch's active Manager(s) who can open this
    module. Never Staff: they hold the module to file reports, not to decide."""
    from accounts.models import User
    from core.module_permissions import DAMAGE_REPORTS, user_can_access_module

    people = list(User.objects.filter(role=OWNER, is_active=True))
    if branch is not None:
        people += [
            u for u in User.objects.filter(
                role=BRANCH_MANAGER, is_active=True, manager_profile__branch=branch,
            )
            if user_can_access_module(u, DAMAGE_REPORTS)
        ]
    return people


def _notify_submitted(report):
    from core.notifications import EVENT_DAMAGE_REPORT_SUBMITTED, notify_event

    who = 'A customer' if report.is_customer_report else (
        report.reported_by.get_full_name() or report.reported_by.username
        if report.reported_by else 'Staff'
    )
    try:
        notify_event(
            EVENT_DAMAGE_REPORT_SUBMITTED,
            f'New damage report {report.report_number}',
            f'{who} reported {_units(report.quantity)} of {report.product_label} '
            f'({report.get_damage_type_display().lower()}) at '
            f'{report.branch.branch_name if report.branch else "an unassigned branch"}. '
            'Nothing changes until it is reviewed.',
            branch=report.branch, actor=report.reported_by,
            related_users={'manager': _managers_for(report.branch)},
            action_url=reverse('damage_report_detail', args=[report.pk]),
            action_label='Review Report',
            reference_type='damage_reports.DamageReport', reference_id=report.pk,
        )
    except Exception:
        logger.exception('Damage report submitted notice failed for %s', report.pk)


def _notify_status(report, actor, headline, *, detail='', priority=None):
    """Tell whoever filed the report that its status changed."""
    from core.notifications import EVENT_DAMAGE_REPORT_STATUS, notify_event

    reporter = report.reported_by
    if reporter is None:
        return
    if reporter.role == CUSTOMER and report.sales_transaction_id:
        url = reverse('orders:track_order', args=[report.sales_transaction_id])
        label = 'View Order'
    else:
        url = reverse('damage_report_detail', args=[report.pk])
        label = 'View Report'
    try:
        notify_event(
            EVENT_DAMAGE_REPORT_STATUS,
            f'Damage report {report.report_number} {headline}',
            f'Your report about {_units(report.quantity)} of {report.product_label} '
            f'{headline}.{(" " + detail) if detail else ""}',
            branch=report.branch, actor=actor,
            related_users={'reporter': reporter},
            action_url=url, action_label=label, priority=priority,
            reference_type='damage_reports.DamageReport', reference_id=report.pk,
        )
    except Exception:
        logger.exception('Damage report status notice failed for %s', report.pk)


# ---------------------------------------------------------------------------
# Filing a report: staff
# ---------------------------------------------------------------------------

def _reporting_branch(actor, branch_id):
    if getattr(actor, 'role', None) not in REPORTER_ROLES:
        raise DamageReportForbidden('Your account cannot file damage reports.')
    if actor.role == OWNER:
        pk = _to_int(branch_id)
        branch = Branch.objects.filter(pk=pk, is_active=True).first() if pk else None
        if branch is None:
            raise DamageReportError('Choose the branch this report is for.')
        return branch
    assigned = branch_for_user(actor)
    if assigned is None:
        raise DamageReportError('No branch is assigned to your account.')
    if branch_id and str(branch_id).strip() != str(assigned.pk):
        raise DamageReportForbidden(
            'Staff and Managers can report damage only for their assigned branch.'
        )
    return assigned


def create_staff_report(
    actor, *, branch_id=None, location, product_id, batch_id=None, quantity,
    damage_type, description, procurement_id=None, photos=(),
):
    """File a report against stock the branch holds. Every argument is
    untrusted client input; nothing here takes a branch, a price or a status
    from the request beyond what is checked. Returns the new DamageReport.
    """
    branch = _reporting_branch(actor, branch_id)

    if location not in Location.values:
        raise DamageReportError('Choose whether the stock is on the floor or in the warehouse.')
    if damage_type not in [t.value for t in DamageReport.STAFF_DAMAGE_TYPES]:
        raise DamageReportError('Choose what kind of damage this is.')

    product = Product.objects.filter(pk=_to_int(product_id)).first() if _to_int(product_id) else None
    if product is None:
        raise DamageReportError('Choose the product that was damaged.')

    batch = None
    if _to_int(batch_id):
        batch = Batch.objects.filter(
            pk=_to_int(batch_id), branch=branch, product=product, location=location,
        ).first()
        if batch is None:
            raise DamageReportError(
                f'That batch is not part of {product.product_name} in '
                f'{branch.branch_name}\'s {_where(location)} stock.'
            )
    elif batch_id not in (None, '', 0, '0'):
        raise DamageReportError('Choose a valid batch.')

    procurement = None
    if _to_int(procurement_id):
        procurement = procurements_visible_to(
            actor, Procurement.objects.filter(pk=_to_int(procurement_id), branch=branch),
        ).first()
        if procurement is None:
            raise DamageReportError('That purchase order is not one you can report against.')
        if not procurement.items.filter(product=product).exists():
            raise DamageReportError(
                f'{product.product_name} is not on purchase order {procurement.po_number}.'
            )
        if batch is not None and batch.procurement_id and batch.procurement_id != procurement.pk:
            raise DamageReportError(
                f'Batch {batch.batch_number} came in on a different purchase order.'
            )
    elif batch is not None and batch.procurement_id:
        # The lot already knows which delivery it came from - keep that link so
        # the report can be flagged for return to the right supplier.
        procurement = batch.procurement

    qty, error = parse_quantity(quantity, 'Quantity', minimum=1)
    if error:
        raise DamageReportError(error)
    text, error = parse_text(
        description, 'Description', max_length=MAX_DESCRIPTION_LENGTH, required=True,
    )
    if error:
        raise DamageReportError(error)
    cleaned_photos = validate_photos(photos)

    with transaction.atomic():
        stock = _stock_row(branch.pk, product.pk, location, lock=True)
        if stock is None or stock.quantity_on_hand <= 0:
            raise DamageReportError(
                f'{product.product_name} has no {_where(location)} stock at '
                f'{branch.branch_name} to report.'
            )
        locked_batch = Batch.objects.select_for_update().get(pk=batch.pk) if batch else None
        on_hand = locked_batch.quantity if locked_batch else stock.quantity_on_hand
        if qty > on_hand:
            raise DamageReportError(
                f'You reported {qty}, but only {_units(on_hand)} of {product.product_name} '
                f'{"are in this batch" if locked_batch else "are in " + _where(location) + " stock"}.'
            )
        available = reportable_quantity(
            branch.pk, product.pk, location, locked_batch, lock=True,
        )
        if qty > available:
            raise DamageReportError(
                f'Only {_units(available)} of {product.product_name} can still be reported '
                f'- the rest are already part of an open damage report.'
            )

        report = DamageReport.objects.create(
            branch=branch, reported_by=actor,
            reporter_type=DamageReport.ReporterType.STAFF,
            product=product, product_name=product.product_name,
            inventory_batch=locked_batch, stock_location=location,
            procurement=procurement, quantity=qty, damage_type=damage_type,
            description=text, estimated_unit_cost=_unit_cost(product),
        )
        for upload in cleaned_photos:
            DamageReportPhoto.objects.create(report=report, image=upload)
        _log(report, '', Status.PENDING, actor, 'Report filed.')
    _notify_submitted(report)
    return report


# ---------------------------------------------------------------------------
# Filing a report: customer
# ---------------------------------------------------------------------------

def report_window_closes(txn):
    """When the customer's reporting window ends (the refund window), or None
    for an order that was never completed."""
    return txn.completed_at + txn.REFUND_WINDOW if txn.completed_at else None


def customer_block_reason(txn, customer):
    """Why `customer` cannot report a problem with `txn` right now, or ''.
    The one definition the order page asks to decide what to draw and the
    create path re-asks against the locked row to decide what to allow."""
    if getattr(customer, 'role', None) != CUSTOMER or txn.customer_id != customer.pk:
        return 'That order is not on your account.'
    if txn.order_status != SalesTransaction.OrderStatus.COMPLETED:
        return 'You can report a problem once your order has been received.'
    if txn.refund_window_expired():
        return (
            f'The {txn.REFUND_WINDOW.days}-day window to report a problem with '
            f'{txn.order_number} has closed.'
        )
    return ''


def reportable_lines(txn):
    """The order's items a customer could still report, each with how many
    units remain. A unit is no longer reportable once a refund holds it, or
    once another report (not Rejected) names it and did not become a refund."""
    reserved = dict(
        RefundItem.objects.filter(
            sales_item__transaction=txn, refund__status__in=Refund.RESERVING_STATUSES,
        ).values('sales_item_id').annotate(n=Sum('quantity')).values_list('sales_item_id', 'n')
    )
    claimed = dict(
        DamageReport.objects.filter(
            sales_transaction=txn, reporter_type=DamageReport.ReporterType.CUSTOMER,
            refund__isnull=True,
        ).exclude(status=Status.REJECTED)
        .values('sales_item_id').annotate(n=Sum('quantity')).values_list('sales_item_id', 'n')
    )
    rows = []
    for item in txn.items.select_related('product'):
        if item.product_id is None:
            continue
        remaining = item.quantity_sold - reserved.get(item.pk, 0) - claimed.get(item.pk, 0)
        if remaining > 0:
            rows.append({'item': item, 'remaining': remaining})
    return rows


def create_customer_report(
    customer, transaction_id, *, sales_item_id, quantity, description, photos=(),
):
    """A customer reports a problem with an item on a received order, inside
    the refund window. At least one photo is required. Returns the report."""
    if getattr(customer, 'role', None) != CUSTOMER:
        raise DamageReportForbidden('Only a customer can report a problem with an order.')

    with transaction.atomic():
        # No select_related here: on MySQL a locking read also locks the joined
        # Branch row, which every order of that branch shares.
        txn = SalesTransaction.objects.select_for_update().filter(
            pk=_to_int(transaction_id), customer=customer,
        ).first()
        if txn is None:
            raise DamageReportForbidden('That order is not on your account.')
        reason = customer_block_reason(txn, customer)
        if reason:
            raise DamageReportError(reason)

        lines = {row['item'].pk: row for row in reportable_lines(txn)}
        row = lines.get(_to_int(sales_item_id))
        if row is None:
            raise DamageReportError(
                'Choose an item from this order that has not already been refunded or reported.'
            )
        item = row['item']
        qty, error = parse_quantity(quantity, 'Quantity', minimum=1)
        if error:
            raise DamageReportError(error)
        if qty > row['remaining']:
            raise DamageReportError(
                f'You can report at most {_units(row["remaining"])} of '
                f'{item.product.product_name} on this order.'
            )
        text, error = parse_text(
            description, 'Description', max_length=MAX_DESCRIPTION_LENGTH, required=True,
        )
        if error:
            raise DamageReportError(error)
        cleaned_photos = validate_photos(photos, minimum=1)

        report = DamageReport.objects.create(
            branch=txn.branch, reported_by=customer,
            reporter_type=DamageReport.ReporterType.CUSTOMER,
            product=item.product, product_name=item.product.product_name,
            stock_location='', sales_transaction=txn, sales_item=item,
            quantity=qty, damage_type=DamageType.CUSTOMER, description=text,
            estimated_unit_cost=_unit_cost(item.product),
        )
        for upload in cleaned_photos:
            DamageReportPhoto.objects.create(report=report, image=upload)
        _log(report, '', Status.PENDING, customer, 'Reported by the customer.')
    _notify_submitted(report)
    return report


# ---------------------------------------------------------------------------
# Deciding a report
# ---------------------------------------------------------------------------

def allowed_resolutions(report):
    """The resolutions a manager may pick when approving `report`, best first.

    What each one does is decided here and nowhere else:
      customer report  -> refund (needs the order line), replacement, none
      staff report     -> write-off, return to supplier, none
    Return to supplier is only offered for warehouse stock that came in on a
    purchase order: that is the only stock a supplier return can take out.
    """
    options = []
    if report.is_customer_report:
        if report.sales_item_id:
            options.append(Resolution.REFUND)
        if report.product_id:
            options.append(Resolution.REPLACEMENT)
    else:
        supplier_ok = bool(
            report.procurement_id and report.stock_location == Location.WAREHOUSE
        )
        write_off_ok = bool(report.product_id)
        if supplier_ok and report.damage_type == DamageType.ARRIVAL:
            options.append(Resolution.RETURN_TO_SUPPLIER)
        if write_off_ok:
            options.append(Resolution.WRITE_OFF)
        if supplier_ok and report.damage_type != DamageType.ARRIVAL:
            options.append(Resolution.RETURN_TO_SUPPLIER)
    options.append(Resolution.NONE)
    return options


def effect_text(report, resolution, *, done=False):
    """One sentence on what picking `resolution` does (or, with done=True,
    did) - shown beside the choice and written to the report's history."""
    qty = _units(report.quantity)
    name = report.product_label
    if resolution == Resolution.WRITE_OFF:
        where = _where(report.stock_location)
        batch = f' (batch {report.inventory_batch.batch_number})' if report.inventory_batch_id else ''
        return (
            f'{"Removed" if done else "Removes"} {qty} of {name} from {where} stock{batch} '
            f'and {"generated" if done else "generates"} a withdrawal slip.'
        )
    if resolution == Resolution.REPLACEMENT:
        return (
            f'{"Issued" if done else "Issues"} {qty} of {name} from floor stock as a '
            f'replacement and {"generated" if done else "generates"} a withdrawal slip. No refund.'
        )
    if resolution == Resolution.REFUND:
        amount = report.quantity * report.sales_item.unit_price if report.sales_item_id else None
        money = f' (₱{amount:,.2f})' if amount is not None else ''
        order = report.sales_transaction.order_number if report.sales_transaction_id else 'the order'
        return (
            f'{"Refunded" if done else "Refunds"} {qty}{money} on {order} through the '
            f'refund workflow. Stock is not touched: the goods were already sold.'
        )
    if resolution == Resolution.RETURN_TO_SUPPLIER:
        po = report.procurement.po_number if report.procurement_id else 'its purchase order'
        return (
            f'{"Flagged" if done else "Flags"} {qty} for return to the supplier on {po}. '
            f'Stock leaves the warehouse when the supplier return is shipped, not now.'
        )
    return 'Records the damage with no stock or money movement.' if not done else (
        'Recorded the damage with no stock or money movement.'
    )


def _locked_for_decision(report_id, actor):
    if getattr(actor, 'role', None) not in MANAGER_ROLES:
        raise DamageReportForbidden('Only the Owner or a Branch Manager can decide a damage report.')
    report = DamageReport.objects.select_for_update().filter(pk=report_id).first()
    if report is None:
        raise DamageReportError('That damage report no longer exists.')
    if not user_can_access_branch(actor, report.branch_id):
        raise DamageReportForbidden('That damage report belongs to another branch.')
    return report


def _already(report):
    return DamageReportError(
        f'{report.report_number} was already {report.status.lower()}.'
    )


def start_review(report_id, actor):
    """Pending -> Under Review: someone has picked the report up."""
    with transaction.atomic():
        report = _locked_for_decision(report_id, actor)
        if report.status != Status.PENDING:
            raise _already(report)
        report.status = Status.UNDER_REVIEW
        report.save()
        _log(report, Status.PENDING, Status.UNDER_REVIEW, actor, 'Review started.')
        record_detail(report, f'Started reviewing damage report {report.report_number}')
    _notify_status(report, actor, 'is now under review')
    return report


def reject_report(report_id, actor, *, reason):
    """Pending / Under Review -> Rejected. Nothing moves, and the photos stay."""
    reason, error = parse_text(reason, 'Reason', max_length=MAX_NOTE_LENGTH, required=True)
    if error:
        raise DamageReportError(error)
    with transaction.atomic():
        report = _locked_for_decision(report_id, actor)
        if report.status not in DamageReport.OPEN_STATUSES:
            raise _already(report)
        previous = report.status
        report.status = Status.REJECTED
        report.reviewed_by = actor
        report.reviewed_at = timezone.now()
        report.resolution = Resolution.NONE
        report.resolution_notes = reason
        report.save()
        _log(report, previous, Status.REJECTED, actor, f'Rejected: {reason}')
        record_detail(report, f'Rejected damage report {report.report_number}: {reason}')
    _notify_status(
        report, actor, 'was rejected', detail=f'Reason: {reason}',
        priority=PRIORITY_MEDIUM,
    )
    return report


def resolve_report(report_id, actor, *, note=''):
    """Approved -> Resolved: the follow-through is done and the report closes."""
    note, error = parse_text(note, 'Notes', max_length=MAX_NOTE_LENGTH)
    if error:
        raise DamageReportError(error)
    with transaction.atomic():
        report = _locked_for_decision(report_id, actor)
        if report.status != Status.APPROVED:
            if report.status == Status.RESOLVED:
                raise _already(report)
            raise DamageReportError(
                f'{report.report_number} must be approved before it can be resolved.'
            )
        report.status = Status.RESOLVED
        if note:
            report.resolution_notes = (
                f'{report.resolution_notes}\n{note}' if report.resolution_notes else note
            )
        report.save()
        _log(report, Status.APPROVED, Status.RESOLVED, actor, note or 'Marked resolved.')
        record_detail(report, f'Resolved damage report {report.report_number}')
    _notify_status(report, actor, 'was resolved')
    return report


def approve_report(report_id, actor, *, resolution, note='', refund_method=''):
    """Pending / Under Review -> Approved, applying the chosen resolution.

    Everything - the status change, the stock deduction, the withdrawal slip,
    the refund - happens in one transaction. If any part refuses (not enough
    stock now, the order can no longer be refunded) none of it is kept and the
    report is still open.
    """
    note, error = parse_text(note, 'Notes', max_length=MAX_NOTE_LENGTH)
    if error:
        raise DamageReportError(error)

    with transaction.atomic():
        report = _locked_for_decision(report_id, actor)
        if report.status not in DamageReport.OPEN_STATUSES:
            raise _already(report)
        if resolution not in [r.value for r in allowed_resolutions(report)]:
            raise DamageReportError('Choose how this report is resolved.')

        if resolution in (Resolution.WRITE_OFF, Resolution.REPLACEMENT):
            _deduct_stock(report, actor, resolution)
        elif resolution == Resolution.REFUND:
            _raise_refund(report, actor, refund_method)
        elif resolution == Resolution.RETURN_TO_SUPPLIER:
            _flag_for_supplier_return(report)

        previous = report.status
        report.status = Status.APPROVED
        report.reviewed_by = actor
        report.reviewed_at = timezone.now()
        report.resolution = resolution
        report.resolution_notes = note
        report.save()

        effect = effect_text(report, resolution, done=True)
        _log(report, previous, Status.APPROVED, actor, f'{effect} {note}'.strip())
        record_detail(
            report,
            f'Approved damage report {report.report_number} ({report.get_resolution_display()}): {effect}',
        )
    _notify_status(
        report, actor, 'was approved',
        detail=f'Outcome: {report.get_resolution_display().lower()}.',
    )
    return report


def _deduct_stock(report, actor, resolution):
    """Take the reported units out of stock, by batch, and write the
    withdrawal slip that records it. Must run inside approve_report's atomic
    block, with the report row already locked."""
    if report.stock_deducted_at or report.withdrawal_slip_id:
        # The unique slip link and this timestamp are the backstop behind the
        # status check: even a code path that skipped it could not deduct twice.
        raise DamageReportError(f'Stock for {report.report_number} was already deducted.')
    branch = report.branch
    if branch is None or report.product_id is None:
        raise DamageReportError('This report no longer has a branch and product to deduct from.')

    # A replacement hands a unit over from the shelf; a write-off comes out of
    # whichever stock the reporter said the units were in.
    location = Location.FLOOR if resolution == Resolution.REPLACEMENT else report.stock_location
    where = _where(location)
    qty = report.quantity
    warehouse = Warehouse.objects.filter(branch=branch).first()
    if warehouse is None:
        raise DamageReportError(
            f'No warehouse is configured for {branch.branch_name}, so the withdrawal slip '
            'cannot be created.'
        )

    stock = _stock_row(branch.pk, report.product_id, location, lock=True)
    if stock is None:
        raise DamageReportError(
            f'{report.product_label} is no longer in {where} stock at {branch.branch_name}.'
        )
    if qty > stock.quantity_on_hand:
        raise DamageReportError(
            f'Only {_units(stock.quantity_on_hand)} of {report.product_label} are in {where} '
            f'stock now, but this report is for {qty}. Reject it, or pick a resolution that '
            'does not remove stock.'
        )

    if report.inventory_batch_id:
        batch = Batch.objects.select_for_update().filter(pk=report.inventory_batch_id).first()
        if (
            batch is None or batch.branch_id != branch.pk
            or batch.product_id != report.product_id or batch.location != location
        ):
            raise DamageReportError(
                'The batch named on this report no longer matches the stock it was reported against.'
            )
        if batch.quantity < qty:
            raise DamageReportError(
                f'Batch {batch.batch_number} has only {_units(batch.quantity)} left, but this '
                f'report is for {qty}. It may already have been written off or sold.'
            )
        batch.quantity -= qty
        batch.save(update_fields=['quantity'])
        consumed = [(batch, qty)]
    else:
        # No lot named: oldest expiry first, expired lots included - damaged
        # and expired goods are exactly what a write-off is for.
        consumed = Batch.consume_fefo(
            report.product, branch, Batch.Location(location), qty, include_expired=True,
        )
    untracked = qty - sum(taken for _batch, taken in consumed)

    is_replacement = resolution == Resolution.REPLACEMENT
    requester = report.reported_by if (
        report.reported_by_id and not report.is_customer_report
    ) else actor
    # The concrete model, not the StockWriteOff proxy: the audit signals are
    # connected to StockTransfer, and a proxy's save sends them under the
    # proxy's own name, so the slip's creation would go unlogged.
    slip = StockTransfer.objects.create(
        warehouse=warehouse, source_branch=branch, destination_branch=None,
        purpose=(
            StockTransfer.Purpose.OTHER if is_replacement
            else StockTransfer.Purpose.DAMAGED_SPOILED
        ),
        requested_by=requester, approved_by=actor,
        status=StockTransfer.Status.ACKNOWLEDGED,
        transfer_date=timezone.localdate(), acknowledged_by=actor, received_qty=qty,
        notes=(
            f'Damage report {report.report_number} - {report.get_damage_type_display()}'
            f'{" (customer replacement)" if is_replacement else ""}: {report.description}'
        )[:1000],
    )
    StockTransferItem.objects.create(
        transfer=slip, product=report.product, quantity_requested=qty,
        quantity_approved=qty, quantity_released=qty, quantity_picked=qty,
        release_method=StockTransferItem.ReleaseMethod.MANUAL,
    )
    record_transfer_event(
        slip, '', StockTransfer.Status.ACKNOWLEDGED, actor,
        f'Generated from damage report {report.report_number}: '
        f'{_units(qty)} removed from {where} stock.',
    )

    before = stock.quantity_on_hand
    stock.quantity_on_hand = before - qty
    stock.save()
    movement_target = (
        {'inventory': stock} if location == Location.FLOOR else {'warehouse_stock': stock}
    )
    running = before
    for batch, taken in consumed + ([(None, untracked)] if untracked > 0 else []):
        StockMovement.objects.create(
            **movement_target,
            location_type=(
                StockMovement.LocationType.FLOOR if location == Location.FLOOR
                else StockMovement.LocationType.WAREHOUSE
            ),
            movement_type=StockMovement.MovementType.WRITE_OFF,
            quantity_change=-taken, quantity_before=running,
            quantity_after=running - taken, reference_id=slip.pk, batch=batch,
            notes=f'Damage report {report.report_number} - slip {slip.slip_number}',
            created_by=actor,
        )
        running -= taken

    report.withdrawal_slip = slip
    report.stock_deducted_at = timezone.now()


def _raise_refund(report, actor, refund_method):
    """Hand a customer's report to the existing refund workflow.

    The refund is raised and, because `actor` is an Owner/Manager, approved in
    the same step by sales.refunds - so the order's refund window, billing
    status, remaining quantity and money bookkeeping are the refund workflow's
    own. The window is judged when the customer FILED the report, not now.
    """
    from sales import refunds

    if not report.sales_item_id or not report.sales_transaction_id:
        raise DamageReportError('This report is not tied to an order line, so it cannot be refunded.')
    method = (refund_method or '').strip()
    if method not in Refund.RefundMethod.values:
        raise DamageReportError('Choose how the money is given back.')
    try:
        outcome = refunds.request_refund(
            report.sales_transaction_id, actor,
            reason_code=Refund.ReasonCode.DEFECTIVE,
            reason=f'Damage report {report.report_number}',
            refund_method=method,
            lines=[{
                'item_id': report.sales_item_id,
                'quantity': report.quantity,
                # The goods are damaged: nothing goes back on the shelf.
                'condition': RefundItem.Condition.DAMAGED,
            }],
            as_of=report.created_at,
        )
    except refunds.RefundError as exc:
        raise DamageReportError(str(exc))
    if not outcome.approved:
        raise DamageReportError(
            'The refund was raised but not approved, so this report was left open.'
        )
    report.refund = outcome.refund


def _flag_for_supplier_return(report):
    if not report.procurement_id:
        raise DamageReportError(
            'This report is not linked to a purchase order, so it cannot be flagged for '
            'return to the supplier.'
        )
    if report.stock_location != Location.WAREHOUSE:
        raise DamageReportError(
            'Only warehouse stock can go back to the supplier. Write these units off instead.'
        )
    report.flagged_for_supplier_return = True
