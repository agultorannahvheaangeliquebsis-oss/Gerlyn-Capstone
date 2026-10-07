"""Inventory mutation and opening-stock migration services.

Every floor-stock credit updates the aggregate Inventory row, creates a lot,
and writes a StockMovement in one transaction. Views should never update an
inventory quantity directly.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from branches.models import Branch
from .models import (
    Batch, Inventory, OpeningStockImport, OpeningStockImportRow, Product,
    StockMovement, StockTransfer, StockTransferEvent, StockTransferItem,
    StockWriteOff, Warehouse, WarehouseFloorTransfer, WarehouseStock,
)


class OpeningStockError(Exception):
    pass


class OpeningStockDuplicateError(OpeningStockError):
    pass


class OpeningStockValidationError(OpeningStockError):
    pass


class ExpiryProcessingError(Exception):
    """An expired lot could not be reconciled with its aggregate stock row."""


class StockTransferRequestError(Exception):
    """A stock request violates its source/destination business rules."""


class StockTransferReceiptError(Exception):
    """A transfer receipt cannot be reconciled safely."""


class CountCorrectionError(Exception):
    """A physical-count correction cannot be applied safely."""


def transfers_visible_to(user, queryset=None):
    """Transfers involving the user's branch; Owners see every transfer."""
    from core.scoping import branch_ids_for_user

    qs = queryset if queryset is not None else StockTransfer.objects.all()
    branch_ids = branch_ids_for_user(user)
    if branch_ids is None:
        return qs
    return qs.filter(
        Q(source_branch_id__in=branch_ids) |
        Q(destination_branch_id__in=branch_ids)
    ).distinct()


def warehouse_available_quantity(product, branch, *, lock=False):
    qs = WarehouseStock.objects
    if lock:
        qs = qs.select_for_update()
    stock = qs.filter(
        warehouse__branch=branch, product=product,
    ).first()
    if stock is None:
        return 0
    batch_available = Batch.available_quantity(
        product, branch, Batch.Location.WAREHOUSE,
    )
    return stock.quantity_on_hand if batch_available is None else min(
        stock.quantity_on_hand, batch_available,
    )


def record_transfer_event(transfer, from_status, to_status, actor, note=''):
    return StockTransferEvent.objects.create(
        transfer=transfer,
        from_status=from_status or '',
        to_status=to_status,
        actor=actor,
        note=(note or '').strip(),
    )


def _notify_new_stock_request(transfer_id):
    from core.module_permissions import WITHDRAWALS
    from core.notifications import (
        CATEGORY_PROCUREMENT, PRIORITY_MEDIUM, notify,
    )

    transfer = StockTransfer.objects.select_related(
        'source_branch', 'destination_branch', 'requested_by',
    ).get(pk=transfer_id)
    destination = transfer.destination_branch.branch_name if transfer.destination_branch else 'No destination'
    notify(
        CATEGORY_PROCUREMENT,
        PRIORITY_MEDIUM,
        f'Stock request {transfer.slip_number} needs source approval',
        f'{destination} requested stock from {transfer.source_branch.branch_name}. '
        'No stock will move until the source branch approves and releases it.',
        branch=transfer.source_branch,
        action_url=reverse('transfer_detail', args=[transfer.pk]),
        action_label='Review Request',
        required_module=WITHDRAWALS,
        reference_type='inventory.StockTransfer',
        reference_id=transfer.pk,
        exclude_user=transfer.requested_by,
    )


def _notify_new_write_off_request(write_off_id):
    from core.module_permissions import WITHDRAWALS
    from core.notifications import CATEGORY_PROCUREMENT, PRIORITY_MEDIUM, notify

    write_off = StockWriteOff.objects.select_related(
        'source_branch', 'requested_by',
    ).get(pk=write_off_id)
    notify(
        CATEGORY_PROCUREMENT,
        PRIORITY_MEDIUM,
        f'Write-off {write_off.slip_number} needs approval',
        f'{write_off.source_branch.branch_name} submitted a '
        f'{write_off.get_purpose_display().lower()} write-off. No floor stock '
        'will be removed until it is approved and completed.',
        branch=write_off.source_branch,
        action_url=reverse('transfer_detail', args=[write_off.pk]),
        action_label='Review Write-off',
        required_module=WITHDRAWALS,
        reference_type='inventory.StockWriteOff',
        reference_id=write_off.pk,
        exclude_user=write_off.requested_by,
    )


