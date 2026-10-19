"""Stock Count (cycle count) sessions — every rule lives here.

Views stay thin: they parse the POST, call one function below, and show its
message. Nothing in this module trusts a client-posted expected quantity,
variance or status; the server reads all of them back under a row lock.

Lifecycle
    Draft -> Counting -> Submitted -> Approved | Rejected | Cancelled
    (Submitted -> Counting again when an approver sends lines back for a
    recount.) One open session per (branch, location): StockCount.open_slot
    is unique while open and NULL once closed.

Snapshot
    start_stock_count() locks the location's stock rows and copies each
    product's quantity_on_hand into StockCountLine.expected_qty. Variance is
    always counted - expected_qty, never counted - "whatever it says now".

Who may approve (maker/checker)
    Only an Owner or Branch Manager, for a branch they can access, with EDIT
    on the Stock Counts module. Anyone who started, counted or submitted the
    session is a "maker" and may not approve, reject or send it back. The
    Owner is the one exception — nobody sits above the Owner, so an Owner may
    review their own count, the same reasoning as inventory.views.
    _try_auto_approve for an Owner's own stock request. Unlike that helper the
    Owner's count is not applied silently on submit: approval needs a reason
    per variance and the drift check below, so the Owner is taken straight to
    the review screen instead.

Drift rule (what approval applies)
    For each counted line the approver accepts:

        target on hand = on hand now + (counted - snapshot)

    i.e. the line's variance is applied on top of the current quantity, which
    equals counted + (stock movements recorded after the line was counted).
    A sale or transfer recorded AFTER a line was counted is therefore kept,
    not wiped out by an old count.

    Movements recorded BETWEEN the snapshot and the moment the line was
    counted are ambiguous: nobody can say whether the counter saw the shelf
    before or after them, so applying either way could be wrong by exactly
    that amount. A line whose in-count drift exceeds DRIFT_TOLERANCE (zero
    units) is therefore never applied. The approver leaves it out, or sends
    it back with recount_stock_count(), which re-snapshots it.

        in-count drift = on hand now - snapshot - (movements after counted_at)

    It is computed as a residual on purpose: a stock change that somehow
    skipped the StockMovement ledger also lands in it, and forces a recount
    instead of being silently overwritten.

    A line whose target would be negative is refused, never clamped.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.capture import record_detail
from audit.context import set_action_label
from branches.models import Branch
from core.limits import MAX_NOTE_LENGTH, MAX_QUANTITY, MAX_STOCK_COUNT_LINES
from core.module_permissions import (
    EDIT, FLOOR_STOCK, STOCK_COUNT, WAREHOUSE_STOCK, user_can_access_module,
    user_can_perform_module_action,
)
from core.scoping import user_can_access_branch

from .models import (
    Batch, Inventory, Product, StockCount, StockCountEvent, StockCountLine,
    StockCountScanLog, StockMovement, WarehouseStock,
)
from .services import CountCorrectionError, correct_stock_count

# Units of stock movement tolerated between a line's snapshot and its count
# before the line must be recounted. Zero: any in-count movement is
# ambiguous (see the module docstring), and raising this trades accuracy —
# an applied figure can then be off by up to this many units — for fewer
# recounts.
DRIFT_TOLERANCE = 0

INTERNAL_ROLES = ('OWNER', 'BRANCH_MANAGER', 'STAFF')
APPROVER_ROLES = ('OWNER', 'BRANCH_MANAGER')

Status = StockCount.Status
Decision = StockCountLine.Decision


class StockCountError(Exception):
    """A user-facing refusal; the message is ready to show."""


class StockCountStaleError(StockCountError):
    """The session changed state while the user was looking at it."""


class StockCountAlreadyApproved(StockCountError):
    """A repeat approve (double-click, resubmitted form): nothing to do."""


# ── who may do what ────────────────────────────────────────────────────────

def location_module(location_type):
    return FLOOR_STOCK if location_type == Batch.Location.FLOOR else WAREHOUSE_STOCK


def can_view_location(user, location_type):
    """Counting a location shows its products, so it needs View on that stock
    module too — a Stock Counts grant can only narrow access, never widen."""
    return user_can_access_module(user, location_module(location_type))


def can_access_count(user, count):
    return (
        getattr(user, 'role', None) in INTERNAL_ROLES
        and user_can_access_branch(user, count.branch_id)
        and can_view_location(user, count.location_type)
    )


def maker_ids(count):
    """Everyone who started, counted or submitted this session."""
    ids = {count.created_by_id, count.submitted_by_id}
    ids.update(
        count.lines.exclude(counted_by=None).values_list('counted_by_id', flat=True)
    )
    ids.update(
        count.scan_logs.exclude(scanned_by=None).values_list('scanned_by_id', flat=True)
    )
    ids.discard(None)
    return ids


def approval_block_reason(user, count):
    """'' when `user` may approve/reject/send back `count`, else why not."""
    role = getattr(user, 'role', None)
    if role not in APPROVER_ROLES:
        return 'Only the Owner or a Branch Manager can review a stock count.'
    if not can_access_count(user, count):
        return 'You cannot review counts for this branch or location.'
    if not user_can_perform_module_action(user, STOCK_COUNT, EDIT):
        return 'Your account does not have Edit permission for Stock Counts.'
    if role != 'OWNER' and user.pk in maker_ids(count):
        return (
            'You took part in this count, so someone else must review it '
            '(the Owner or another Branch Manager).'
        )
    return ''


# ── helpers ────────────────────────────────────────────────────────────────

def _event(count, from_status, to_status, actor, note=''):
    StockCountEvent.objects.create(
        count=count, from_status=from_status or '', to_status=to_status,
        actor=actor, note=(note or '').strip()[:MAX_NOTE_LENGTH],
    )


def _lock_count(count_id):
    try:
        return StockCount.objects.select_for_update().select_related('branch').get(pk=count_id)
    except StockCount.DoesNotExist as exc:
        raise StockCountError('That stock count no longer exists.') from exc


def _check_expected(count, expected_status):
    if expected_status is not None and expected_status != count.status:
        raise StockCountStaleError(
            f'{count.count_number} is now {count.get_status_display()} — it '
            'changed while you were looking at it. Review it again.'
        )


def _stock_rows(branch, location_type, product_ids=None, *, lock=False):
    """{product_id: Inventory|WarehouseStock} for one branch and location."""
    if location_type == Batch.Location.FLOOR:
        qs = Inventory.objects.filter(branch=branch)
    else:
        qs = WarehouseStock.objects.filter(warehouse__branch=branch)
    if product_ids is not None:
        qs = qs.filter(product_id__in=list(product_ids))
    if lock:
        qs = qs.select_for_update()
    return {row.product_id: row for row in qs.order_by('pk')}


def _lock_products(product_ids):
    # Same order as correct_stock_count(): Product first, then the stock row.
    list(Product.objects.select_for_update().filter(pk__in=list(product_ids)).order_by('pk'))


def _close(count, to_status, actor, note):
    from_status = count.status
    count.status = to_status
    count.open_slot = None
    count.closed_at = timezone.now()
    count.save()
    _event(count, from_status, to_status, actor, note)


# ── start ──────────────────────────────────────────────────────────────────

def start_stock_count(*, branch, location_type, actor, category=None, notes=''):
    if location_type not in (Batch.Location.FLOOR, Batch.Location.WAREHOUSE):
        raise StockCountError('Choose Floor or Warehouse stock to count.')
    set_action_label('Started a stock count')
    slot = StockCount.slot_for(branch.pk, location_type)
    try:
        with transaction.atomic():
            # Serializes two people pressing Start for the same branch.
            Branch.objects.select_for_update().get(pk=branch.pk)
            existing = StockCount.objects.filter(open_slot=slot).first()
            if existing:
                raise StockCountError(
                    f'{existing.count_number} is still open for '
                    f'{branch.branch_name} {location_type} stock. Finish or '
                    'cancel it before starting another.'
                )
            # Locking the rows makes the snapshot one consistent moment: a
            # sale waiting on one of these rows records its movement after
            # snapshot_at, so it is correctly seen as drift later.
            rows = _stock_rows(branch, location_type, lock=True)
            products = Product.objects.filter(pk__in=list(rows))
            if category is not None:
                products = products.filter(category=category)
            products = list(products.order_by('product_name', 'pk'))
            if not products:
                raise StockCountError(
                    f'{branch.branch_name} has no {location_type.lower()} stock '
                    f'{"in that category " if category else ""}to count.'
                )
            if len(products) > MAX_STOCK_COUNT_LINES:
                raise StockCountError(
                    f'That is {len(products)} products; one count can hold at '
                    f'most {MAX_STOCK_COUNT_LINES}. Pick a category to count '
                    'a smaller section at a time.'
                )
            count = StockCount.objects.create(
                branch=branch, location_type=location_type, category=category,
                open_slot=slot, notes=(notes or '').strip(), created_by=actor,
            )
            now = timezone.now()
            StockCountLine.objects.bulk_create([
                StockCountLine(
                    count=count, product=product,
                    expected_qty=rows[product.pk].quantity_on_hand,
                    snapshot_at=now,
                )
                for product in products
            ])
            _event(count, '', Status.DRAFT, actor, f'{len(products)} items snapshotted.')
            return count
    except IntegrityError as exc:
        # The unique open_slot caught a race the lock above did not.
        raise StockCountError(
            f'Another count is already open for {branch.branch_name} '
            f'{location_type} stock.'
        ) from exc


# ── counting ───────────────────────────────────────────────────────────────

def _require_countable(count):
    if not count.is_countable:
        raise StockCountStaleError(
            f'{count.count_number} is {count.get_status_display()} — counts '
            'can no longer be changed.'
        )


def _mark_counting(count, actor):
    if count.status == Status.DRAFT:
        count.status = Status.COUNTING
        count.save(update_fields=['status', 'updated_at'])
        _event(count, Status.DRAFT, Status.COUNTING, actor)


def save_counts(*, count_id, actor, changes):
    """Write the counted quantities the user actually changed.

    `changes` is {line_id: (original, new)} — the value the form was
    rendered with and the value posted. Only lines whose posted value differs
    from what that form showed are written, so two phones counting one
    session never overwrite each other's untouched lines with stale numbers.
    Returns how many lines changed.
    """
    set_action_label('Saved stock count progress')
    with transaction.atomic():
        count = _lock_count(count_id)
        _require_countable(count)
        lines = {
            line.pk: line
            for line in count.lines.select_for_update().filter(pk__in=list(changes))
        }
        now = timezone.now()
        changed = 0
        for line_id, (original, new) in changes.items():
            line = lines.get(line_id)
            if line is None or original == new or line.counted_qty == new:
                continue
            if new is not None and not 0 <= new <= MAX_QUANTITY:
                raise StockCountError(f'Counted quantity cannot be more than {MAX_QUANTITY:,}.')
            line.counted_qty = new
            line.counted_by = actor if new is not None else None
            line.counted_at = now if new is not None else None
            line.save(update_fields=['counted_qty', 'counted_by', 'counted_at'])
            changed += 1
        if changed:
            _mark_counting(count, actor)
        return changed


def resolve_code(code):
    """Barcode (case-insensitive) first, then SKU — the same order as
    inventory.views.barcode_lookup and procurement.views.delivery_scan."""
    return (
        Product.objects.filter(barcode__iexact=code).first()
        or Product.objects.filter(sku_code__iexact=code).first()
    )


def record_scan(*, count, actor, code):
    """Resolve one scanned code against the session and log it.

    Never changes a count: the counting screen adds 1 to the matched line in
    the form, and only a save writes it. Every scan is logged, mis-scans
    included, mirroring procurement.DeliveryScanLog.
    """
    product = resolve_code(code)
    line = count.lines.filter(product=product).first() if product else None
    StockCountScanLog.objects.create(
        count=count, line=line, scanned_code=code[:64], matched_product=product,
        was_unexpected=line is None, scanned_by=actor,
    )
    if line:
        return {
            'status': 'matched', 'line_id': line.pk,
            'product_name': product.product_name,
        }
    if product:
        return {
            'status': 'not_in_count', 'product_id': product.pk,
            'product_name': product.product_name, 'sku_code': product.sku_code,
        }
    return {'status': 'unknown', 'code': code}


def add_line(*, count_id, actor, product):
    """Add a product found on the shelf that the start snapshot did not
    include (no stock row at this location, or outside the category filter).
    Its expected quantity is snapshotted now, under the stock row's lock.
    Returns (line, created)."""
    set_action_label('Added an item to a stock count')
    with transaction.atomic():
        count = _lock_count(count_id)
        _require_countable(count)
        existing = count.lines.filter(product=product, batch=None).first()
        if existing:
            return existing, False
        if count.lines.count() >= MAX_STOCK_COUNT_LINES:
            raise StockCountError(
                f'A count can hold at most {MAX_STOCK_COUNT_LINES} items. '
                'Start another count for the rest.'
            )
        _lock_products([product.pk])
        row = _stock_rows(count.branch, count.location_type, [product.pk], lock=True).get(product.pk)
        line = StockCountLine.objects.create(
            count=count, product=product,
            expected_qty=row.quantity_on_hand if row else 0,
            snapshot_at=timezone.now(), is_unexpected=True,
        )
        _mark_counting(count, actor)
        return line, True


# ── submit ─────────────────────────────────────────────────────────────────

def submit_stock_count(*, count_id, actor, uncounted_policy):
    """Hand the session to an approver. Uncounted lines are an explicit
    choice: LEAVE (the default) never touches them; ZERO means "we looked
    and there were none", and is applied as a count of 0 at approval."""
    from core.notifications import EVENT_STOCK_COUNT_SUBMITTED, notify_event

    if uncounted_policy not in StockCount.UncountedPolicy.values:
        raise StockCountError('Choose what to do with items that were not counted.')
    set_action_label('Submitted a stock count for approval')
    with transaction.atomic():
        count = _lock_count(count_id)
        _require_countable(count)
        total = count.lines.count()
        counted = count.lines.exclude(counted_qty=None).count()
        if counted == 0:
            raise StockCountError('Count at least one item before submitting.')
        from_status = count.status
        count.status = Status.SUBMITTED
        count.uncounted_policy = uncounted_policy
        count.submitted_by = actor
        count.submitted_at = timezone.now()
        count.save()
        uncounted = total - counted
        note = f'{counted} of {total} items counted.'
        if uncounted:
            note += (
                f' {uncounted} uncounted — '
                f'{count.get_uncounted_policy_display().lower()}.'
            )
        _event(count, from_status, Status.SUBMITTED, actor, note)
        notify_event(
            EVENT_STOCK_COUNT_SUBMITTED,
            f'Stock count {count.count_number} awaiting approval',
            f'{count.branch.branch_name} {count.location_type.lower()} count: {note}',
            branch=count.branch, actor=actor,
            action_url=reverse('stock_count_review', args=[count.pk]),
            action_label='Review Count',
            reference_type='inventory.StockCount', reference_id=count.pk,
        )
        return count


