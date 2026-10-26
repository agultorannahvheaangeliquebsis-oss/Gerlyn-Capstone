"""Per-user, per-module CRUD permissions for internal accounts.

Role and branch checks remain authoritative. This module adds a restrictive
layer on top: an Owner can grant a Manager or Staff account View, Add, Edit,
and Delete access to each internal module. A grant here never elevates a
role; all existing role and branch checks still run afterward.

``allowed_modules`` on accounts.User is the canonical View grant (kept for
backward compatibility with existing data and notification rules).
``module_actions`` stores the Add/Edit/Delete grants. Every write permission
also requires View, so disabling a module closes its direct URL/API write
surface even if stale action grants remain in stored data.
"""

PRODUCTS = 'products'
FLOOR_STOCK = 'floor_stock'
WAREHOUSE_STOCK = 'warehouse_stock'
WITHDRAWALS = 'withdrawals'
ALERTS = 'alerts'
DAMAGE_REPORTS = 'damage_reports'
SALES_POS = 'sales_pos'
BILLING = 'billing'
PROCUREMENT = 'procurement'
DELIVERIES = 'deliveries'
REPORTS = 'reports'

# Roles allowed to see management reports (mirrors
# core.permissions.MANAGEMENT_REPORT_ROLES, repeated as plain strings because
# core.permissions imports DRF and this module is kept dependency-free).
REPORT_ROLES = ('OWNER', 'BRANCH_MANAGER')

VIEW = 'view'
ADD = 'add'
EDIT = 'edit'
DELETE = 'delete'
MODULE_ACTIONS = (VIEW, ADD, EDIT, DELETE)
WRITE_ACTIONS = (ADD, EDIT, DELETE)
MODULE_ACTION_LABELS = {
    VIEW: 'View', ADD: 'Add', EDIT: 'Edit', DELETE: 'Delete',
}