def _create_stock_workflow(
    *, actor, source_branch_id=None, destination_branch_id=None, purpose,
    items, transfer_date=None, notes='',
):
    """Shared persistence helper for the two explicit stock workflows.

    Restricted users always request *for* their assigned branch. For a
    Branch Restock they may name another active branch as the source, but the
    local warehouse must be exhausted for every requested product first.
    Non-restock purposes are local floor write-offs and never have a
    destination branch.
    """
    from accounts.models import User
    from audit.context import set_action_label
    from core.scoping import branch_for_user

    if actor.role not in (User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF):
        raise StockTransferRequestError('Your account cannot create stock requests.')

    normalized = []
    seen = set()
    for item in items:
        try:
            product_id = int(item['product_id'])
            quantity = int(item['quantity'])
        except (KeyError, TypeError, ValueError):
            raise StockTransferRequestError('Every item needs a valid product and quantity.')
        if quantity <= 0:
            raise StockTransferRequestError('Requested quantities must be greater than zero.')
        if product_id in seen:
            raise StockTransferRequestError('A product can appear only once in a stock request.')
        seen.add(product_id)
        normalized.append((product_id, quantity))
    if not normalized:
        raise StockTransferRequestError('Add at least one product to the stock request.')

    products = Product.objects.in_bulk(seen)
    if len(products) != len(seen):
        raise StockTransferRequestError('One or more selected products no longer exist.')

    assigned_branch = branch_for_user(actor)
    is_restock = purpose == StockTransfer.Purpose.BRANCH_RESTOCK

    if actor.role != User.Role.OWNER:
        if assigned_branch is None:
            raise StockTransferRequestError('No branch is assigned to your account.')
        try:
            requested_destination_id = (
                int(destination_branch_id) if destination_branch_id else None
            )
        except (TypeError, ValueError) as exc:
            raise StockTransferRequestError('Choose a valid destination branch.') from exc
        if requested_destination_id and requested_destination_id != assigned_branch.pk:
            raise StockTransferRequestError(
                'Staff and Managers can request stock only for their assigned branch.'
            )
        destination_branch = assigned_branch if is_restock else None
        local_branch = assigned_branch
    else:
        owner_branch_id = destination_branch_id if is_restock else source_branch_id
        if not owner_branch_id:
            raise StockTransferRequestError('Choose the branch this request is for.')
        local_branch = Branch.objects.filter(pk=owner_branch_id, is_active=True).first()
        if local_branch is None:
            raise StockTransferRequestError('Choose an active destination branch.')
        destination_branch = local_branch if is_restock else None

    if is_restock:
        source_id = source_branch_id or local_branch.pk
        source_branch = Branch.objects.filter(pk=source_id, is_active=True).first()
        if source_branch is None:
            raise StockTransferRequestError('Choose an active source branch.')
    else:
        if destination_branch_id:
            raise StockTransferRequestError('A write-off cannot have a destination branch.')
        source_branch = local_branch

    try:
        warehouse = Warehouse.objects.get(branch=source_branch)
    except Warehouse.DoesNotExist as exc:
        raise StockTransferRequestError(
            f'No warehouse is configured for {source_branch.branch_name}.'
        ) from exc

    if is_restock and source_branch.pk != local_branch.pk:
        local_products = []
        for product_id, _quantity in normalized:
            product = products[product_id]
            if warehouse_available_quantity(product, local_branch) > 0:
                local_products.append(product.product_name)
        if local_products:
            raise StockTransferRequestError(
                'Use the destination branch warehouse first for: '
                + ', '.join(local_products)
                + '. Request the remaining shortage from another branch after local stock is released.'
            )

    # Every line is checked before anything is saved, so one bad line never
    # leaves a partial request behind. This is only a submit-time guard — no
    # stock is reserved or moved here; approval and release re-check against
    # the then-current (locked) quantity.
    location = 'warehouse' if is_restock else 'floor'
    shortages = []
    for product_id, quantity in normalized:
        product = products[product_id]
        available = (
            warehouse_available_quantity(product, source_branch)
            if is_restock else
            (Inventory.objects.filter(
                product=product, branch=source_branch,
            ).values_list('quantity_on_hand', flat=True).first() or 0)
        )
        if available <= 0:
            shortages.append(
                f'No available {location} stock for {product.product_name}.'
            )
        elif quantity > available:
            shortages.append(
                f'Insufficient {location} stock for {product.product_name}. '
                f'Only {available} unit{"" if available == 1 else "s"} '
                f'{"is" if available == 1 else "are"} available.'
            )
    if shortages:
        raise StockTransferRequestError(' '.join(shortages))

    set_action_label(
        'Created a warehouse-to-floor transfer request'
        if is_restock else 'Created a stock write-off request'
    )
    with transaction.atomic():
        workflow_model = WarehouseFloorTransfer if is_restock else StockWriteOff
        transfer = workflow_model.objects.create(
            warehouse=warehouse,
            source_branch=source_branch,
            destination_branch=destination_branch,
            purpose=purpose,
            requested_by=actor,
            status=StockTransfer.Status.PENDING,
            transfer_date=transfer_date or timezone.localdate(),
            notes=(notes or '').strip(),
        )
        for product_id, quantity in normalized:
            StockTransferItem.objects.create(
                transfer=transfer,
                product_id=product_id,
                quantity_requested=quantity,
                quantity_approved=0,
            )
        record_transfer_event(
            transfer, '', StockTransfer.Status.PENDING, actor,
            f'Requested from {source_branch.branch_name}'
            + (f' for {destination_branch.branch_name}' if destination_branch else ''),
        )
        notifier = _notify_new_stock_request if is_restock else _notify_new_write_off_request
        transaction.on_commit(lambda pk=transfer.pk, fn=notifier: fn(pk))
    return transfer


def create_warehouse_floor_transfer(
    *, actor, source_branch_id=None, destination_branch_id=None,
    items, transfer_date=None, notes='',
):
    """Create a warehouse-to-floor request with transfer invariants."""
    return _create_stock_workflow(
        actor=actor,
        source_branch_id=source_branch_id,
        destination_branch_id=destination_branch_id,
        purpose=StockTransfer.Purpose.BRANCH_RESTOCK,
        items=items,
        transfer_date=transfer_date,
        notes=notes,
    )


def create_stock_write_off(
    *, actor, source_branch_id=None, purpose, items,
    transfer_date=None, notes='',
):
    """Create a floor-stock write-off with no receiving destination."""
    if purpose == StockTransfer.Purpose.BRANCH_RESTOCK:
        raise StockTransferRequestError('Choose a valid write-off reason.')
    return _create_stock_workflow(
        actor=actor,
        source_branch_id=source_branch_id,
        destination_branch_id=None,
        purpose=purpose,
        items=items,
        transfer_date=transfer_date,
        notes=notes,
    )


