"""What the audit log tracks, and what it must never write down.

Only models listed here are captured. Anything else (sessions, tokens,
notifications, the stock ledger — which is itself an append-only history, and
would just be logged twice) is deliberately absent.

Per model:
  * mask    — personal-data fields. A change is still recorded ("email
              changed") but the values are replaced, so an erased account's
              identity cannot survive in the log (see hard_delete_personal_data).
  * exclude — fields not tracked at all: secrets, and counters that change on
              every sale and are already covered by the stock ledger.
  * repr_field — the attribute that names a record in the log. Only ever a
              non-personal one; anything else reads "<Label> #<id>".
  * branch_path — dotted attribute path to the record's branch id, so the log
              can be narrowed by branch later.

File fields (payment proofs, avatars, images) are always masked as "[file]" —
the log records that a file was set or replaced, never its contents or name.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class AuditConfig:
    label: str
    repr_field: str = ''
    mask: frozenset = frozenset()
    exclude: frozenset = frozenset()
    branch_path: str = 'branch_id'


def _f(*names):
    return frozenset(names)


# Never tracked on any model: credentials, and bookkeeping that changes on
# every save without meaning anything happened.
GLOBAL_EXCLUDE = _f('password', 'last_login', 'updated_at')

MASK_PERSONAL = '[personal data]'
MASK_FILE = '[file]'


REGISTRY = {
    # --- accounts ----------------------------------------------------------
    'accounts.User': AuditConfig(
        'User account',
        mask=_f('username', 'first_name', 'last_name', 'email', 'pending_email', 'avatar'),
        branch_path='',
    ),
    'accounts.OwnerProfile': AuditConfig(
        'Owner profile', mask=_f('owner_name', 'contact_number'), branch_path='',
    ),
    'accounts.BranchManagerProfile': AuditConfig(
        'Manager profile', mask=_f('manager_name', 'contact_number'),
    ),
    'accounts.StaffProfile': AuditConfig(
        'Staff profile', mask=_f('staff_name', 'contact_number'),
    ),
    'accounts.SupplierProfile': AuditConfig(
        'Supplier profile',
        mask=_f('supplier_name', 'contact_number', 'contact_person', 'address'),
    ),
    'accounts.SupplierCategory': AuditConfig(
        'Supplier category', repr_field='name', branch_path='',
    ),
    'accounts.CustomerProfile': AuditConfig(
        'Customer profile',
        mask=_f('first_name', 'last_name', 'contact_number', 'address'),
        branch_path='',
    ),
    'accounts.Address': AuditConfig(
        'Saved address',
        mask=_f('text', 'region', 'province', 'city', 'barangay',
                'street_detail', 'latitude', 'longitude'),
        branch_path='',
    ),
    # --- branches ------------------------------------------------------------
    'branches.Branch': AuditConfig('Branch', repr_field='branch_name', branch_path='id'),
    # --- inventory -----------------------------------------------------------
    'inventory.ProductCategory': AuditConfig(
        'Category', repr_field='category_name', branch_path='',
    ),
    'inventory.Product': AuditConfig('Product', repr_field='product_name', branch_path=''),
    'inventory.ProductPriceTier': AuditConfig('Price tier', branch_path=''),
    'inventory.ProductImage': AuditConfig('Product image', branch_path=''),
    'inventory.Warehouse': AuditConfig('Warehouse', repr_field='name'),
    'inventory.WarehouseStock': AuditConfig(
        'Warehouse stock',
        exclude=_f('quantity_on_hand', 'last_updated', 'restock_flag'),
        branch_path='warehouse.branch_id',
    ),
    'inventory.Inventory': AuditConfig(
        'Floor stock',
        exclude=_f('quantity_on_hand', 'last_stock_update', 'restock'),
    ),
    'inventory.Batch': AuditConfig(
        'Stock batch', repr_field='batch_number', exclude=_f('quantity'),
    ),
    'inventory.StockTransfer': AuditConfig(
        'Stock transfer', repr_field='slip_number', branch_path='source_branch_id',
    ),
    'inventory.StockTransferItem': AuditConfig(
        'Transfer item', branch_path='transfer.source_branch_id',
    ),
    'inventory.OpeningStockImport': AuditConfig(
        'Opening stock import', repr_field='original_filename', branch_path='',
    ),
    # StockCountEvent and StockCountScanLog are absent on purpose: both are
    # append-only histories already, like the stock ledger.
    'inventory.StockCount': AuditConfig('Stock count', repr_field='count_number'),
    'inventory.StockCountLine': AuditConfig(
        'Stock count line', branch_path='count.branch_id',
    ),
    # --- procurement ---------------------------------------------------------
    'procurement.Procurement': AuditConfig('Purchase order', repr_field='po_number'),
    'procurement.ProcurementItem': AuditConfig(
        'Purchase order item', branch_path='procurement.branch_id',
    ),
    'procurement.DeliveryReceipt': AuditConfig(
        'Delivery receipt', branch_path='procurement.branch_id',
        mask=_f('delivered_by'),
    ),
    'procurement.SupplierReturn': AuditConfig(
        'Supplier return', repr_field='return_number',
    ),
    'procurement.SupplierReturnItem': AuditConfig(
        'Supplier return item', branch_path='supplier_return.branch_id',
    ),
    'procurement.SupplierProduct': AuditConfig('Supplier product', branch_path=''),
    'procurement.SupplierProductRequest': AuditConfig(
        'Supplier product request', repr_field='product_name', branch_path='',
    ),
    # --- sales ---------------------------------------------------------------
    'sales.SalesTransaction': AuditConfig(
        'Order', repr_field='order_number',
        # pwd_request_* is what the customer typed at checkout (P15) — the
        # same ID number and name as discount_*, and just as private.
        mask=_f('delivery_address', 'discount_id_number', 'discount_holder_name',
                'pwd_request_id_number', 'pwd_request_holder_name', 'pos_cashier_id'),
    ),
    'sales.SalesItem': AuditConfig('Order item', branch_path='transaction.branch_id'),
    'sales.PosSalesImport': AuditConfig(
        'ZANK POS sales import', repr_field='original_filename',
    ),
    'procurement.DeliveryReceiptItem': AuditConfig(
        'Delivery receipt item', branch_path='receipt.procurement.branch_id',
    ),
    'sales.Refund': AuditConfig('Refund', branch_path='transaction.branch_id'),
    'sales.RefundItem': AuditConfig(
        'Refund item', branch_path='refund.transaction.branch_id',
    ),
    # --- billing -------------------------------------------------------------
    'billing.BillingRecord': AuditConfig(
        'Payment record', branch_path='transaction.branch_id',
    ),
    # --- core ----------------------------------------------------------------
    'core.StoreSetting': AuditConfig('Store settings', branch_path=''),
    'core.HomeHeroSlide': AuditConfig('Homepage slide', branch_path=''),
    # --- backups (KATRINA-BACKUP-002) -----------------------------------------
    # Not branch-scoped — a backup covers the whole database. reap_stuck_runs'
    # bulk .update() of an abandoned run is not captured (see the module
    # docstring's "Known limits" — QuerySet.update() emits no signals); every
    # run still gets its own create + at least one save()-driven update row
    # from _finish(), so the lifecycle and who triggered it stay visible here.
    'backups.BackupRun': AuditConfig('Backup run', branch_path=''),
    'backups.RestoreTestRecord': AuditConfig(
        'Restore test', repr_field='backup_used', branch_path='',
    ),
}


def entity_choices():
    """(entity key, friendly label) pairs for the viewer's filter, by label."""
    return sorted(((key, cfg.label) for key, cfg in REGISTRY.items()), key=lambda kv: kv[1])


def label_for(entity):
    cfg = REGISTRY.get(entity)
    return cfg.label if cfg else entity
