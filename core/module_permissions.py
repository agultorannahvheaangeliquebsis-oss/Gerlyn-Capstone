"""Central action-level authorization for GerlynPLUS internal modules.

Authorization is cumulative: the role ceiling, branch scope, and the stored
per-user grant must all permit an operation. Grants can restrict a Manager or
Staff account, but can never promote it beyond its role.
"""

PRODUCTS = 'products'
FLOOR_STOCK = 'floor_stock'
WAREHOUSE_STOCK = 'warehouse_stock'
WITHDRAWALS = 'withdrawals'
ALERTS = 'alerts'
SALES_POS = 'sales_pos'
BILLING = 'billing'
PROCUREMENT = 'procurement'
DELIVERIES = 'deliveries'
REPORTS = 'reports'

VIEW = 'view'
ADD = 'add'
EDIT = 'edit'
DELETE = 'delete'
ACTIONS = (VIEW, ADD, EDIT, DELETE)

MODULE_LABELS = {
    PRODUCTS: 'Product Master List',
    FLOOR_STOCK: 'Floor Stock',
    WAREHOUSE_STOCK: 'Warehouse Stock',
    WITHDRAWALS: 'Stock Withdrawals',
    ALERTS: 'Alerts Center',
    SALES_POS: 'Sales / Order Console',
    BILLING: 'Billing',
    PROCUREMENT: 'Procurement / Purchase Orders',
    DELIVERIES: 'Deliveries',
    REPORTS: 'Reports Dashboard',
}
ALL_MODULE_CODES = list(MODULE_LABELS)

# Mirrors existing role checks. Stored data cannot exceed this ceiling.
ROLE_ACTION_CEILINGS = {
    'BRANCH_MANAGER': {
        PRODUCTS: {VIEW, ADD, EDIT, DELETE},
        FLOOR_STOCK: {VIEW, EDIT},
        WAREHOUSE_STOCK: {VIEW, ADD, EDIT},
        WITHDRAWALS: {VIEW, ADD, EDIT},
        ALERTS: {VIEW, DELETE},
        SALES_POS: {VIEW, ADD, EDIT},
        BILLING: {VIEW, ADD, EDIT},
        PROCUREMENT: {VIEW, ADD, EDIT, DELETE},
        DELIVERIES: {VIEW, EDIT},
        REPORTS: {VIEW},
    },
    'STAFF': {
        PRODUCTS: {VIEW},
        FLOOR_STOCK: {VIEW},
        WAREHOUSE_STOCK: {VIEW, ADD},
        WITHDRAWALS: {VIEW, ADD, EDIT},
        ALERTS: {VIEW, DELETE},
        SALES_POS: {VIEW, ADD, EDIT},
        BILLING: {VIEW, ADD, EDIT},
        PROCUREMENT: {VIEW},
        DELIVERIES: {VIEW, EDIT},
        REPORTS: {VIEW},
    },
}

DEFAULT_STAFF_MODULES = [FLOOR_STOCK, WAREHOUSE_STOCK, SALES_POS, DELIVERIES]


def default_allowed_modules():
    """Legacy module default retained for compatibility and rollback safety."""
    return list(ALL_MODULE_CODES)


def grants_for_modules(role, module_codes):
    """Translate legacy module grants into action grants within a role ceiling."""
    ceiling = ROLE_ACTION_CEILINGS.get(role, {})
    selected = set(module_codes or [])
    return {
        module: [action for action in ACTIONS if action in ceiling.get(module, set())]
        for module in ALL_MODULE_CODES
        if module in selected and ceiling.get(module)
    }


def default_action_permissions(role, module_codes=None):
    if role == 'STAFF' and module_codes is None:
        module_codes = DEFAULT_STAFF_MODULES
    elif module_codes is None:
        module_codes = ALL_MODULE_CODES
    return grants_for_modules(role, module_codes)


def role_allows(role, module_code, action):
    if role == 'OWNER':
        return module_code in MODULE_LABELS and action in ACTIONS
    return action in ROLE_ACTION_CEILINGS.get(role, {}).get(module_code, set())


def _has_assigned_branch(user):
    role = getattr(user, 'role', None)
    if role == 'BRANCH_MANAGER':
        profile = getattr(user, 'manager_profile', None)
        return bool(profile and profile.branch_id)
    if role == 'STAFF':
        profile = getattr(user, 'staff_profile', None)
        return bool(profile and profile.branch_id)
    return True


def _branch_id(branch):
    if branch is None:
        return None
    return getattr(branch, 'branch_id', None) or getattr(branch, 'pk', None) or branch