def create_stock_transfer(
    *, actor, source_branch_id=None, destination_branch_id=None, purpose,
    items, transfer_date=None, notes='',
):
    """Backward-compatible dispatcher for existing API/service callers."""
    if purpose == StockTransfer.Purpose.BRANCH_RESTOCK:
        return create_warehouse_floor_transfer(
            actor=actor,
            source_branch_id=source_branch_id,
            destination_branch_id=destination_branch_id,
            items=items,
            transfer_date=transfer_date,
            notes=notes,
        )
    if destination_branch_id:
        raise StockTransferRequestError('A write-off cannot have a destination branch.')
    return create_stock_write_off(
        actor=actor,
        source_branch_id=source_branch_id,
        purpose=purpose,
        items=items,
        transfer_date=transfer_date,
        notes=notes,
    )


def reconcile_transfer_receipt(*, transfer_id, actor, received_by_item, reason):
    """Reconcile a released restock per product and record one atomic event.

    The release already credited the destination floor. A short delivery
    removes only the missing units for that product; an over-delivery adds
    only its extra units. Aggregate-only correction is deliberately refused
    because it cannot say which product changed on a multi-item slip.
    """
    from core.scoping import user_can_access_branch

    reason = (reason or '').strip()
    if not reason:
        raise StockTransferReceiptError('A reason is required to flag a discrepancy.')

    try:
        normalized = {int(item_id): int(qty) for item_id, qty in received_by_item.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        raise StockTransferReceiptError('Enter a valid received quantity for every item.') from exc
    if any(qty < 0 for qty in normalized.values()):
        raise StockTransferReceiptError('Received quantities cannot be negative.')

    with transaction.atomic():
        transfer = StockTransfer.objects.select_for_update().select_related(
            'destination_branch',
        ).get(pk=transfer_id)
        if not user_can_access_branch(actor, transfer.destination_branch_id):
            raise StockTransferReceiptError(
                'That stock transfer belongs to another branch.'
            )
        if transfer.status != StockTransfer.Status.RELEASED:
            raise StockTransferReceiptError(
                'Only released withdrawals can be flagged.'
            )
        if not transfer.is_warehouse_floor_transfer:
            raise StockTransferReceiptError('Only warehouse-to-floor transfers can be received.')
        if transfer.destination_branch_id is None:
            raise StockTransferReceiptError('This transfer has no receiving branch.')

        items = list(transfer.items.select_related('product').order_by('pk'))
        expected_ids = {item.pk for item in items}
        if set(normalized) != expected_ids:
            raise StockTransferReceiptError(
                'Enter a received quantity for every item on the withdrawal.'
            )

        differences = {
            item.pk: normalized[item.pk] - item.quantity_released for item in items
        }
        if not any(differences.values()):
            raise StockTransferReceiptError(
                'All quantities match the release. Confirm receipt instead.'
            )

        # Lock and validate every affected aggregate before changing any row.
        inventories = {}
        for item in items:
            difference = differences[item.pk]
            if difference == 0:
                continue
            inventory, _ = Inventory.objects.select_for_update().get_or_create(
                product=item.product,
                branch=transfer.destination_branch,
                defaults={'quantity_on_hand': 0, 'reorder_level': 0},
            )
            if difference < 0 and inventory.quantity_on_hand < -difference:
                raise StockTransferReceiptError(
                    f'Cannot reconcile {item.product.product_name}: destination floor '
                    f'has {inventory.quantity_on_hand}, but {-difference} units must be removed.'
                )
            inventories[item.pk] = inventory

        for item in items:
            difference = differences[item.pk]
            if difference == 0:
                continue
            inventory = inventories[item.pk]
            before = inventory.quantity_on_hand
            inventory.quantity_on_hand += difference
            inventory.save()
            StockMovement.objects.create(
                inventory=inventory,
                location_type=StockMovement.LocationType.FLOOR,
                movement_type=StockMovement.MovementType.TRANSFER_DISCREPANCY,
                quantity_change=difference,
                quantity_before=before,
                quantity_after=inventory.quantity_on_hand,
                reference_id=transfer.pk,
                notes=f'Discrepancy on {transfer.slip_number}: {reason}',
                created_by=actor,
            )
            if difference < 0:
                Batch.consume_fefo(
                    item.product, transfer.destination_branch,
                    Batch.Location.FLOOR, -difference, include_expired=True,
                )
            else:
                Batch.objects.create(
                    product=item.product,
                    branch=transfer.destination_branch,
                    location=Batch.Location.FLOOR,
                    quantity=difference,
                    received_date=timezone.localdate(),
                    expiry_date=None,
                    notes=f'Discrepancy on {transfer.slip_number}: {reason}',
                )

        previous = transfer.status
        transfer.status = StockTransfer.Status.DISCREPANCY
        transfer.acknowledged_by = actor
        transfer.received_qty = sum(normalized.values())
        transfer.flag_reason = reason
        transfer.save()
        record_transfer_event(
            transfer, previous, transfer.status, actor,
            f'{reason} Received per item: '
            + ', '.join(
                f'{item.product.product_name}={normalized[item.pk]}' for item in items
            ),
        )
    return transfer


@dataclass(frozen=True)
class ExpirySweepResult:
    batch_count: int = 0
    unit_count: int = 0
    floor_batch_count: int = 0
    floor_unit_count: int = 0
    warehouse_batch_count: int = 0
    warehouse_unit_count: int = 0


def expire_due_batches(
    *, as_of_date=None, actor=None, batch_ids=None, locations=None,
):
    """Atomically remove expired lots from aggregate stock and audit each lot.

    A lot is expired on its ``expiry_date`` (not the day after). Both floor
    and warehouse stock are processed by default: warehouse stock is not
    directly sellable, but leaving it there would allow a later release to
    put expired goods back onto the floor.

    The positive-quantity filter makes the sweep idempotent. Row locks keep a
    concurrent sale, transfer, manual write-off, or second sweep from removing
    the same units twice. Any aggregate/batch mismatch aborts the whole sweep
    instead of fabricating a disposal quantity.
    """
    as_of_date = as_of_date or timezone.localdate()
    batch_ids = tuple(batch_ids) if batch_ids is not None else None
    locations = tuple(locations) if locations is not None else None
    counts = {
        'batch_count': 0,
        'unit_count': 0,
        'floor_batch_count': 0,
        'floor_unit_count': 0,
        'warehouse_batch_count': 0,
        'warehouse_unit_count': 0,
    }
    # One notification per (branch, location) once everything below has
    # actually committed — never per batch, or a bad day (several lots due
    # at once) would flood Alerts with one row per lot. Only populated, and
    # only ever notified on, the unattended sweep (actor is None): the
    # manual "Write off" button on Alerts Center's Expired tab already gives
    # the person doing it an on-screen confirmation, so a second, async
    # notification telling them what they just clicked would be noise.
    breakdown = {}

    with transaction.atomic():
        due = Batch.objects.filter(
            quantity__gt=0,
            expiry_date__isnull=False,
            expiry_date__lte=as_of_date,
        )
        if batch_ids is not None:
            due = due.filter(pk__in=batch_ids)
        if locations is not None:
            due = due.filter(location__in=locations)
        due = due.order_by(
            'branch_id', 'product_id', 'location', 'pk',
        ).values_list(
            'pk', 'batch_number', 'product_id', 'product__product_name',
            'branch_id', 'branch__branch_name', 'location',
        )

        # Match the lock order used by sales and transfers: aggregate row
        # first, then the specific batch. This avoids a transfer holding a
        # WarehouseStock lock while the sweep holds the Batch lock each needs
        # from the other side.
        for (
            batch_id, batch_number, product_id, product_name,
            branch_id, branch_name, location,
        ) in due:
            if location == Batch.Location.FLOOR:
                try:
                    aggregate = Inventory.objects.select_for_update().get(
                        product_id=product_id, branch_id=branch_id,
                    )
                except Inventory.DoesNotExist as exc:
                    raise ExpiryProcessingError(
                        f'Expired batch {batch_number} has no floor inventory row.'
                    ) from exc
                location_type = StockMovement.LocationType.FLOOR
                movement_kwargs = {'inventory': aggregate}
                batch_counter = 'floor_batch_count'
                unit_counter = 'floor_unit_count'
            else:
                try:
                    aggregate = WarehouseStock.objects.select_for_update().get(
                        product_id=product_id,
                        warehouse__branch_id=branch_id,
                    )
                except WarehouseStock.DoesNotExist as exc:
                    raise ExpiryProcessingError(
                        f'Expired batch {batch_number} has no warehouse stock row.'
                    ) from exc
                location_type = StockMovement.LocationType.WAREHOUSE
                movement_kwargs = {'warehouse_stock': aggregate}
                batch_counter = 'warehouse_batch_count'
                unit_counter = 'warehouse_unit_count'

            batch = Batch.objects.select_for_update().filter(
                pk=batch_id,
                quantity__gt=0,
                expiry_date__isnull=False,
                expiry_date__lte=as_of_date,
                product_id=product_id,
                branch_id=branch_id,
                location=location,
            ).first()
            if batch is None:
                # Another expiry sweep or a manual disposal won the race.
                continue

            quantity = batch.quantity
            before = aggregate.quantity_on_hand
            if before < quantity:
                raise ExpiryProcessingError(
                    f'Expired batch {batch.batch_number} contains {quantity} units '
                    f'but its aggregate stock row contains only {before}.'
                )

            aggregate.quantity_on_hand = before - quantity
            aggregate.save()
            batch.quantity = 0
            batch.save(update_fields=['quantity'])

            action = 'Automatic' if actor is None else 'Confirmed'
            lot = f' / lot {batch.external_lot_code}' if batch.external_lot_code else ''
            StockMovement.objects.create(
                **movement_kwargs,
                batch=batch,
                location_type=location_type,
                movement_type=StockMovement.MovementType.EXPIRY_WRITE_OFF,
                quantity_change=-quantity,
                quantity_before=before,
                quantity_after=aggregate.quantity_on_hand,
                reference_id=batch.pk,
                notes=(
                    f'{action} expiry write-off for {product_name} at '
                    f'{branch_name} ({location}), batch {batch.batch_number}{lot}; '
                    f'expiry date {batch.expiry_date:%Y-%m-%d}.'
                ),
                created_by=actor,
            )

            counts['batch_count'] += 1
            counts['unit_count'] += quantity
            counts[batch_counter] += 1
            counts[unit_counter] += quantity

            if actor is None:
                key = (branch_id, branch_name, location)
                entry = breakdown.setdefault(
                    key, {'batch_count': 0, 'unit_count': 0, 'products': []}
                )
                entry['batch_count'] += 1
                entry['unit_count'] += quantity
                entry['products'].append(product_name)

    if breakdown:
        _notify_expiry_write_off(breakdown)

    return ExpirySweepResult(**counts)


def _notify_expiry_write_off(breakdown):
    """One EVENT_EXPIRY_WRITE_OFF notification per (branch, location) group
    the unattended sweep just wrote off — see expire_due_batches' own
    comment for why this is grouped rather than per-batch, and why it never
    fires for the manual per-batch "Write off" action."""
    from django.urls import reverse

    from core.module_permissions import FLOOR_STOCK, WAREHOUSE_STOCK
    from core.notifications import EVENT_EXPIRY_WRITE_OFF, notify_event

    branches = Branch.objects.in_bulk({key[0] for key in breakdown})
    for (branch_id, branch_name, location), entry in breakdown.items():
        branch = branches.get(branch_id)
        if branch is None:
            continue
        names = ', '.join(dict.fromkeys(entry['products']))  # de-duplicated, order kept
        location_label = 'Floor Stock' if location == Batch.Location.FLOOR else 'Warehouse Stock'
        module = FLOOR_STOCK if location == Batch.Location.FLOOR else WAREHOUSE_STOCK
        notify_event(
            EVENT_EXPIRY_WRITE_OFF,
            f'{entry["batch_count"]} batch{"" if entry["batch_count"] == 1 else "es"} '
            f'written off at {branch_name} ({location_label})',
            f'{entry["unit_count"]} unit{"" if entry["unit_count"] == 1 else "s"} across '
            f'{entry["batch_count"]} batch{"" if entry["batch_count"] == 1 else "es"} removed '
            f'from {location_label} at {branch_name} for having reached their expiry date: '
            f'{names}.',
            branch=branch,
            action_url=f'{reverse("alerts_center")}#expired',
            action_label='Review Expiry',
            required_module=module,
        )


HEADER_ALIASES = {
    'barcode': {'barcode', 'ean', 'upc'},
    'branch': {'branch', 'branchname', 'store'},
    'quantity': {'openingqty', 'openingquantity', 'quantity', 'qty'},
    'lot': {'lotbatch', 'lot', 'batch', 'lotnumber', 'batchnumber'},
    'expiry': {'expirydate', 'expiry', 'expirationdate'},
    'received': {'receiveddate', 'date received', 'datereceived'},
}
MAX_STOCK_QUANTITY = 2_147_483_647


def _header_key(value):
    return ''.join(ch for ch in str(value or '').strip().lower() if ch.isalnum())


def _normalize_barcode(value):
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
            if number.is_finite():
                text = format(number, 'f')
                return text.split('.')[0] if number == number.to_integral_value() else text
        except InvalidOperation:
            pass
    text = str(value).strip()
    match = re.fullmatch(r'([+-]?\d+)\.0+', text)
    return match.group(1) if match else text


def _parse_positive_integer(value):
    if value is None or isinstance(value, bool):
        raise ValueError
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError
    if (
        not number.is_finite() or number != number.to_integral_value()
        or number <= 0 or number > MAX_STOCK_QUANTITY
    ):
        raise ValueError
    return int(number)


def _parse_sheet_date(value, label, errors):
    if value is None or str(value).strip() == '':
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        parsed = parse_date(text)
    except ValueError:
        parsed = None
    if parsed is None:
        errors.append(f'{label} must be a valid date in YYYY-MM-DD format.')
    return parsed


def parse_opening_stock_workbook(path, *, default_received_date=None):
    """Parse the active sheet without writing to the database."""
    import openpyxl

    default_received_date = default_received_date or timezone.localdate()
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    iterator = ws.iter_rows(values_only=True)
    raw_headers = next(iterator, None)
    if not raw_headers:
        raise OpeningStockValidationError('The spreadsheet has no header row.')

    headers = [str(value).strip() if value is not None else '' for value in raw_headers]
    normalized_headers = [_header_key(value) for value in headers]
    nonblank = [value for value in normalized_headers if value]
    if len(nonblank) != len(set(nonblank)):
        raise OpeningStockValidationError('Spreadsheet column headers must be unique.')

    indexes = {}
    for field, aliases in HEADER_ALIASES.items():
        normalized_aliases = {_header_key(alias) for alias in aliases}
        matches = [i for i, header in enumerate(normalized_headers) if header in normalized_aliases]
        if len(matches) > 1:
            raise OpeningStockValidationError(f'More than one column matches {field}.')
        if matches:
            indexes[field] = matches[0]

    missing = [field for field in ('barcode', 'branch', 'quantity') if field not in indexes]
    if missing:
        labels = {'barcode': 'Barcode', 'branch': 'Branch', 'quantity': 'Opening Qty'}
        raise OpeningStockValidationError(
            'Missing required column(s): ' + ', '.join(labels[field] for field in missing) + '.'
        )

    def cell(row, field):
        idx = indexes.get(field)
        return row[idx] if idx is not None and idx < len(row) else None

    rows = []
    for row_number, raw in enumerate(iterator, start=2):
        if not raw or all(value is None or str(value).strip() == '' for value in raw):
            continue
        errors = []
        barcode = _normalize_barcode(cell(raw, 'barcode'))
        branch_name = str(cell(raw, 'branch') or '').strip()
        lot_code = str(cell(raw, 'lot') or '').strip()
        if not barcode:
            errors.append('Barcode is required.')
        elif len(barcode) > 64:
            errors.append('Barcode cannot exceed 64 characters.')
        if not branch_name:
            errors.append('Branch is required.')
        elif len(branch_name) > 100:
            errors.append('Branch cannot exceed 100 characters.')
        if len(lot_code) > 100:
            errors.append('Lot/Batch cannot exceed 100 characters.')
        try:
            quantity = _parse_positive_integer(cell(raw, 'quantity'))
        except ValueError:
            quantity = None
            errors.append('Opening Qty must be a positive whole number.')
        expiry_date = _parse_sheet_date(cell(raw, 'expiry'), 'Expiry Date', errors)
        received_date = _parse_sheet_date(cell(raw, 'received'), 'Received Date', errors)
        if received_date is None and not any(e.startswith('Received Date') for e in errors):
            received_date = default_received_date
        if expiry_date and received_date and expiry_date < received_date:
            errors.append('Expiry Date cannot be earlier than Received Date.')

        normalized = {
            'barcode': barcode.lower(),
            'branch': branch_name.casefold(),
            'quantity': quantity,
            'lot': lot_code.casefold(),
            'expiry': expiry_date.isoformat() if expiry_date else '',
            'received': received_date.isoformat() if received_date else '',
        }
        row_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        rows.append({
            'row_number': row_number,
            'barcode': barcode,
            'branch_name': branch_name,
            'quantity': quantity,
            'lot_code': lot_code,
            'expiry_date': expiry_date,
            'received_date': received_date,
            'normalized': normalized,
            'row_hash': row_hash,
            'errors': errors,
            'warnings': [],
            'product': None,
            'branch': None,
        })
    return rows


def canonical_opening_fingerprint(rows):
    payload = {
        'schema': 1,
        'rows': sorted(
            (row['normalized'] for row in rows),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(',', ':')),
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def validate_opening_stock_rows(rows):
    """Resolve references and attach errors/warnings without changing stock."""
    product_map = {}
    for product in Product.objects.exclude(barcode__isnull=True).exclude(barcode=''):
        product_map.setdefault(product.barcode.casefold(), []).append(product)
    branch_map = {}
    for branch in Branch.objects.all():
        branch_map.setdefault(branch.branch_name.casefold(), []).append(branch)

    inventory_map = {
        (inv.product_id, inv.branch_id): inv
        for inv in Inventory.objects.select_related('product', 'branch')
    }
    floor_sums = {
        (item['product_id'], item['branch_id']): item['total']
        for item in Batch.objects.filter(location=Batch.Location.FLOOR)
        .values('product_id', 'branch_id').annotate(total=Sum('quantity'))
    }
    prior_pairs = set(
        OpeningStockImportRow.objects.values_list('product_id', 'branch_id')
    )

    seen_rows = {}
    seen_lots = {}
    today = timezone.localdate()
    for row in rows:
        products = product_map.get(row['barcode'].casefold(), []) if row['barcode'] else []
        if not products and row['barcode']:
            row['errors'].append('Barcode does not match an existing product.')
        elif len(products) > 1:
            row['errors'].append('Barcode matches more than one product.')
        elif products:
            row['product'] = products[0]

        branches = branch_map.get(row['branch_name'].casefold(), []) if row['branch_name'] else []
        if not branches and row['branch_name']:
            row['errors'].append('Branch does not exist.')
        elif len(branches) > 1:
            row['errors'].append('Branch name is ambiguous; branch names must be unique for import.')
        elif branches:
            row['branch'] = branches[0]

        if row['row_hash'] in seen_rows:
            row['errors'].append(
                f'Exact duplicate of spreadsheet row {seen_rows[row["row_hash"]]}.'
            )
        else:
            seen_rows[row['row_hash']] = row['row_number']

        lot_identity = (
            row['barcode'].casefold(), row['branch_name'].casefold(),
            row['lot_code'].casefold(), row['expiry_date'], row['received_date'],
        )
        if lot_identity in seen_lots:
            row['errors'].append(
                f'The same product/branch/lot is already present on row {seen_lots[lot_identity]}.'
            )
        else:
            seen_lots[lot_identity] = row['row_number']

        if row['product'] and row['branch']:
            pair = (row['product'].pk, row['branch'].pk)
            inv = inventory_map.get(pair)
            batch_total = floor_sums.get(pair)
            current = inv.quantity_on_hand if inv else 0
            row['current_quantity'] = current
            if batch_total is not None and batch_total != current:
                row['errors'].append(
                    f'Existing floor stock is out of reconciliation: aggregate {current}, batches {batch_total}.'
                )
            if current > 0:
                row['errors'].append(
                    f'Product already has {current} unit(s) of floor stock at this branch.'
                )
            if pair in prior_pairs:
                row['errors'].append('An opening-stock import already exists for this product and branch.')
        else:
            row['current_quantity'] = 0

        if row['expiry_date']:
            if row['expiry_date'] <= today:
                row['warnings'].append('Stock is expired or expires today and will be surfaced for review.')
            elif row['expiry_date'] <= today + timedelta(days=30):
                row['warnings'].append('Stock expires within 30 days.')
        else:
            row['warnings'].append('No expiry date was supplied.')
        if not row['lot_code']:
            row['warnings'].append('No external lot/batch code was supplied.')

        row['status'] = 'invalid' if row['errors'] else ('warning' if row['warnings'] else 'valid')

    # Project the final aggregate once all lots for a pair are included.
    planned = {}
    for row in rows:
        if row['product'] and row['branch'] and row['quantity'] and not row['errors']:
            pair = (row['product'].pk, row['branch'].pk)
            planned[pair] = planned.get(pair, 0) + row['quantity']
    for row in rows:
        if row['product'] and row['branch']:
            pair = (row['product'].pk, row['branch'].pk)
            if planned.get(pair, 0) > MAX_STOCK_QUANTITY:
                row['errors'].append(
                    'Combined opening quantity for this product and branch exceeds the supported limit.'
                )
                row['status'] = 'invalid'
            row['resulting_quantity'] = row.get('current_quantity', 0) + planned.get(pair, 0)
        else:
            row['resulting_quantity'] = None

    return rows


def opening_preview_summary(rows, canonical_sha256):
    usable = [row for row in rows if not row['errors']]
    return {
        'total': len(rows),
        'valid': sum(row['status'] == 'valid' for row in rows),
        'warning': sum(row['status'] == 'warning' for row in rows),
        'invalid': sum(row['status'] == 'invalid' for row in rows),
        'products': len({row['product'].pk for row in usable if row['product']}),
        'branches': len({row['branch'].pk for row in usable if row['branch']}),
        'quantity': sum(row['quantity'] or 0 for row in usable),
        'canonical_sha256': canonical_sha256,
        'duplicate_import': OpeningStockImport.objects.filter(
            committed_fingerprint=canonical_sha256
        ).first(),
    }


def credit_floor_stock(
    *, product, branch, quantity, actor, movement_type, notes,
    expiry_date=None, received_date=None, reference_id=None,
    external_lot_code='', opening_import_row=None,
):
    """Atomically credit aggregate floor stock, its lot, and its audit row."""
    with transaction.atomic():
        # Locking the product also serializes the no-Inventory-row case, where
        # select_for_update cannot lock a row that does not exist yet.
        product = Product.objects.select_for_update().get(pk=product.pk)
        inv, _ = Inventory.objects.select_for_update().get_or_create(
            product=product, branch=branch,
            defaults={'quantity_on_hand': 0, 'reorder_level': 0},
        )
        before = inv.quantity_on_hand
        inv.quantity_on_hand += quantity
        inv.save()
        import_note = f'Opening stock import #{opening_import_row.import_batch_id}' if opening_import_row else notes
        movement = StockMovement.objects.create(
            inventory=inv,
            location_type=StockMovement.LocationType.FLOOR,
            movement_type=movement_type,
            quantity_change=quantity,
            quantity_before=before,
            quantity_after=inv.quantity_on_hand,
            reference_id=reference_id,
            notes=import_note,
            created_by=actor,
            opening_import_row=opening_import_row,
        )
        batch = Batch.objects.create(
            product=product,
            branch=branch,
            location=Batch.Location.FLOOR,
            quantity=quantity,
            received_date=received_date or timezone.localdate(),
            expiry_date=expiry_date,
            external_lot_code=external_lot_code,
            notes=import_note,
            opening_import_row=opening_import_row,
        )
        return inv, batch, movement


def correct_floor_stock_count(
    *, product, branch, counted_quantity, reason, actor, expiry_date=None,
):
    """Set floor stock to a physical count and write one auditable movement.

    The aggregate row and its floor batches are corrected in the same locked
    transaction. Positive differences create a correction batch; negative
    differences consume existing batches FEFO, including expired batches
    because a physical count covers every unit actually present.
    """
    reason = (reason or '').strip()
    if not reason:
        raise CountCorrectionError('Enter a reason for the correction.')
    if counted_quantity < 0 or counted_quantity > MAX_STOCK_QUANTITY:
        raise CountCorrectionError('Enter a valid counted quantity.')

    with transaction.atomic():
        # This also serializes creation when no Inventory row exists yet.
        product = Product.objects.select_for_update().get(pk=product.pk)
        inventory, _ = Inventory.objects.select_for_update().get_or_create(
            product=product,
            branch=branch,
            defaults={'quantity_on_hand': 0, 'reorder_level': 0},
        )
        system_quantity = inventory.quantity_on_hand
        difference = counted_quantity - system_quantity

        if difference > 0:
            Batch.objects.create(
                product=product,
                branch=branch,
                location=Batch.Location.FLOOR,
                quantity=difference,
                received_date=timezone.localdate(),
                expiry_date=expiry_date,
                notes=f'Count correction: {reason}',
            )
        elif difference < 0:
            quantity_to_remove = abs(difference)
            consumed = Batch.consume_fefo(
                product,
                branch,
                Batch.Location.FLOOR,
                quantity_to_remove,
                include_expired=True,
            )
            if sum(quantity for _, quantity in consumed) != quantity_to_remove:
                raise CountCorrectionError(
                    'The floor-stock batches do not match the system quantity. '
                    'Reconcile the batch records before lowering this count.'
                )

        inventory.quantity_on_hand = counted_quantity
        inventory.save()
        movement = StockMovement.objects.create(
            inventory=inventory,
            location_type=StockMovement.LocationType.FLOOR,
            movement_type=StockMovement.MovementType.ADJUSTMENT,
            quantity_change=difference,
            quantity_before=system_quantity,
            quantity_after=counted_quantity,
            notes=(
                f'Count correction — system quantity: {system_quantity}; '
                f'counted quantity: {counted_quantity}; difference: {difference:+d}; '
                f'reason: {reason}'
            ),
            created_by=actor,
        )
        return inventory, movement


def commit_opening_stock_import(
    *, rows, filename, raw_sha256, canonical_sha256, as_of_date, actor,
    warnings_acknowledged=False,
):
    if any(row['errors'] for row in rows):
        raise OpeningStockValidationError('Resolve every invalid row before confirming the import.')
    if any(row['warnings'] for row in rows) and not warnings_acknowledged:
        raise OpeningStockValidationError('Acknowledge the warning rows before confirming.')
    if not rows:
        raise OpeningStockValidationError('The spreadsheet has no inventory rows.')

    with transaction.atomic():
        if OpeningStockImport.objects.select_for_update().filter(
            committed_fingerprint=canonical_sha256
        ).exists():
            raise OpeningStockDuplicateError('These normalized opening-stock rows were already imported.')

        # Recheck the zero-balance invariant while holding locks. Rows for the
        # same product/branch are legitimate separate lots and share one lock.
        pairs = sorted({(row['product'].pk, row['branch'].pk) for row in rows})
        for product_id, branch_id in pairs:
            Product.objects.select_for_update().get(pk=product_id)
            inv = Inventory.objects.select_for_update().filter(
                product_id=product_id, branch_id=branch_id
            ).first()
            if inv and inv.quantity_on_hand > 0:
                raise OpeningStockValidationError(
                    f'{inv.product.product_name} already has floor stock at {inv.branch.branch_name}; preview is stale.'
                )
            if OpeningStockImportRow.objects.filter(
                product_id=product_id, branch_id=branch_id
            ).exists():
                raise OpeningStockValidationError(
                    'An opening-stock import was committed for one of these product/branch pairs.'
                )

        import_batch = OpeningStockImport.objects.create(
            original_filename=filename[:255],
            raw_sha256=raw_sha256,
            canonical_sha256=canonical_sha256,
            committed_fingerprint=canonical_sha256,
            as_of_date=as_of_date,
            row_count=len(rows),
            product_count=len({row['product'].pk for row in rows}),
            branch_count=len({row['branch'].pk for row in rows}),
            total_quantity=sum(row['quantity'] for row in rows),
            warning_count=sum(bool(row['warnings']) for row in rows),
            warnings_acknowledged=warnings_acknowledged,
            uploaded_by=actor,
        )

        for data in sorted(rows, key=lambda row: (row['product'].pk, row['branch'].pk, row['row_number'])):
            # credit_floor_stock locks again inside this same transaction; the
            # lock is retained and the nested atomic block is only a savepoint.
            current = Inventory.objects.filter(
                product=data['product'], branch=data['branch']
            ).values_list('quantity_on_hand', flat=True).first() or 0
            import_row = OpeningStockImportRow.objects.create(
                import_batch=import_batch,
                row_number=data['row_number'],
                normalized_hash=data['row_hash'],
                product=data['product'],
                branch=data['branch'],
                product_name_snapshot=data['product'].product_name,
                barcode_snapshot=data['barcode'],
                branch_name_snapshot=data['branch'].branch_name,
                quantity=data['quantity'],
                external_lot_code=data['lot_code'],
                received_date=data['received_date'] or as_of_date,
                expiry_date=data['expiry_date'],
                previous_quantity=current,
                resulting_quantity=current + data['quantity'],
                warnings=data['warnings'],
            )
            credit_floor_stock(
                product=data['product'], branch=data['branch'], quantity=data['quantity'],
                actor=actor, movement_type=StockMovement.MovementType.OPENING_BALANCE,
                notes=f'Opening stock import #{import_batch.pk}',
                expiry_date=data['expiry_date'], received_date=data['received_date'] or as_of_date,
                reference_id=import_batch.pk, external_lot_code=data['lot_code'],
                opening_import_row=import_row,
            )
        return import_batch


def reverse_opening_stock_import(*, import_id, actor, reason):
    if not reason.strip():
        raise OpeningStockValidationError('A reversal reason is required.')
    with transaction.atomic():
        import_batch = OpeningStockImport.objects.select_for_update().get(pk=import_id)
        if import_batch.status == OpeningStockImport.Status.REVERSED:
            raise OpeningStockValidationError('This opening-stock import was already reversed.')

        rows = list(import_batch.rows.select_related('product', 'branch').order_by('row_number'))
        for row in rows:
            if not row.product or not row.branch:
                raise OpeningStockValidationError(
                    f'Row {row.row_number} no longer has a product/branch and cannot be reversed automatically.'
                )
            batches = list(row.batches.select_for_update().all())
            if not batches:
                raise OpeningStockValidationError(
                    f'Row {row.row_number} has no linked batch and cannot be reversed automatically.'
                )
            remaining = sum(batch.quantity for batch in batches)
            inv = Inventory.objects.select_for_update().filter(
                product=row.product, branch=row.branch
            ).first()
            if inv is None or inv.quantity_on_hand < remaining:
                raise OpeningStockValidationError(
                    f'Row {row.row_number} cannot be reversed because aggregate stock no longer covers its remaining batch.'
                )
            before = inv.quantity_on_hand
            if remaining:
                inv.quantity_on_hand -= remaining
                inv.save()
                for batch in batches:
                    batch.quantity = 0
                    batch.save(update_fields=['quantity'])
                StockMovement.objects.create(
                    inventory=inv,
                    location_type=StockMovement.LocationType.FLOOR,
                    movement_type=StockMovement.MovementType.OPENING_BALANCE_REVERSAL,
                    quantity_change=-remaining,
                    quantity_before=before,
                    quantity_after=inv.quantity_on_hand,
                    reference_id=import_batch.pk,
                    notes=f'Opening stock import #{import_batch.pk} reversal: {reason.strip()}',
                    created_by=actor,
                    opening_import_row=row,
                )
            row.reversed_quantity = remaining
            row.consumed_quantity = row.quantity - remaining
            row.save(update_fields=['reversed_quantity', 'consumed_quantity'])

        import_batch.status = OpeningStockImport.Status.REVERSED
        import_batch.reversed_by = actor
        import_batch.reversed_at = timezone.now()
        import_batch.reversal_reason = reason.strip()
        import_batch.save(update_fields=[
            'status', 'reversed_by', 'reversed_at', 'reversal_reason',
        ])
        return import_batch
