"""
core/notifications.py — fan-out helper for the Owner Notification Center.

Every internal-facing event that should surface to the back office goes
through notify() rather than each app creating accounts.models.Notification
rows by hand — one place to get recipients and required fields right.

Recipients, by role:
  Owner           — always, every trigger, every branch (they oversee all of
                    them, same as core.scoping.branch_ids_for_user's None).
  Branch Manager,
  Staff           — `branch`'s Manager and Staff, but ONLY when the call
                    site passes `required_module`, and only the ones who
                    actually have that module enabled
                    (core.module_permissions.user_can_access_module — which
                    already treats Manager and Staff identically; "Only
                    STAFF and BRANCH_MANAGER are subject to this [gating]").
                    Triggers with no `required_module` (role changes, login
                    lockouts, new staff accounts) are Administration-tier
                    and reach the Owner ONLY — not even the branch's own
                    Manager — matching module_permissions.py's own "Branch
                    Management and User Management... stay Owner-only, full
                    stop." A Manager's default module grant is every module
                    (core.module_permissions.default_allowed_modules), so in
                    practice an unrestricted Manager still sees everything
                    module-tied about their branch; this only bites once the
                    Owner has actually narrowed a specific Manager's access.

Deferred imports throughout (matching core/context_processors.py's existing
pattern): core is imported early via INSTALLED_APPS/context processors, so
importing accounts.models or branches.models at module load time risks
running before those apps are ready.
"""
from decimal import Decimal

from core.module_permissions import (
    ALERTS, BILLING, DELIVERIES, FLOOR_STOCK, PROCUREMENT, WAREHOUSE_STOCK,
)

CATEGORY_STAFF = 'staff'
CATEGORY_PROCUREMENT = 'procurement'
CATEGORY_SALES = 'sales'
CATEGORY_SYSTEM = 'system'

PRIORITY_CRITICAL = 'critical'
PRIORITY_HIGH = 'high'
PRIORITY_MEDIUM = 'medium'
PRIORITY_LOW = 'low'

# Stable event names.  These mirror Notification.EventType without importing
# the accounts model while Django is still loading applications.
EVENT_LOW_STOCK = 'low_stock'
EVENT_NEAR_EXPIRY = 'near_expiry'
EVENT_PO_APPROVED = 'po_approved'
EVENT_INCOMING_DELIVERY = 'incoming_delivery'
EVENT_PO_TERMS_FOR_BUYER = 'po_terms_for_buyer'
EVENT_PO_TERMS_FOR_SUPPLIER = 'po_terms_for_supplier'
EVENT_SUPPLIER_OFFER_FOR_REVIEW = 'supplier_offer_for_review'
EVENT_SUPPLIER_OFFER_DECISION = 'supplier_offer_decision'
EVENT_PO_OVERDUE = 'po_overdue'
EVENT_PO_OVERDUE_ESCALATED = 'po_overdue_escalated'
EVENT_SUPPLIER_RETURN_FOR_SUPPLIER = 'supplier_return_for_supplier'
EVENT_SUPPLIER_RETURN_RESPONSE = 'supplier_return_response'
EVENT_ORDER_DELIVERED = 'order_delivered'
EVENT_PAYMENT_VERIFIED = 'payment_verified'
EVENT_PAYMENT_REJECTED = 'payment_rejected'
EVENT_EXPIRY_WRITE_OFF = 'expiry_write_off'