def user_can(user, module_code, action, branch=None):
    """Return whether role, branch, and stored-grant checks all pass."""
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', True):
        return False
    if module_code not in MODULE_LABELS or action not in ACTIONS:
        return False

    role = getattr(user, 'role', None)
    if role == 'OWNER':
        return True
    if role not in ROLE_ACTION_CEILINGS or not role_allows(role, module_code, action):
        return False
    if not _has_assigned_branch(user):
        return False

    if branch is not None:
        from core.scoping import user_can_access_branch
        if not user_can_access_branch(user, _branch_id(branch)):
            return False

    grants = getattr(user, 'action_permissions', None)
    # Null is the pre-migration/legacy-fixture state. A key mismatch means an
    # older integration used QuerySet.update() on allowed_modules (which
    # bypasses User.save()); retain that legacy module-level behavior while
    # intersecting it with the role ceiling. Unknown matrix keys are ignored
    # and cannot trigger a broader fallback.
    legacy_modules = {
        module for module in (getattr(user, 'allowed_modules', None) or [])
        if module in MODULE_LABELS
    }
    if grants is None:
        grants = grants_for_modules(
            role, legacy_modules
        )
    elif not isinstance(grants, dict):
        return False
    elif {module for module in grants if module in MODULE_LABELS} != legacy_modules:
        grants = grants_for_modules(role, legacy_modules)
    module_actions = grants.get(module_code, [])
    return isinstance(module_actions, list) and action in module_actions


def user_can_access_module(user, module_code):
    """Compatibility alias for existing notification/navigation callers."""
    return user_can(user, module_code, VIEW)


def module_access_map(user):
    return {code: user_can(user, code, VIEW) for code in ALL_MODULE_CODES}


def action_access_map(user):
    return {
        code: {action: user_can(user, code, action) for action in ACTIONS}
        for code in ALL_MODULE_CODES
    }


