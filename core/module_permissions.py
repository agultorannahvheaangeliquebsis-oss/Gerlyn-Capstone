"""Per-user module access — the Owner's granular "who can see what" panel.

This is a THIRD, additive axis on top of the existing two:
  role            (core.permissions)  — what kind of account this is
  branch scoping  (core.scoping)      — which branch's data they see
  module access   (this file)         — which internal sections they see,
                                         independent of the other two

Only STAFF and BRANCH_MANAGER are subject to this. OWNER always has every
module (they're the one granting access); SUPPLIER/CUSTOMER have their own
separate portals and never touch this system at all.

A module being unchecked for a user is a RESTRICTION, never an escalation:
some of these views were previously Owner/Manager-only at the role level and
have been opened up to STAFF specifically so this permission can mean
something for them — see the `role_required`/inline role-check changes made
alongside this file. None of this ever grants a Staff/Manager account access
to something their role couldn't already do (Branch Management and User
Management are not on this list — they stay Owner-only, full stop).

Each module maps to the Django url names it gates. ModulePermissionMiddleware
(accounts/middleware.py) resolves the current request's url name against
this registry once per request, so adding a new gated page later is just
adding its url name to a list here — no new decorator to remember to attach.
"""

PRODUCTS = 'products'
FLOOR_STOCK = 'floor_stock'
WAREHOUSE_STOCK = 'warehouse_stock'
WITHDRAWALS = 'withdrawals'
ALERTS = 'alerts'
SALES_POS = 'sales_pos'
RETURNS = 'returns'
BILLING = 'billing'
PROCUREMENT = 'procurement'
DELIVERIES = 'deliveries'
REPORTS = 'reports'

# code -> (label, [url names this module gates])
MODULE_REGISTRY = {
    PRODUCTS: (
        'Product Master List',
        ['product_list', 'product_detail', 'category_list'],
    ),
    FLOOR_STOCK: (
        'Floor Stock',
        ['inventory_list', 'inventory_edit', 'stock_adjustment_add'],
    ),
    WAREHOUSE_STOCK: (
        'Warehouse Stock',
        ['warehouse_stock_list', 'warehouse_stock_edit', 'warehouse_stock_request'],
    ),
    WITHDRAWALS: (
        'Stock Withdrawals',
        ['transfer_list', 'transfer_detail', 'transfer_confirm_receipt',
         'transfer_flag_discrepancy'],
    ),
    ALERTS: (
        'Alerts Center',
        ['alerts_center'],
    ),
    SALES_POS: (
        'Sales / Order Console',
        ['transaction_list', 'transaction_add', 'transaction_detail',
         'transaction_set_status'],
    ),
    RETURNS: (
        'Customer Returns',
        ['return_list', 'return_detail'],
    ),
    BILLING: (
        'Billing',
        ['billing_list', 'billing_detail', 'billing_create', 'billing_confirm'],
    ),
    PROCUREMENT: (
        'Procurement / Purchase Orders',
        ['procurement_list', 'procurement_detail', 'supplier_list'],
    ),
    DELIVERIES: (
        'Deliveries',
        ['delivery_list', 'delivery_receive', 'receiving_history'],
    ),
    REPORTS: (
        'Reports Dashboard',
        ['reports_dashboard', 'sales_report', 'inventory_report',
         'procurement_report', 'billing_report'],
    ),
}

# Deliberately NOT covered by any module (stay gated purely by role, as
# before): product_add/edit/delete, category_form/delete, transfer_approve/
# complete, procurement_add/update_status, supplier_add/edit/delete,
# export_csv, return_create, return_approve, return_reject,
# return_mark_refunded. These are management/approval/data-export actions
# rather than "viewing a section" — granting a module here only ever opens
# the view, not these adjacent write/approve actions, which stay
# Owner/Manager-only (or, for return_create, role-checked inline the same
# way transfer_create is) exactly as they were before this feature existed.

ALL_MODULE_CODES = list(MODULE_REGISTRY.keys())

# Reverse index built once at import time: url_name -> module_code.
URL_NAME_TO_MODULE = {
    url_name: code
    for code, (_label, url_names) in MODULE_REGISTRY.items()
    for url_name in url_names
}

# What a brand-new (or pre-existing, backfilled) STAFF account defaults to —
# exactly what STAFF could already reach before this feature shipped, so
# turning this on never silently expands anyone's access. The Owner opts
# staff INTO the newly-available modules (Product Master List, Withdrawals,
# Alerts, Billing, Procurement, Reports) explicitly, per employee.
DEFAULT_STAFF_MODULES = [FLOOR_STOCK, WAREHOUSE_STOCK, SALES_POS, RETURNS, DELIVERIES]


def default_allowed_modules():
    """Model field default — used for BRANCH_MANAGER and as the fallback
    everywhere else. Full list: a Branch Manager already had every one of
    these modules before this feature existed, so this default preserves
    that. Staff accounts get DEFAULT_STAFF_MODULES instead, set explicitly
    wherever a Staff account is created (see accounts/signals.py and
    accounts/views.py:user_form) rather than through this field default,
    since a static default can't see the role being assigned."""
    return list(ALL_MODULE_CODES)


def user_can_access_module(user, module_code):
    """True if `user` may reach a page gated to `module_code`.

    OWNER (and anyone not STAFF/BRANCH_MANAGER — SUPPLIER, CUSTOMER) always
    passes: this system only ever restricts Staff/Manager accounts.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    role = getattr(user, 'role', None)
    if role not in ('STAFF', 'BRANCH_MANAGER'):
        return True
    allowed = getattr(user, 'allowed_modules', None) or []
    return module_code in allowed


def module_access_map(user):
    """{module_code: bool} for every registered module — what the sidebar
    nav (and any other "should I show this link" check) reads from."""
    return {code: user_can_access_module(user, code) for code in ALL_MODULE_CODES}