# ── assessment (shared by the review screen and approval) ─────────────────

@dataclass
class LineAssessment:
    line: StockCountLine
    snapshot: int
    counted: int | None
    zeroed_by_policy: bool
    on_hand: int
    after_count: int
    in_count_drift: int
    variance: int | None
    target: int | None
    state: str        # uncounted | match | variance | recount | negative
    unit_cost: Decimal
    value_impact: Decimal

    @property
    def needs_reason(self):
        return self.state == 'variance'


def assess_lines(count, *, lock=False):
    """One LineAssessment per line, computed from the database only.

    With lock=True (approval), the products and stock rows are locked first,
    so the numbers cannot move between this check and the apply. The review
    screen calls it unlocked to show the same figures the approver will get.
    """
    lines = list(
        count.lines.select_related('product').prefetch_related('product__price_tiers')
        .order_by('product__product_name', 'pk')
    )
    product_ids = [line.product_id for line in lines]
    if lock:
        _lock_products(product_ids)
    rows = _stock_rows(count.branch, count.location_type, product_ids, lock=lock)

    zero_policy = (
        count.uncounted_policy == StockCount.UncountedPolicy.ZERO
        and count.submitted_at is not None
    )

    def effective(line):
        if line.counted_qty is not None:
            return line.counted_qty, line.counted_at, False
        if zero_policy:
            return 0, count.submitted_at, True
        return None, None, False

    # Movements recorded after each line was counted, from one query.
    counted_times = [effective(line)[1] for line in lines if effective(line)[1]]
    after = defaultdict(list)
    if counted_times and rows:
        fk = 'inventory_id' if count.location_type == Batch.Location.FLOOR else 'warehouse_stock_id'
        movements = StockMovement.objects.filter(
            **{f'{fk}__in': [row.pk for row in rows.values()]},
            created_at__gt=min(counted_times),
        ).values_list(fk, 'created_at', 'quantity_change')
        for row_id, created_at, change in movements:
            after[row_id].append((created_at, change))

    result = []
    for line in lines:
        counted, counted_at, zeroed = effective(line)
        row = rows.get(line.product_id)
        on_hand = row.quantity_on_hand if row else 0
        unit_cost = line.product.bulk_floor_price
        after_count = 0
        if counted_at is not None and row is not None:
            after_count = sum(c for at, c in after[row.pk] if at > counted_at)
        drift = on_hand - line.expected_qty - after_count
        variance = target = None
        if counted is None:
            state = 'uncounted'
        else:
            variance = counted - line.expected_qty
            target = on_hand + variance
            if abs(drift) > DRIFT_TOLERANCE:
                state = 'recount'
            elif variance == 0:
                state = 'match'
            elif target < 0:
                state = 'negative'
            else:
                state = 'variance'
        result.append(LineAssessment(
            line=line, snapshot=line.expected_qty, counted=counted,
            zeroed_by_policy=zeroed, on_hand=on_hand, after_count=after_count,
            in_count_drift=drift, variance=variance, target=target, state=state,
            unit_cost=unit_cost,
            value_impact=(unit_cost * variance) if variance else Decimal('0.00'),
        ))
    return result