# URL name -> one action, or a method/action mapping. A known URL with an
# unmapped method fails closed. None is an explicit public-method exemption.
ROUTE_ACTIONS = {
    'product_list': (PRODUCTS, VIEW),
    'product_add': (PRODUCTS, ADD), 'product_edit': (PRODUCTS, EDIT),
    'product_delete': (PRODUCTS, DELETE), 'product_export_xlsx': (PRODUCTS, VIEW),
    'product_import_upload': (PRODUCTS, ADD), 'product_import_map': (PRODUCTS, ADD),
    'product_import_preview': (PRODUCTS, ADD), 'product_import_cancel': (PRODUCTS, ADD),
    'barcode_lookup': (PRODUCTS, VIEW), 'category_list': (PRODUCTS, VIEW),
    'category_add': (PRODUCTS, ADD), 'category_edit': (PRODUCTS, EDIT),
    'category_delete': (PRODUCTS, DELETE),
    'api-product-list': {'GET': None, 'HEAD': None, 'OPTIONS': None, 'POST': (PRODUCTS, ADD)},
    'api-product-detail': {'GET': None, 'HEAD': None, 'OPTIONS': None,
                           'PUT': (PRODUCTS, EDIT), 'PATCH': (PRODUCTS, EDIT),
                           'DELETE': (PRODUCTS, DELETE)},
    'api-category-list': {'GET': None, 'HEAD': None, 'OPTIONS': None, 'POST': (PRODUCTS, ADD)},
    'api-category-detail': {'GET': None, 'HEAD': None, 'OPTIONS': None,
                            'PUT': (PRODUCTS, EDIT), 'PATCH': (PRODUCTS, EDIT),
                            'DELETE': (PRODUCTS, DELETE)},

    'inventory_list': (FLOOR_STOCK, VIEW), 'inventory_edit': (FLOOR_STOCK, EDIT),
    'stock_adjustment_add': (FLOOR_STOCK, EDIT),
    'opening_stock_import': (FLOOR_STOCK, EDIT), 'opening_stock_confirm': (FLOOR_STOCK, EDIT),
    'opening_stock_cancel': (FLOOR_STOCK, EDIT), 'opening_stock_template': (FLOOR_STOCK, VIEW),
    'opening_stock_import_history': (FLOOR_STOCK, VIEW),
    'opening_stock_import_detail': (FLOOR_STOCK, VIEW),
    'opening_stock_import_reverse': (FLOOR_STOCK, DELETE),
    'api-inventory-list': (FLOOR_STOCK, VIEW), 'api-inventory-detail': (FLOOR_STOCK, VIEW),
    'warehouse_stock_list': (WAREHOUSE_STOCK, VIEW),
    'warehouse_stock_edit': (WAREHOUSE_STOCK, EDIT),
    'warehouse_stock_request': (WAREHOUSE_STOCK, ADD),
    'api-warehouse-list': {'GET': (WAREHOUSE_STOCK, VIEW), 'HEAD': (WAREHOUSE_STOCK, VIEW),
                           'OPTIONS': (WAREHOUSE_STOCK, VIEW), 'POST': (WAREHOUSE_STOCK, ADD)},
    'api-warehouse-detail': {'GET': (WAREHOUSE_STOCK, VIEW), 'HEAD': (WAREHOUSE_STOCK, VIEW),
                             'OPTIONS': (WAREHOUSE_STOCK, VIEW), 'PUT': (WAREHOUSE_STOCK, EDIT),
                             'PATCH': (WAREHOUSE_STOCK, EDIT), 'DELETE': (WAREHOUSE_STOCK, DELETE)},
    'api-warehouse-stock-list': {'GET': (WAREHOUSE_STOCK, VIEW), 'HEAD': (WAREHOUSE_STOCK, VIEW),
                                 'OPTIONS': (WAREHOUSE_STOCK, VIEW), 'POST': (WAREHOUSE_STOCK, ADD)},
    'api-warehouse-stock-detail': {'GET': (WAREHOUSE_STOCK, VIEW), 'HEAD': (WAREHOUSE_STOCK, VIEW),
                                   'OPTIONS': (WAREHOUSE_STOCK, VIEW), 'PUT': (WAREHOUSE_STOCK, EDIT),
                                   'PATCH': (WAREHOUSE_STOCK, EDIT), 'DELETE': (WAREHOUSE_STOCK, DELETE)},
    'api-batch-list': (ALERTS, VIEW), 'api-batch-detail': (ALERTS, VIEW),
    'api-stock-movements': (FLOOR_STOCK, VIEW),

    'transfer_list': (WITHDRAWALS, VIEW), 'transfer_detail': (WITHDRAWALS, VIEW),
    'transfer_create': (WITHDRAWALS, ADD), 'transfer_approve': (WITHDRAWALS, EDIT),
    'transfer_complete': (WITHDRAWALS, EDIT), 'transfer_scan': (WITHDRAWALS, EDIT),
    'transfer_confirm_receipt': (WITHDRAWALS, EDIT),
    'transfer_flag_discrepancy': (WITHDRAWALS, EDIT),
    'api-stock-transfer-list': {'GET': (WITHDRAWALS, VIEW), 'HEAD': (WITHDRAWALS, VIEW),
                                'OPTIONS': (WITHDRAWALS, VIEW), 'POST': (WITHDRAWALS, ADD)},
    'api-stock-transfer-detail': (WITHDRAWALS, VIEW),
    'api-stock-transfer-approve': (WITHDRAWALS, EDIT),
    'api-stock-transfer-release': (WITHDRAWALS, EDIT),
    'api-stock-transfer-acknowledge': (WITHDRAWALS, EDIT),
    'alerts_center': (ALERTS, VIEW), 'expiry_write_off': (ALERTS, DELETE),

    'transaction_list': (SALES_POS, VIEW), 'transaction_detail': (SALES_POS, VIEW),
    'transaction_receipt': (SALES_POS, VIEW), 'transaction_add': (SALES_POS, ADD),
    'transaction_set_status': (SALES_POS, EDIT),
    'transaction_apply_discount': (SALES_POS, EDIT),
    'transaction_decide_pwd': (SALES_POS, EDIT),
    'api-sales-list': {'GET': (SALES_POS, VIEW), 'HEAD': (SALES_POS, VIEW),
                       'OPTIONS': (SALES_POS, VIEW), 'POST': (SALES_POS, ADD)},
    'api-sales-detail': (SALES_POS, VIEW), 'api-sales-pwd-verification': (SALES_POS, EDIT),
    'billing_list': (BILLING, VIEW), 'billing_detail': (BILLING, VIEW),
    'billing_proof': (BILLING, VIEW), 'payment_proof_download': (BILLING, VIEW),
    'billing_create': (BILLING, ADD), 'billing_confirm': (BILLING, EDIT),
    'api-billing-list': (BILLING, VIEW), 'api-billing-detail': (BILLING, VIEW),
    'api-billing-pay': (BILLING, ADD), 'api-billing-confirm': (BILLING, EDIT),

    'procurement_list': (PROCUREMENT, VIEW), 'procurement_detail': (PROCUREMENT, VIEW),
    'procurement_print': (PROCUREMENT, VIEW), 'procurement_pdf': (PROCUREMENT, VIEW),
    'procurement_add': (PROCUREMENT, ADD),
    'procurement_update_status': (PROCUREMENT, EDIT), 'procurement_approve': (PROCUREMENT, EDIT),
    'supplier_list': (PROCUREMENT, VIEW), 'supplier_detail': (PROCUREMENT, VIEW),
    'supplier_add': (PROCUREMENT, ADD), 'supplier_edit': (PROCUREMENT, EDIT),
    'supplier_delete': (PROCUREMENT, DELETE), 'supplier_product_add': (PROCUREMENT, EDIT),
    'supplier_product_remove': (PROCUREMENT, EDIT),
    'supplier_product_transfer': (PROCUREMENT, EDIT),
    'api-procurement-list': {'GET': (PROCUREMENT, VIEW), 'HEAD': (PROCUREMENT, VIEW),
                             'OPTIONS': (PROCUREMENT, VIEW), 'POST': (PROCUREMENT, ADD)},
    'api-procurement-detail': {'GET': (PROCUREMENT, VIEW), 'HEAD': (PROCUREMENT, VIEW),
                               'OPTIONS': (PROCUREMENT, VIEW), 'DELETE': (PROCUREMENT, DELETE)},
    'api-procurement-status': (PROCUREMENT, EDIT),
    'api-procurement-suppliers': (PROCUREMENT, VIEW),
    'api-procurement-supplier-products': (PROCUREMENT, VIEW),
    'delivery_list': (DELIVERIES, VIEW), 'delivery_receive': (DELIVERIES, EDIT),
    'delivery_scan': (DELIVERIES, EDIT), 'delivery_add_unexpected': (DELIVERIES, EDIT),

    'reports_dashboard': (REPORTS, VIEW), 'sales_report': (REPORTS, VIEW),
    'inventory_report': (REPORTS, VIEW), 'procurement_report': (REPORTS, VIEW),
    'billing_report': (REPORTS, VIEW), 'export_csv': (REPORTS, VIEW),
    'api-reports-dashboard': (REPORTS, VIEW), 'api-reports-sales': (REPORTS, VIEW),
    'api-reports-inventory': (REPORTS, VIEW), 'api-reports-procurement': (REPORTS, VIEW),
}