# code -> {label, action -> URL names}. A route belongs to exactly one module.
# Existing role/branch checks still decide whether a granted action is valid
# for that account and record; this registry only adds restrictions.
MODULE_PERMISSION_REGISTRY = {
    PRODUCTS: {
        'label': 'Product Master List',
        VIEW: [
            'product_list', 'product_detail', 'category_list',
            'product_export_xlsx', 'barcode_lookup',
            'api-product-list', 'api-product-detail',
            'api-category-list', 'api-category-detail',
        ],
        ADD: [
            'product_add', 'category_add',
            'product_import_upload', 'product_import_map',
            'product_import_preview', 'product_import_cancel',
            'api-product-list', 'api-category-list',
        ],
        EDIT: [
            'product_edit', 'category_edit',
            'api-product-detail', 'api-category-detail',
        ],
        DELETE: [
            'product_delete', 'category_delete',
            'api-product-detail', 'api-category-detail',
        ],
    },
    FLOOR_STOCK: {
        'label': 'Floor Stock',
        VIEW: [
            'inventory_list', 'opening_stock_template',
            'opening_stock_import_history', 'opening_stock_import_detail',
            'api-inventory-list', 'api-inventory-detail',
            'api-batch-list', 'api-batch-detail', 'api-stock-movements',
        ],
        ADD: [
            'stock_adjustment_add', 'opening_stock_import',
            'opening_stock_confirm',
        ],
        EDIT: ['inventory_edit'],
        DELETE: ['opening_stock_cancel', 'opening_stock_import_reverse'],
    },
    WAREHOUSE_STOCK: {
        'label': 'Warehouse Stock',
        VIEW: [
            'warehouse_stock_list',
            'api-warehouse-stock-list', 'api-warehouse-stock-detail',
            'api-warehouse-list', 'api-warehouse-detail',
        ],
        ADD: [
            'warehouse_stock_request', 'warehouse_stock_bulk_request',
            'api-warehouse-stock-list', 'api-warehouse-list',
        ],
        EDIT: [
            'warehouse_stock_edit',
            'api-warehouse-stock-detail', 'api-warehouse-detail',
        ],
        DELETE: ['api-warehouse-stock-detail', 'api-warehouse-detail'],
    },
    WITHDRAWALS: {
        'label': 'Stock Transfers',
        VIEW: [
            'transfer_list', 'transfer_detail',
            'api-stock-transfer-list', 'api-stock-transfer-detail',
        ],
        ADD: ['transfer_create', 'api-stock-transfer-list'],
        EDIT: [
            'transfer_approve', 'transfer_complete', 'transfer_scan',
            'transfer_pick', 'transfer_confirm_receipt', 'transfer_flag_discrepancy',
            'api-stock-transfer-approve', 'api-stock-transfer-release',
            'api-stock-transfer-acknowledge',
        ],
        DELETE: [],
    },
    ALERTS: {
        'label': 'Alerts Center',
        VIEW: ['alerts_center'],
        ADD: [], EDIT: [], DELETE: ['expiry_write_off'],
    },
    # Damaged Product Reports. ADD is filing a report (Staff do, for their own
    # branch); EDIT is deciding one (review/approve/reject/resolve). The views
    # keep the role rule - only an Owner or Branch Manager can decide - so an
    # Edit grant on a Staff account opens nothing.
    DAMAGE_REPORTS: {
        'label': 'Damage Reports',
        VIEW: [
            'damage_report_list', 'damage_report_detail', 'damage_report_photo',
        ],
        ADD: ['damage_report_create'],
        EDIT: [
            'damage_report_review', 'damage_report_approve',
            'damage_report_reject', 'damage_report_resolve',
        ],
        DELETE: [],
    },
    SALES_POS: {
        'label': 'Sales / Order Console',
        VIEW: [
            'transaction_list', 'transaction_detail', 'transaction_receipt',
            'transaction_delivery_receipt', 'transaction_delivery_receipt_pdf',
            'transaction_receipt_evidence',
            'pos_sales_import_history',
            'api-sales-list', 'api-sales-detail', 'api-sales-refunds',
        ],
        ADD: [
            'transaction_add', 'pos_sales_import', 'pos_sales_import_confirm',
            'pos_sales_import_cancel', 'api-sales-list',
        ],
        EDIT: [
            'transaction_set_status', 'transaction_confirm_receipt',
            'transaction_apply_discount',
            'transaction_pwd_request_review', 'transaction_refund',
            'transaction_refund_approve', 'transaction_refund_reject',
            'api-sales-refunds', 'api-sales-refund-approve',
            'api-sales-refund-reject', 'api-sales-pwd-request',
        ],
        DELETE: ['pos_sales_import_undo'],
    },
    BILLING: {
        'label': 'Billing',
        VIEW: [
            'billing_list', 'billing_detail', 'billing_proof',
            'payment_proof_download', 'api-billing-list', 'api-billing-detail',
        ],
        ADD: [],
        EDIT: [
            'billing_create', 'billing_confirm',
            'api-billing-pay', 'api-billing-confirm',
        ],
        DELETE: [],
    },
    PROCUREMENT: {
        'label': 'Procurement / Purchase Orders',
        VIEW: [
            'procurement_list', 'procurement_detail', 'procurement_print',
            'procurement_pdf', 'supplier_list', 'supplier_detail',
            'supplier_category_list', 'supplier_return_list',
            'supplier_return_detail', 'api-procurement-list',
            'api-procurement-detail', 'api-procurement-suppliers',
            'api-procurement-supplier-products',
        ],
        ADD: [
            'procurement_add', 'supplier_add', 'supplier_category_add',
            'supplier_product_add', 'supplier_return_add',
            'api-procurement-list',
        ],
        EDIT: [
            'procurement_update_status', 'procurement_approve',
            'procurement_set_funding', 'procurement_reschedule',
            'procurement_owner_respond', 'supplier_edit',
            'supplier_category_edit', 'supplier_offer_review',
            'supplier_request_review', 'supplier_product_transfer',
            'supplier_return_ship', 'supplier_return_credit',
            'supplier_return_replacement', 'api-procurement-status',
        ],
        DELETE: [
            'supplier_delete', 'supplier_category_delete',
            'supplier_product_remove', 'supplier_return_cancel',
            'api-procurement-detail',
        ],
    },
    DELIVERIES: {
        'label': 'Deliveries',
        VIEW: ['delivery_list', 'delivery_detail'],
        ADD: ['delivery_add_unexpected'],
        EDIT: [
            'delivery_receive', 'delivery_scan',
            'procurement_mark_communicated',
            'api-procurement-communicate',
            'api-procurement-receive',
        ], DELETE: [],
    },
    REPORTS: {
        'label': 'Reports Dashboard',
        VIEW: [
            'reports_dashboard', 'sales_report', 'inventory_report',
            'procurement_report', 'billing_report', 'export_csv',
            'api-reports-dashboard', 'api-reports-sales',
            'api-reports-inventory', 'api-reports-procurement',
        ],
        ADD: [], EDIT: [], DELETE: [],
    },
}