# ── approve / reject / recount / cancel ───────────────────────────────────

def approve_stock_count(*, count_id, actor, expected_status, accepted, reasons,
                        default_reason=''):
    """Apply the accepted variances through correct_stock_count().

    `accepted` is the set of line ids the approver ticked; `reasons` maps a
    line id to its typed reason, and `default_reason` fills any left blank.
    All-or-nothing: if any accepted line is refused (in-count drift, would go
    negative, no reason), nothing is applied and every problem is reported.
    A repeat call on an already-approved session raises
    StockCountAlreadyApproved and changes nothing.
    """
    set_action_label('Approved a stock count and applied its variances')
    with transaction.atomic():
        count = _lock_count(count_id)
        if count.status == Status.APPROVED:
            raise StockCountAlreadyApproved(
                f'{count.count_number} was already approved. Nothing was applied twice.'
            )
        _check_expected(count, expected_status)
        if count.status != Status.SUBMITTED:
            raise StockCountStaleError(
                f'{count.count_number} is {count.get_status_display()}, not '
                'waiting for approval.'
            )
        blocked = approval_block_reason(actor, count)
        if blocked:
            raise StockCountError(blocked)

        default_reason = (default_reason or '').strip()
        assessed = assess_lines(count, lock=True)
        problems, to_apply = [], []
        for a in assessed:
            name = a.line.product.product_name
            take = a.line.pk in accepted
            if a.state == 'recount' and take:
                problems.append(
                    f'{name}: stock moved by {a.in_count_drift:+d} while it was '
                    'being counted. Untick it or send it back for a recount.'
                )
            elif a.state == 'negative' and take:
                problems.append(
                    f'{name}: applying {a.variance:+d} to the {a.on_hand} on hand '
                    'would go below zero. Untick it or send it back for a recount.'
                )
            elif a.state == 'variance' and take:
                reason = (reasons.get(a.line.pk) or '').strip() or default_reason
                if not reason:
                    problems.append(f'{name}: enter a reason for the {a.variance:+d} variance.')
                elif len(reason) > MAX_NOTE_LENGTH:
                    problems.append(f'{name}: the reason is longer than {MAX_NOTE_LENGTH} characters.')
                else:
                    to_apply.append((a, reason))
        if problems:
            raise StockCountError(' '.join(problems))

        applying = {a.line.pk for a, _ in to_apply}
        for a in assessed:
            line = a.line
            line.system_qty_at_approval = a.on_hand
            if line.pk in applying:
                continue
            if a.state == 'uncounted':
                line.decision = Decision.UNCOUNTED
            elif a.state == 'match':
                line.decision = Decision.NO_CHANGE
                line.applied_change = 0
            else:
                line.decision = Decision.SKIPPED
            line.save(update_fields=['decision', 'system_qty_at_approval', 'applied_change'])

        applied = []
        for a, reason in to_apply:
            line = a.line
            notes = (
                f'Stock count {count.count_number}: snapshot {a.snapshot}, counted '
                f'{a.counted}{" (uncounted, treated as zero)" if a.zeroed_by_policy else ""}'
                f' — variance {a.variance:+d}; on hand at approval {a.on_hand} → '
                f'{a.target}. Reason: {reason}'
            )
            try:
                _stock, movement = correct_stock_count(
                    location=count.location_type, product=line.product,
                    branch=count.branch, counted_quantity=a.target, reason=reason,
                    actor=actor, movement_type=StockMovement.MovementType.STOCK_COUNT,
                    reference_id=count.pk, notes=notes,
                )
            except CountCorrectionError as exc:
                raise StockCountError(str(exc)) from exc
            line.decision = Decision.APPLIED
            line.applied_change = a.variance
            line.reason = reason
            line.movement = movement
            line.save(update_fields=[
                'decision', 'applied_change', 'reason', 'movement',
                'system_qty_at_approval',
            ])
            applied.append(f'{line.product.product_name} {a.variance:+d}')

        count.approved_by = actor
        count.approved_at = timezone.now()
        note = (
            f'Applied {len(applied)} variance{"s" if len(applied) != 1 else ""}'
            + (f': {", ".join(applied)}.' if applied else '.')
        )
        _close(count, Status.APPROVED, actor, note)
        record_detail(count, note, after={'applied': applied})
        return count, len(applied)


