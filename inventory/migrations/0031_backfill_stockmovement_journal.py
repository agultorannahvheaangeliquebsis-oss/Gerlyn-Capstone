"""Backfill StockMovement.product / branch / reference_type for existing rows.

Every value is derived from data the row already carries - nothing is guessed:

* product / branch come from the stock row (Inventory, or WarehouseStock via
  its warehouse). A row whose stock row was later deleted (both FKs NULL) falls
  back to its batch, which knows its own product and branch. A row with neither
  is left NULL: the information is genuinely gone.
* reference_type comes from movement_type, which fixes what reference_id is for
  every type except ADJUSTMENT. Adjustments are told apart by the note text
  their writers have always used ("Voided order ..." / "Discrepancy on ...");
  an adjustment with no reference_id (a manual count correction) or an
  unrecognised note keeps a blank type rather than a wrong one.

Rows are only updated where the new column is still empty, so the migration is
safe to re-run and never overwrites a value a writer already set.
"""
from django.db import migrations
from django.db.models import OuterRef, Subquery

# movement_type value -> reference_type value (mirrors
# StockMovement._REFERENCE_TYPE_BY_MOVEMENT at the time of writing; inlined so
# the migration keeps meaning the same thing if the model constants change).
REFERENCE_TYPE_BY_MOVEMENT = {
    'Sale': 'sales_transaction',
    'Customer Return': 'sales_transaction',
    'Procurement Received': 'procurement',
    'Supplier Return': 'supplier_return',
    'Warehouse to Floor': 'stock_transfer',
    'Floor to Warehouse': 'stock_transfer',
    'Manual Out': 'stock_transfer',
    'Expiry Write-off': 'batch',
    'Opening Balance': 'opening_stock_import',
    'Opening Reversal': 'opening_stock_import',
}
ADJUSTMENT_NOTE_PREFIXES = (
    ('Voided order', 'sales_transaction'),
    ('Discrepancy on', 'stock_transfer'),
)


def backfill_journal_columns(apps, schema_editor):
    StockMovement = apps.get_model('inventory', 'StockMovement')
    Inventory = apps.get_model('inventory', 'Inventory')
    WarehouseStock = apps.get_model('inventory', 'WarehouseStock')
    Batch = apps.get_model('inventory', 'Batch')

    movements = StockMovement.objects

    # Floor rows: product and branch are on the Inventory row itself.
    floor = Inventory.objects.filter(pk=OuterRef('inventory_id'))
    movements.filter(inventory__isnull=False, product__isnull=True).update(
        product_id=Subquery(floor.values('product_id')[:1]))
    movements.filter(inventory__isnull=False, branch__isnull=True).update(
        branch_id=Subquery(floor.values('branch_id')[:1]))

    # Warehouse rows: product is on WarehouseStock, branch is on its warehouse.
    warehouse = WarehouseStock.objects.filter(pk=OuterRef('warehouse_stock_id'))
    movements.filter(warehouse_stock__isnull=False, product__isnull=True).update(
        product_id=Subquery(warehouse.values('product_id')[:1]))
    movements.filter(warehouse_stock__isnull=False, branch__isnull=True).update(
        branch_id=Subquery(warehouse.values('warehouse__branch_id')[:1]))

    # Stock row since deleted: the lot still names its product and branch.
    lot = Batch.objects.filter(pk=OuterRef('batch_id'))
    movements.filter(batch__isnull=False, product__isnull=True).update(
        product_id=Subquery(lot.values('product_id')[:1]))
    movements.filter(batch__isnull=False, branch__isnull=True).update(
        branch_id=Subquery(lot.values('branch_id')[:1]))

    # reference_type. Only rows that carry a reference_id have one to name.
    unset = movements.filter(reference_id__isnull=False, reference_type='')
    for movement_type, reference_type in REFERENCE_TYPE_BY_MOVEMENT.items():
        unset.filter(movement_type=movement_type).update(
            reference_type=reference_type)
    for prefix, reference_type in ADJUSTMENT_NOTE_PREFIXES:
        unset.filter(
            movement_type='Adjustment', notes__startswith=prefix,
        ).update(reference_type=reference_type)


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0030_stockmovement_journal_columns'),
    ]

    operations = [
        # Reversing just leaves the (nullable / blank) columns populated; the
        # schema migration before this one is what removes them.
        migrations.RunPython(backfill_journal_columns, migrations.RunPython.noop),
    ]