# Compatibility shape used by existing navigation/admin code: code ->
# (label, every URL in the module). De-duplication preserves declaration order.
MODULE_REGISTRY = {}
for _code, _config in MODULE_PERMISSION_REGISTRY.items():
    _urls = []
    for _action in MODULE_ACTIONS:
        for _url in _config[_action]:
            if _url not in _urls:
                _urls.append(_url)
    MODULE_REGISTRY[_code] = (_config['label'], _urls)

ALL_MODULE_CODES = list(MODULE_PERMISSION_REGISTRY.keys())

# Defaults are the attached GerlynPLUS permission matrix expressed in the
# application's existing module/action vocabulary.  Some matrix rows (for
# example receiving and verifying a delivery) are workflow operations rather
# than literal CRUD operations; those are registered as ``edit`` above.
# Role checks still put the upper bound on what a grant can authorize.
ROLE_DEFAULT_ACTIONS = {
    'OWNER': {
        code: [
            action for action in WRITE_ACTIONS
            if MODULE_PERMISSION_REGISTRY[code][action]
        ]
        for code in ALL_MODULE_CODES
    },
    'BRANCH_MANAGER': {
        PRODUCTS: [],
        FLOOR_STOCK: [ADD],
        WAREHOUSE_STOCK: [],
        WITHDRAWALS: [ADD, EDIT],
        # DELETE here is the expiry write-off (inventory.views.
        # expiry_write_off), not a destructive delete — it only ever zeroes
        # a lot that is ALREADY expired. The view itself has always allowed
        # Owner/Branch Manager/Staff; this default previously didn't match
        # (KATRINA-RBAC-001).
        ALERTS: [DELETE],
        DAMAGE_REPORTS: [ADD, EDIT],
        SALES_POS: [ADD, EDIT],
        BILLING: [EDIT],
        PROCUREMENT: [ADD, EDIT, DELETE],
        DELIVERIES: [ADD, EDIT],
        REPORTS: [],
    },
    'STAFF': {
        PRODUCTS: [],
        FLOOR_STOCK: [ADD],
        WAREHOUSE_STOCK: [],
        WITHDRAWALS: [ADD, EDIT],
        # See the Branch Manager row above — same fix, same reasoning. The
        # view's own docstring calls this "a safe operational fallback"
        # open to Staff by design.
        ALERTS: [DELETE],
        # Staff file reports and follow their own; deciding them is a
        # Manager/Owner job (the views enforce it whatever is granted here).
        DAMAGE_REPORTS: [ADD],
        SALES_POS: [ADD, EDIT],
        BILLING: [EDIT],
        PROCUREMENT: [],
        DELIVERIES: [EDIT],
        REPORTS: [],
    },
    # Supplier/customer portals have their own action-level role checks.  The
    # entries here still describe their matrix defaults for role switching and
    # keep irrelevant internal modules unchecked.
    'SUPPLIER': {
        code: [] for code in ALL_MODULE_CODES
    },
    'CUSTOMER': {
        code: [] for code in ALL_MODULE_CODES
    },
}

ROLE_DEFAULT_MODULES = {
    'OWNER': list(ALL_MODULE_CODES),
    'BRANCH_MANAGER': list(ALL_MODULE_CODES),
    'STAFF': [
        PRODUCTS, FLOOR_STOCK, WAREHOUSE_STOCK, WITHDRAWALS, ALERTS,
        DAMAGE_REPORTS, SALES_POS, BILLING, PROCUREMENT, DELIVERIES,
    ],
    'SUPPLIER': [PROCUREMENT, DELIVERIES],
    'CUSTOMER': [SALES_POS, BILLING],
}

# Kept as a public compatibility constant for notification and test code.
DEFAULT_STAFF_MODULES = list(ROLE_DEFAULT_MODULES['STAFF'])

URL_NAME_TO_MODULE = {
    url_name: code
    for code, (_label, url_names) in MODULE_REGISTRY.items()
    for url_name in url_names
}

URL_NAME_TO_ACTIONS = {}
for _code, _config in MODULE_PERMISSION_REGISTRY.items():
    for _action in MODULE_ACTIONS:
        for _url in _config[_action]:
            URL_NAME_TO_ACTIONS.setdefault(_url, set()).add(_action)


def default_allowed_modules():
    """Default View grants for a Manager; other roles are signal-managed."""
    return list(ROLE_DEFAULT_MODULES['BRANCH_MANAGER'])


def default_module_actions():
    """Default Manager actions used by the model field callable."""
    return role_permission_defaults('BRANCH_MANAGER')['module_actions']