def reject_stock_count(*, count_id, actor, expected_status, reason):
    reason = (reason or '').strip()
    if not reason:
        raise StockCountError('Enter a reason for rejecting this count.')
    if len(reason) > MAX_NOTE_LENGTH:
        raise StockCountError(f'The reason cannot be longer than {MAX_NOTE_LENGTH} characters.')
    set_action_label('Rejected a stock count')
    with transaction.atomic():
        count = _lock_count(count_id)
        _check_expected(count, expected_status)
        if count.status != Status.SUBMITTED:
            raise StockCountStaleError(f'{count.count_number} is not waiting for approval.')
        blocked = approval_block_reason(actor, count)
        if blocked:
            raise StockCountError(blocked)
        _close(count, Status.REJECTED, actor, reason)
        return count


def recount_stock_count(*, count_id, actor, expected_status, line_ids, note=''):
    """Send lines back to the counters. Each one is re-snapshotted to the
    quantity on hand now and its count cleared, so the next count is
    measured against a fresh, unambiguous baseline."""
    set_action_label('Sent a stock count back for recount')
    with transaction.atomic():
        count = _lock_count(count_id)
        _check_expected(count, expected_status)
        if count.status != Status.SUBMITTED:
            raise StockCountStaleError(f'{count.count_number} is not waiting for approval.')
        blocked = approval_block_reason(actor, count)
        if blocked:
            raise StockCountError(blocked)
        lines = list(count.lines.select_for_update().filter(pk__in=list(line_ids)))
        if not lines:
            raise StockCountError('Tick at least one item to send back for a recount.')
        _lock_products([line.product_id for line in lines])
        rows = _stock_rows(
            count.branch, count.location_type, [line.product_id for line in lines], lock=True,
        )
        now = timezone.now()
        for line in lines:
            row = rows.get(line.product_id)
            line.expected_qty = row.quantity_on_hand if row else 0
            line.snapshot_at = now
            line.counted_qty = line.counted_by = line.counted_at = None
            line.save(update_fields=[
                'expected_qty', 'snapshot_at', 'counted_qty', 'counted_by', 'counted_at',
            ])
        count.status = Status.COUNTING
        count.submitted_by = None
        count.submitted_at = None
        count.save()
        names = ', '.join(line.product.product_name for line in lines[:10])
        more = f' and {len(lines) - 10} more' if len(lines) > 10 else ''
        message = f'Recount {len(lines)} item{"s" if len(lines) != 1 else ""}: {names}{more}.'
        if note:
            message += f' {note.strip()}'
        _event(count, Status.SUBMITTED, Status.COUNTING, actor, message)
        return count, len(lines)


def cancel_stock_count(*, count_id, actor, reason='', expected_status=None):
    """Close an open session with no stock change. The person who started it
    may cancel it; otherwise an Owner/Branch Manager of that branch."""
    reason = (reason or '').strip()
    if len(reason) > MAX_NOTE_LENGTH:
        raise StockCountError(f'The reason cannot be longer than {MAX_NOTE_LENGTH} characters.')
    set_action_label('Cancelled a stock count')
    with transaction.atomic():
        count = _lock_count(count_id)
        _check_expected(count, expected_status)
        if not count.is_open:
            raise StockCountStaleError(
                f'{count.count_number} is already {count.get_status_display()}.'
            )
        if count.created_by_id != actor.pk and actor.role not in APPROVER_ROLES:
            raise StockCountError(
                'Only the person who started this count, the Owner or a '
                'Branch Manager can cancel it.'
            )
        _close(count, Status.CANCELLED, actor, reason)
        return count