PROTECTED_URL_NAMES = frozenset(ROUTE_ACTIONS)

# Some URLs intentionally have a narrower role policy than their module/action
# (for example, Staff may view Products but only Owner/Manager may export).
# Their existing view checks retain the original denial path and message.
ROUTE_ROLE_CEILINGS = {
    'product_export_xlsx': {'OWNER', 'BRANCH_MANAGER'},
    'product_import_upload': {'OWNER', 'BRANCH_MANAGER'},
    'product_import_map': {'OWNER', 'BRANCH_MANAGER'},
    'product_import_preview': {'OWNER', 'BRANCH_MANAGER'},
    'product_import_cancel': {'OWNER', 'BRANCH_MANAGER'},
    'opening_stock_import': {'OWNER'}, 'opening_stock_confirm': {'OWNER'},
    'opening_stock_cancel': {'OWNER'}, 'opening_stock_template': {'OWNER'},
    'opening_stock_import_history': {'OWNER'},
    'opening_stock_import_detail': {'OWNER'}, 'opening_stock_import_reverse': {'OWNER'},
    'procurement_approve': {'OWNER'},
    'supplier_add': {'OWNER'}, 'supplier_edit': {'OWNER'}, 'supplier_delete': {'OWNER'},
    'supplier_product_add': {'OWNER'}, 'supplier_product_remove': {'OWNER'},
    'supplier_product_transfer': {'OWNER'},
    'export_csv': {'OWNER', 'BRANCH_MANAGER'},
}


def route_role_allows(url_name, role):
    roles = ROUTE_ROLE_CEILINGS.get(url_name)
    return roles is None or role in roles


def permission_for_request(url_name, method):
    rule = ROUTE_ACTIONS.get(url_name, False)
    if isinstance(rule, dict):
        return rule.get(method.upper(), False)
    return rule


# Backwards-compatible display shape.
MODULE_REGISTRY = {
    code: (label, [name for name, rule in ROUTE_ACTIONS.items()
                   if (isinstance(rule, tuple) and rule[0] == code)
                   or (isinstance(rule, dict) and any(v and v[0] == code for v in rule.values()))])
    for code, label in MODULE_LABELS.items()
}

URL_NAME_TO_MODULE = {
    name: next((v[0] for v in ([rule] if isinstance(rule, tuple) else rule.values()) if v), None)
    for name, rule in ROUTE_ACTIONS.items()
}