def role_permission_defaults(role):
    """Return a fresh, normalized permission payload for ``role``.

    Returning copies is important because JSONField values and the browser's
    role-switch payload are both mutable.
    """
    modules = list(ROLE_DEFAULT_MODULES.get(role, []))
    configured_actions = ROLE_DEFAULT_ACTIONS.get(role, {})
    return {
        'allowed_modules': modules,
        'module_actions': {
            code: [
                action for action in WRITE_ACTIONS
                if (
                    action in configured_actions.get(code, [])
                    and MODULE_PERMISSION_REGISTRY[code][action]
                )
            ]
            for code in ALL_MODULE_CODES
        },
    }


def role_permission_defaults_for_ui(roles):
    """JSON-serializable role defaults consumed by the edit-page script."""
    return {role: role_permission_defaults(role) for role in roles}


def normalize_module_actions(value):
    """Return a clean ``{module: [add/edit/delete]}`` mapping."""
    if not isinstance(value, dict):
        # Malformed/corrupt persisted data must fail closed. Normal accounts
        # receive the backward-compatible grants from the model default and
        # data migration, so there is no reason to infer grants here.
        return {code: [] for code in ALL_MODULE_CODES}
    normalized = {}
    for code in ALL_MODULE_CODES:
        raw = value.get(code, [])
        if not isinstance(raw, (list, tuple, set)):
            raw = []
        normalized[code] = [
            action for action in WRITE_ACTIONS
            if action in raw and MODULE_PERMISSION_REGISTRY[code][action]
        ]
    return normalized


def permission_rows(allowed_modules, module_actions):
    """Rows consumed by the User Management permission matrix template."""
    allowed = set(allowed_modules or [])
    actions = normalize_module_actions(module_actions)
    return [
        {
            'code': code,
            'label': MODULE_PERMISSION_REGISTRY[code]['label'],
            VIEW: code in allowed,
            ADD: code in allowed and ADD in actions[code],
            EDIT: code in allowed and EDIT in actions[code],
            DELETE: code in allowed and DELETE in actions[code],
            'supports_add': bool(MODULE_PERMISSION_REGISTRY[code][ADD]),
            'supports_edit': bool(MODULE_PERMISSION_REGISTRY[code][EDIT]),
            'supports_delete': bool(MODULE_PERMISSION_REGISTRY[code][DELETE]),
        }
        for code in ALL_MODULE_CODES
    ]


def user_can_perform_module_action(user, module_code, action=VIEW):
    """Whether a user passes this restrictive module/action layer."""
    if not getattr(user, 'is_authenticated', False):
        return False
    role = getattr(user, 'role', None)
    # Management reports are Owner / Branch Manager only. Without this cap the
    # "Owner/Supplier/Customer always True" rule below would advertise the
    # Reports link to a Supplier or Customer, and an Owner-granted View on
    # Staff would light it up for a role the report views reject.
    if module_code == REPORTS and role not in REPORT_ROLES:
        return False
    if role not in ('STAFF', 'BRANCH_MANAGER'):
        return True
    if module_code not in ALL_MODULE_CODES or action not in MODULE_ACTIONS:
        return False

    allowed = set(getattr(user, 'allowed_modules', None) or [])
    if module_code not in allowed:
        return False
    if action == VIEW:
        return True

    actions = normalize_module_actions(getattr(user, 'module_actions', None))
    return action in actions.get(module_code, [])


def user_can_access_module(user, module_code):
    """Backward-compatible name for the module's View permission."""
    return user_can_perform_module_action(user, module_code, VIEW)


def required_action_for_request(url_name, method='GET'):
    """Resolve one route/method to View, Add, Edit, or Delete."""
    actions = URL_NAME_TO_ACTIONS.get(url_name, set())
    if len(actions) <= 1:
        return next(iter(actions), VIEW)

    method = method.upper()
    if method in ('GET', 'HEAD', 'OPTIONS') and VIEW in actions:
        return VIEW
    if method == 'POST' and ADD in actions:
        return ADD
    if method in ('PUT', 'PATCH') and EDIT in actions:
        return EDIT
    if method == 'DELETE' and DELETE in actions:
        return DELETE

    for action in (DELETE, EDIT, ADD, VIEW):
        if action in actions:
            return action
    return VIEW


def module_access_map(user):
    return {code: user_can_access_module(user, code) for code in ALL_MODULE_CODES}


def module_action_map(user):
    """Nested map exposed to templates (module_actions.products.add, etc.)."""
    return {
        code: {
            action: user_can_perform_module_action(user, code, action)
            for action in MODULE_ACTIONS
        }
        for code in ALL_MODULE_CODES
    }
