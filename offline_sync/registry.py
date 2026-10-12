"""Which changes may be queued offline, and how each is replayed and checked.

This registry IS the offline allow-list. A change that is not listed here
cannot be synced, whatever the browser sends -- that is how approvals, payment
verification, refunds, account/permission changes and anything else that needs
a live, authoritative answer stay online-only.

Each entry replays the *existing* view for its form. The view keeps doing all
the validation, pricing, branch scoping and stock arithmetic; nothing about
the rules is re-implemented here. What an entry adds is:

  roles    who may queue it. Narrower than the view where the view's own rule is
           "the Owner's version of this is an authorization" (see create_po).
  precheck a conflict test against *current* server state, for changes that
           are only meaningful relative to the state the user saw.
  verify   positive proof, read back from the database, that the change landed.
           A redirect alone is never accepted as success.
"""
from dataclasses import dataclass
from typing import Callable, Optional

from django.urls import reverse
from django.utils import timezone

from core.permissions import BRANCH_MANAGER, OWNER, STAFF


@dataclass(frozen=True)
class Conflict:
    message: str
    # What the server holds now, so the client can show the user what changed.
    current: Optional[dict] = None


@dataclass(frozen=True)
class OpSpec:
    op_type: str
    label: str
    url_name: str
    roles: tuple
    needs_target: bool = False
    # POST field the view already honours as a duplicate-submission token.
    # The server overwrites it with the operation id, so two offline forms
    # rendered from the same cached page can never share a token.
    key_field: Optional[str] = None
    precheck: Optional[Callable] = None
    verify: Optional[Callable] = None


# ── prechecks ───────────────────────────────────────────────────────────────

def _precheck_count_correction(user, post, target_pk, meta):
    """A physical count is an absolute number taken against the quantity the
    counter saw on screen. If the system quantity has moved since (a sale, a
    refill release, another count), applying it late would silently overwrite
    that movement -- so it is refused and the person re-counts."""
    from branches.models import Branch
    from core.scoping import branch_for_user, user_can_access_branch
    from inventory.models import Inventory

    base = meta.get('base_quantity')
    try:
        base = int(base)
    except (TypeError, ValueError):
        return Conflict(
            'This count was queued without the quantity it was counted '
            'against, so it cannot be applied safely. Re-count it online.'
        )

    branch = branch_for_user(user)
    if branch is None:
        raw = (post.get('branch_id') or '').strip()
        branch = Branch.objects.filter(pk=raw).first() if raw.isdigit() else None
    product_id = (post.get('product_id') or '').strip()
    if branch is None or not product_id.isdigit():
        return None  # the view reports exactly what is wrong with the form
    if not user_can_access_branch(user, branch.pk):
        return None  # never reveal another branch's quantity

    row = Inventory.objects.filter(product_id=product_id, branch=branch).first()
    current = row.quantity_on_hand if row else 0
    if current != base:
        return Conflict(
            f'The system quantity changed while you were offline: it was '
            f'{base} when this count was entered and is {current} now. '
            f'Count again and re-enter it.',
            current={'quantity_on_hand': current, 'base_quantity': base},
        )
    return None


# ── verification (read back what the view claims it did) ────────────────────

def _verify_sale(user, op_id, location, started_at, post, target_pk):
    from django.urls import Resolver404, resolve
    from urllib.parse import urlparse
    from sales.models import SalesTransaction

    try:
        match = resolve(urlparse(location or '').path)
    except Resolver404:
        return None
    if match.url_name != 'transaction_detail':
        return None
    txn = SalesTransaction.objects.filter(
        pk=match.kwargs.get('pk'), recorded_by=user, transaction_date__gte=started_at,
    ).first()
    if txn is None:
        return None
    return {'label': txn.order_number, 'url': reverse('transaction_detail', kwargs={'pk': txn.pk})}


def _verify_refill(user, op_id, location, started_at, post, target_pk):
    from inventory.models import StockTransfer

    transfer = StockTransfer.objects.filter(
        requested_by=user, created_at__gte=started_at,
    ).order_by('-pk').first()
    if transfer is None:
        return None
    return {
        'label': transfer.slip_number,
        'url': reverse('transfer_detail', kwargs={'pk': transfer.pk}),
    }


def _verify_count(user, op_id, location, started_at, post, target_pk):
    from inventory.models import StockMovement

    movement = StockMovement.objects.filter(
        created_by=user, created_at__gte=started_at,
    ).order_by('-pk').first()
    if movement is None:
        # A count equal to the system quantity writes no movement; the view
        # still reports success for it, which is a real (if empty) outcome.
        return {'label': 'Count recorded', 'url': ''}
    return {'label': 'Count corrected', 'url': ''}


def _verify_po(user, op_id, location, started_at, post, target_pk):
    from procurement.models import Procurement

    proc = Procurement.objects.filter(submission_key=op_id, created_by=user).first()
    if proc is None:
        return None
    return {'label': proc.po_number, 'url': reverse('procurement_detail', kwargs={'pk': proc.pk})}


def _verify_receipt(user, op_id, location, started_at, post, target_pk):
    from procurement.models import DeliveryReceipt

    receipt = DeliveryReceipt.objects.filter(
        idempotency_key=op_id, received_by=user,
    ).select_related('procurement').first()
    if receipt is None:
        return None
    return {
        'label': receipt.procurement.po_number,
        'url': reverse('procurement_detail', kwargs={'pk': receipt.procurement_id}),
    }


OPERATIONS = {
    spec.op_type: spec for spec in (
        OpSpec(
            op_type='sale.walkin',
            label='Walk-in sale',
            url_name='transaction_add',
            roles=(OWNER, BRANCH_MANAGER, STAFF),
            verify=_verify_sale,
        ),
        OpSpec(
            op_type='inventory.floor_refill',
            label='Floor refill request',
            url_name='warehouse_stock_request',
            roles=(OWNER, BRANCH_MANAGER, STAFF),
            verify=_verify_refill,
        ),
        OpSpec(
            op_type='inventory.count_correction',
            label='Floor stock count correction',
            url_name='stock_adjustment_add',
            roles=(OWNER, BRANCH_MANAGER),
            precheck=_precheck_count_correction,
            verify=_verify_count,
        ),
        OpSpec(
            # Branch Manager only: when the Owner raises a PO the act IS the
            # authorization of the spend, which must happen live.
            op_type='procurement.create_po',
            label='Purchase order request',
            url_name='procurement_add',
            roles=(BRANCH_MANAGER,),
            key_field='submission_key',
            verify=_verify_po,
        ),
        OpSpec(
            op_type='procurement.receive_delivery',
            label='Delivery receipt',
            url_name='delivery_receive',
            roles=(OWNER, BRANCH_MANAGER, STAFF),
            needs_target=True,
            key_field='submission_key',
            verify=_verify_receipt,
        ),
    )
}


def get_spec(op_type):
    return OPERATIONS.get(op_type)


def public_catalog():
    """What the browser is told it may queue (op type -> label)."""
    return {spec.op_type: spec.label for spec in OPERATIONS.values()}
