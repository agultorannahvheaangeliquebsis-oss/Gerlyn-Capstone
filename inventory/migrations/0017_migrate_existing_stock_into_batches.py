from django.db import migrations
from django.utils import timezone


def migrate_existing_stock_into_batches(apps, schema_editor):
    """One-time backfill: every Inventory/WarehouseStock row that already
    carries stock gets exactly one Batch, carrying that row's current
    quantity and whatever expiry_date it already had (often none) — so
    existing stock is batch-tracked from this point on, same as anything
    received after this ships. Rows at zero quantity get no batch; there is
    nothing to track a lot of.

    batch_number is set explicitly here rather than relying on Batch.save()
    — the historical model apps.get_model() hands back has no custom
    methods, only the fields, so the app's own auto-numbering logic has to
    be reproduced by hand.
    """
    Inventory = apps.get_model('inventory', 'Inventory')
    WarehouseStock = apps.get_model('inventory', 'WarehouseStock')
    Batch = apps.get_model('inventory', 'Batch')
    year = timezone.now().year

    def create_batch(**kwargs):
        batch = Batch.objects.create(batch_number='', **kwargs)
        batch.batch_number = f'BAT-{year}-{str(batch.pk).zfill(3)}'
        batch.save(update_fields=['batch_number'])

    for inv in Inventory.objects.filter(quantity_on_hand__gt=0):
        create_batch(
            product_id=inv.product_id,
            branch_id=inv.branch_id,
            location='Floor',
            quantity=inv.quantity_on_hand,
            received_date=inv.last_stock_update.date() if inv.last_stock_update else timezone.now().date(),
            expiry_date=inv.expiry_date,
            notes='Migrated from pre-batch-tracking stock.',
        )

    for ws in WarehouseStock.objects.select_related('warehouse').filter(quantity_on_hand__gt=0):
        create_batch(
            product_id=ws.product_id,
            branch_id=ws.warehouse.branch_id,
            location='Warehouse',
            quantity=ws.quantity_on_hand,
            received_date=ws.last_updated.date() if ws.last_updated else timezone.now().date(),
            expiry_date=ws.expiry_date,
            notes='Migrated from pre-batch-tracking stock.',
        )


def noop_reverse(apps, schema_editor):
    """Deliberately not reversed: deleting the migrated batches on a
    rollback would silently destroy real (if newly-created) records with no
    way to tell them apart from ones added since. Rolling back this
    migration leaves the batches in place — harmless, since the aggregate
    Inventory/WarehouseStock rows they were built from are untouched."""


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0016_batch_location_and_migrate_existing_stock'),
    ]

    operations = [
        migrations.RunPython(migrate_existing_stock_into_batches, noop_reverse),
    ]