# Executable notification matrix.  ``internal`` means all active Owners plus
# the branch's active Manager/Staff accounts that can access required_module.
# Relational audiences are supplied by the call site (for example, the
# Procurement's creator/supplier or a SalesTransaction's customer).
#
# ``when`` and ``recipients`` are intentionally kept here with the routing
# data.  This makes the policy reviewable without reconstructing it from a
# collection of views and signals; notify_event() consumes the other keys.
EVENT_MATRIX = {
    EVENT_LOW_STOCK: {
        'when': 'Floor or warehouse stock crosses from above to at/below its reorder level.',
        'recipients': 'Owner; branch Manager/Staff with the affected Floor/Warehouse Stock access.',
        'audiences': ('internal',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_MEDIUM,
        'priority_note': 'Medium at/below reorder level; Critical at zero units.',
        'required_module': FLOOR_STOCK,
        'allowed_modules': (FLOOR_STOCK, WAREHOUSE_STOCK),
        'exclude_actor': False,
    },
    EVENT_NEAR_EXPIRY: {
        'when': 'A positive-quantity batch first enters the next 30 days before expiry.',
        'recipients': 'Owner; branch Manager/Staff with Alerts access.',
        'audiences': ('internal',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_HIGH,
        'required_module': ALERTS,
        'exclude_actor': False,
    },
    EVENT_PO_APPROVED: {
        'when': 'The Owner authorizes a Draft purchase order into Approved.',
        'recipients': 'The PO requester and supplier; never duplicate the approving actor.',
        'audiences': ('requester', 'supplier'),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_MEDIUM,
        'required_module': None,
        'exclude_actor': True,
    },
    EVENT_INCOMING_DELIVERY: {
        'when': 'The supplier confirms or changes the expected delivery date.',
        'recipients': 'Owner; branch Manager/Staff with Deliveries access.',
        'audiences': ('internal',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_MEDIUM,
        'required_module': DELIVERIES,
        'exclude_actor': True,
    },
    EVENT_PO_TERMS_FOR_BUYER: {
        'when': 'The supplier submits a price/delivery-date offer or counter-offer '
                'on an Approved purchase order — it is now the buyer\'s turn.',
        'recipients': 'Owner; branch Manager/Staff with Procurement access.',
        'audiences': ('internal',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_MEDIUM,
        'required_module': PROCUREMENT,
        'exclude_actor': True,
    },
    EVENT_PO_TERMS_FOR_SUPPLIER: {
        'when': 'The Owner/Manager counters or accepts the supplier\'s offer — '
                'it is now the supplier\'s turn (respond, or give the final '
                'confirmation once terms are agreed).',
        'recipients': 'The supplier on this order.',
        'audiences': ('supplier',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_MEDIUM,
        'required_module': None,
        'exclude_actor': True,
    },
    EVENT_SUPPLIER_OFFER_FOR_REVIEW: {
        'when': 'A supplier offers a product from the Product Master List, or '
                "requests a brand-new one, and it needs the Owner's decision.",
        'recipients': 'Owner only (no branch is involved, so no Manager/Staff).',
        'audiences': ('internal',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_MEDIUM,
        'required_module': None,
        'exclude_actor': True,
    },
    EVENT_SUPPLIER_OFFER_DECISION: {
        'when': "The Owner approves or rejects a supplier's product offer or "
                'new-product request.',
        'recipients': 'The supplier who made the offer.',
        'audiences': ('supplier',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_MEDIUM,
        'required_module': None,
        'exclude_actor': True,
    },
    EVENT_PO_OVERDUE: {
        'when': 'An Incoming or Partially Received purchase order is still '
                'undelivered PO_OVERDUE_GRACE_DAYS (default 1) after its '
                'delivery date; fires once, by the daily sweep.',
        'recipients': 'Owner; branch Manager/Staff with Deliveries access; the supplier on the order.',
        'audiences': ('internal', 'supplier'),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_HIGH,
        'required_module': DELIVERIES,
        'exclude_actor': False,
    },
    EVENT_PO_OVERDUE_ESCALATED: {
        'when': 'The same order is still undelivered PO_OVERDUE_ESCALATE_DAYS '
                '(default 3) after its delivery date; fires once, by the daily sweep.',
        'recipients': 'Owner only.',
        'audiences': ('internal',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_CRITICAL,
        'required_module': None,
        'exclude_actor': False,
    },
    EVENT_SUPPLIER_RETURN_FOR_SUPPLIER: {
        'when': 'The store raises a return to the supplier, ships it back, '
                'settles it, or cancels it.',
        'recipients': 'The supplier the goods are being returned to.',
        'audiences': ('supplier',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_MEDIUM,
        'required_module': None,
        'exclude_actor': True,
    },
    EVENT_SUPPLIER_RETURN_RESPONSE: {
        'when': 'The supplier acknowledges or rejects a return request.',
        'recipients': 'Owner; branch Manager/Staff with Procurement access.',
        'audiences': ('internal',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_MEDIUM,
        'priority_note': 'High when the supplier rejects the return.',
        'required_module': PROCUREMENT,
        'exclude_actor': True,
    },
    EVENT_ORDER_DELIVERED: {
        'when': 'An online Delivery order moves to Completed.',
        'recipients': 'The customer who placed the order.',
        'audiences': ('customer',),
        'category': CATEGORY_SALES,
        'priority': PRIORITY_LOW,
        'required_module': None,
        'exclude_actor': True,
    },
    EVENT_PAYMENT_VERIFIED: {
        'when': 'A payment moves from Pending to Paid.',
        'recipients': 'The customer attached to the order.',
        'audiences': ('customer',),
        'category': CATEGORY_SALES,
        'priority': PRIORITY_LOW,
        'required_module': None,
        'exclude_actor': True,
    },
    EVENT_PAYMENT_REJECTED: {
        'when': 'A payment moves from Pending to Rejected.',
        'recipients': 'Customer; Owner; branch Manager/Staff with Billing access.',
        'audiences': ('customer', 'internal'),
        'category': CATEGORY_SALES,
        'priority': PRIORITY_HIGH,
        'required_module': BILLING,
        'exclude_actor': True,
    },
    EVENT_EXPIRY_WRITE_OFF: {
        'when': 'The daily expiry sweep removes one or more due batches from '
                'Floor or Warehouse Stock, unattended (no actor).',
        'recipients': 'Owner; branch Manager/Staff with the affected Floor/Warehouse Stock access.',
        'audiences': ('internal',),
        'category': CATEGORY_PROCUREMENT,
        'priority': PRIORITY_HIGH,
        'priority_note': 'High — stays unresolved until the Owner (or that '
                          'branch\'s Manager/Staff) opens it, the same "needs '
                          'confirming" treatment every other High-priority '
                          'alert gets; there is no separate approval step '
                          'before the write-off itself, which the unattended '
                          'sweep has already applied by the time this fires.',
        'required_module': FLOOR_STOCK,
        'allowed_modules': (FLOOR_STOCK, WAREHOUSE_STOCK),
        'exclude_actor': False,
    },
}

# Priorities that stay pinned as unresolved until the Owner acts on them,
# rather than clearing the moment they're opened.
_REQUIRES_ACTION = frozenset({PRIORITY_CRITICAL, PRIORITY_HIGH})

# Placeholder branch-wide threshold for the "unusually large order" trigger.
# A per-branch threshold (set from Administration) is the natural next step
# — not built yet, so every branch shares this one constant for now.
LARGE_ORDER_THRESHOLD = Decimal('5000.00')


def _recipients(branch=None, required_module=None):
    """Owner accounts, plus — only when `required_module` is given —
    `branch`'s Manager and Staff who have that module enabled. See the
    module docstring for the reasoning."""
    from django.db.models import Q

    from accounts.models import User
    from core.module_permissions import user_can_access_module
    from core.permissions import BRANCH_MANAGER, OWNER, STAFF

    users = list(User.objects.filter(role=OWNER, is_active=True))
    if branch is None or not required_module:
        return users

    branch_users = User.objects.filter(
        role__in=(BRANCH_MANAGER, STAFF), is_active=True,
    ).filter(
        Q(manager_profile__branch=branch) | Q(staff_profile__branch=branch)
    )
    users += [
        u for u in branch_users if user_can_access_module(u, required_module)
    ]

    return users


def notify(category, priority, title, message, branch=None,
           action_url='', action_label='', required_module=None,
           event_type='', reference_type='', reference_id='', exclude_user=None):
    """
    Create one Notification per recipient (see _recipients). Returns how
    many rows were written.

    `required_module` is one of core.module_permissions' codes (e.g.
    PROCUREMENT, SALES_POS, BILLING, FLOOR_STOCK, DELIVERIES) — pass it when
    the event is something a Manager or Staff member with that module could
    plausibly act on, so it also reaches whichever of the branch's Manager/
    Staff have it enabled. Leave it unset for Administration/security-tier
    events, which reach the Owner only — not even the branch's Manager.

    requires_action is derived from priority (Critical/High), not passed in
    — keeps every call site from having to repeat that policy decision.
    """
    from accounts.models import Notification

    recipients = _recipients(branch, required_module)
    excluded_id = getattr(exclude_user, 'pk', None)
    if excluded_id is not None:
        recipients = [user for user in recipients if user.pk != excluded_id]
    if not recipients:
        return 0

    rows = [
        Notification(
            user=user, title=title, message=message,
            category=category, priority=priority, branch=branch,
            action_url=action_url, action_label=action_label,
            requires_action=priority in _REQUIRES_ACTION,
            event_type=event_type, recipient_role=user.role,
            reference_type=reference_type,
            reference_id=str(reference_id) if reference_id is not None else '',
        )
        for user in recipients
    ]
    Notification.objects.bulk_create(rows)
    return len(rows)


def notify_supplier(supplier, priority, title, message,
                     action_url='', action_label='', event_type='',
                     reference_type='', reference_id=''):
    """
    One notification straight to a specific Supplier user.

    Unlike notify() above, there's no fan-out to compute: a purchase order
    already has exactly one supplier (Procurement.supplier is a direct FK to
    that User), so this writes one row for that one recipient and nothing
    else — never the Owner or a branch's Manager/Staff, who already learn
    about the same event through their own notify() call at the same call
    site when the trigger warrants it.

    requires_action is always False here: a supplier can't "resolve"
    anything the way the Owner Notification Center means it (there's no
    internal action left for them to take beyond reading), so their tray
    never needs the Resolve workflow — only Dismiss.

    No-ops (returns 0) if `supplier` isn't an active Supplier account —
    Procurement.supplier is nullable (SET_NULL), so a deleted/deactivated
    supplier must not silently raise here.
    """
    from accounts.models import Notification
    from core.permissions import SUPPLIER

    if supplier is None or supplier.role != SUPPLIER or not supplier.is_active:
        return 0

    Notification.objects.create(
        user=supplier, title=title, message=message,
        category=CATEGORY_PROCUREMENT, priority=priority,
        action_url=action_url, action_label=action_label,
        requires_action=False,
        event_type=event_type, recipient_role=supplier.role,
        reference_type=reference_type,
        reference_id=str(reference_id) if reference_id is not None else '',
    )
    return 1


def notify_customer(customer, title, message, action_url='', action_label=''):
    """
    One notification straight to a specific CUSTOMER account — the
    customer-facing counterpart to notify_supplier() above. A
    SalesTransaction has exactly one customer, so there's no fan-out to
    compute, same reasoning as notify_supplier().

    category/priority/branch/requires_action are left at the Notification
    model's own defaults (blank/LOW/None/False): those fields back the Owner
    Notification Center (see notify() above and the model's own docstring),
    not a customer's order-update inbox, which only ever reads title/
    message/action_url/action_label/is_read.

    No-ops (returns 0) if `customer` is None or not an active CUSTOMER
    account — SalesTransaction.customer is nullable (SET_NULL), so a
    deleted/deactivated customer must not silently raise here.
    """
    from accounts.models import Notification
    from core.permissions import CUSTOMER

    if customer is None or customer.role != CUSTOMER or not customer.is_active:
        return 0

    Notification.objects.create(
        user=customer, title=title, message=message,
        action_url=action_url, action_label=action_label,
    )
    return 1


def notify_event(event_type, title, message, *, branch=None, actor=None,
                 related_users=None, action_url='', action_label='',
                 reference_type='', reference_id='', audience_overrides=None,
                 deduplicate=False, priority=None, audiences=None,
                 required_module=None):
    """Dispatch one matrix event and return the number of rows created.

    ``related_users`` supplies relational audiences named by the rule, such
    as ``{'customer': transaction.customer}`` or
    ``{'requester': procurement.created_by, 'supplier': procurement.supplier}``.
    ``audience_overrides`` can give one audience its portal-specific title,
    message or link while preserving one recipient policy.  Expiry scans use
    ``deduplicate=True`` so the daily command creates at most one notice per
    recipient and batch.
    """
    from accounts.models import Notification, User

    try:
        rule = EVENT_MATRIX[event_type]
    except KeyError as exc:
        raise ValueError(f'Unknown notification event: {event_type}') from exc

    related_users = related_users or {}
    audience_overrides = audience_overrides or {}
    selected_priority = priority or rule['priority']
    selected_module = required_module or rule['required_module']
    allowed_modules = rule.get('allowed_modules')
    if required_module and (
        allowed_modules is None or required_module not in allowed_modules
    ):
        raise ValueError(
            f'{event_type} cannot be routed through module {required_module}'
        )
    candidates = []

    selected_audiences = tuple(audiences or rule['audiences'])
    invalid_audiences = set(selected_audiences) - set(rule['audiences'])
    if invalid_audiences:
        raise ValueError(
            f'{event_type} does not route to: {sorted(invalid_audiences)}'
        )

    for audience in selected_audiences:
        if audience == 'internal':
            candidates.extend(
                (audience, user)
                for user in _recipients(branch, selected_module)
            )
            continue

        users = related_users.get(audience)
        if users is None:
            continue
        if isinstance(users, User):
            users = (users,)
        candidates.extend((audience, user) for user in users if user is not None)

    # One row per recipient even if a person appears in two audiences (for
    # example an Owner who both raised and approved their own PO).
    seen_user_ids = set()
    rows = []
    ref_id = str(reference_id) if reference_id is not None else ''
    actor_id = getattr(actor, 'pk', None)
    for audience, user in candidates:
        if not user.is_active or user.pk in seen_user_ids:
            continue
        if rule['exclude_actor'] and actor_id == user.pk:
            continue
        seen_user_ids.add(user.pk)

        if deduplicate and Notification.objects.filter(
            user=user,
            event_type=event_type,
            reference_type=reference_type,
            reference_id=ref_id,
        ).exists():
            continue

        content = audience_overrides.get(audience, {})
        row_title = content.get('title', title)
        row_message = content.get('message', message)
        row_url = content.get('action_url', action_url)
        row_label = content.get('action_label', action_label)

        is_customer = user.role == User.Role.CUSTOMER
        is_supplier = user.role == User.Role.SUPPLIER
        category = '' if is_customer else rule['category']
        if is_supplier:
            category = CATEGORY_PROCUREMENT
        requires_action = (
            not is_customer and not is_supplier
            and selected_priority in _REQUIRES_ACTION
        )

        rows.append(Notification(
            user=user,
            title=row_title,
            message=row_message,
            category=category,
            priority=selected_priority,
            branch=branch if not is_customer and not is_supplier else None,
            action_url=row_url,
            action_label=row_label,
            requires_action=requires_action,
            event_type=event_type,
            recipient_role=user.role,
            reference_type=reference_type,
            reference_id=ref_id,
        ))

    if rows:
        Notification.objects.bulk_create(rows)
    return len(rows)
