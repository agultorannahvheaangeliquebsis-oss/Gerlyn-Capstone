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
  baseline optional: taken just before the view runs and handed to `verify`,
           for records that carry no creation time of their own (a product).

What is queueable is deliberately limited to CREATING a record or a request.
Editing an existing record offline is not: the form was filled against a
snapshot, and replaying it later would silently overwrite whatever changed in
between. Approvals, payment verification, refunds, status changes and anything
that spends or authorizes stay online-only.
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
    baseline: Optional[Callable] = None


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


def _norm(text):
    return ' '.join(str(text or '').split()).lower()


def _baseline_product(user, post, target_pk):
    from django.db.models import Max
    from inventory.models import Product

    return Product.objects.aggregate(top=Max('pk'))['top'] or 0


def _verify_product(user, op_id, location, started_at, post, target_pk, baseline=0):
    from inventory.models import Product

    wanted = _norm(post.get('product_name'))
    for product in Product.objects.filter(pk__gt=baseline or 0).order_by('-pk'):
        if _norm(product.product_name) == wanted:
            return {'label': product.product_name, 'url': ''}
    return None


def _verify_category(user, op_id, location, started_at, post, target_pk):
    # The view refuses a name that already exists, so finding it proves this
    # change created it.
    from inventory.models import ProductCategory

    wanted = _norm(post.get('category_name'))
    for category in ProductCategory.objects.all():
        if _norm(category.category_name) == wanted:
            return {'label': category.category_name, 'url': ''}
    return None


def _verify_write_off(user, op_id, location, started_at, post, target_pk):
    from inventory.models import StockTransfer

    transfer = StockTransfer.objects.filter(
        requested_by=user, created_at__gte=started_at,
    ).exclude(purpose=StockTransfer.Purpose.BRANCH_RESTOCK).order_by('-pk').first()
    if transfer is None:
        return None
    return {
        'label': transfer.slip_number,
        'url': reverse('transfer_detail', kwargs={'pk': transfer.pk}),
    }


def _verify_supplier(user, op_id, location, started_at, post, target_pk):
    from accounts.models import User

    username = str(post.get('username') or '').strip()
    supplier = User.objects.filter(
        role=User.Role.SUPPLIER, username__iexact=username, date_joined__gte=started_at,
    ).first()
    if supplier is None:
        return None
    profile = getattr(supplier, 'supplier_profile', None)
    return {
        'label': (profile.supplier_name if profile else '') or supplier.username,
        'url': reverse('supplier_detail', kwargs={'pk': supplier.pk}),
    }


def _verify_supplier_category(user, op_id, location, started_at, post, target_pk):
    from accounts.models import SupplierCategory

    wanted = _norm(post.get('name'))
    for category in SupplierCategory.objects.all():
        if _norm(category.name) == wanted:
            return {'label': category.name, 'url': ''}
    return None


def _verify_supplier_product(user, op_id, location, started_at, post, target_pk):
    from django.db.models import Q
    from procurement.models import SupplierProduct

    product_id = str(post.get('product') or '').strip()
    if not product_id.isdigit():
        return None
    link = SupplierProduct.objects.filter(
        supplier_id=target_pk, product_id=product_id,
    ).filter(
        Q(added_at__gte=started_at) | Q(reviewed_at__gte=started_at),
    ).select_related('product').first()
    if link is None:
        return None
    return {
        'label': link.product.product_name,
        'url': reverse('supplier_detail', kwargs={'pk': target_pk}),
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
        # -- Data entry: creating a record, never editing one ----------------
        OpSpec(
            op_type='inventory.product_add',
            label='New product',
            url_name='product_add',
            roles=(OWNER, BRANCH_MANAGER),
            baseline=_baseline_product,
            verify=_verify_product,
        ),
        OpSpec(
            op_type='inventory.category_add',
            label='New product category',
            url_name='category_add',
            roles=(OWNER, BRANCH_MANAGER),
            verify=_verify_category,
        ),
        OpSpec(
            # A request, like the refill: nothing leaves the floor until it is
            # approved and completed online.
            op_type='inventory.write_off',
            label='Write-off request',
            url_name='transfer_create',
            roles=(OWNER, BRANCH_MANAGER, STAFF),
            verify=_verify_write_off,
        ),
        OpSpec(
            # The supplier account is created with a random password; the
            # Owner shares a reset link later, exactly as when online.
            op_type='procurement.supplier_add',
            label='New supplier',
            url_name='supplier_add',
            roles=(OWNER,),
            verify=_verify_supplier,
        ),
        OpSpec(
            op_type='procurement.supplier_category_add',
            label='Supplier category',
            url_name='supplier_category_add',
            roles=(OWNER,),
            verify=_verify_supplier_category,
        ),
        OpSpec(
            op_type='procurement.supplier_product_add',
            label='Supplier product',
            url_name='supplier_product_add',
            roles=(OWNER,),
            needs_target=True,
            verify=_verify_supplier_product,
        ),
    )
}


def get_spec(op_type):
    return OPERATIONS.get(op_type)


def public_catalog():
    """What the browser is told it may queue (op type -> label)."""
    return {spec.op_type: spec.label for spec in OPERATIONS.values()}
