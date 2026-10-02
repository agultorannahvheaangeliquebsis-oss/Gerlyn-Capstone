from django.db import migrations, models
from django.db.models import Q


def classify_existing_movements(apps, schema_editor):
    StockMovement = apps.get_model('inventory', 'StockMovement')
    StockTransfer = apps.get_model('inventory', 'StockTransfer')

    transfer_ids = StockTransfer.objects.filter(
        purpose='Branch Restock',
    ).values_list('pk', flat=True)
    write_off_ids = StockTransfer.objects.exclude(
        purpose='Branch Restock',
    ).values_list('pk', flat=True)

    StockMovement.objects.filter(
        movement_type='Adjustment',
        reference_id__in=transfer_ids,
        notes__startswith='Discrepancy on ',
    ).update(movement_type='Transfer Discrepancy')

    StockMovement.objects.filter(
        Q(notes__startswith='Withdrawal Slip ') |
        Q(notes__startswith='Stock withdrawal '),
        movement_type='Manual Out',
        reference_id__in=write_off_ids,
    ).update(movement_type='Write-off')


def restore_legacy_movement_types(apps, schema_editor):
    StockMovement = apps.get_model('inventory', 'StockMovement')
    StockMovement.objects.filter(
        movement_type='Transfer Discrepancy',
    ).update(movement_type='Adjustment')
    StockMovement.objects.filter(
        movement_type='Write-off',
    ).update(movement_type='Manual Out')


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0029_stocktransferevent'),
    ]

    operations = [
        migrations.CreateModel(
            name='StockWriteOff',
            fields=[],
            options={
                'verbose_name': 'Stock write-off',
                'verbose_name_plural': 'Stock write-offs',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('inventory.stocktransfer',),
        ),
        migrations.CreateModel(
            name='WarehouseFloorTransfer',
            fields=[],
            options={
                'verbose_name': 'Warehouse-to-floor transfer',
                'verbose_name_plural': 'Warehouse-to-floor transfers',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('inventory.stocktransfer',),
        ),
        migrations.AlterField(
            model_name='stockmovement',
            name='movement_type',
            field=models.CharField(
                choices=[
                    ('Sale', 'Sale (Floor Out)'),
                    ('Procurement Received', 'Procurement Received (Warehouse In)'),
                    ('Warehouse to Floor', 'Warehouse to Floor (Transfer Out/In)'),
                    ('Floor to Warehouse', 'Floor to Warehouse (Return)'),
                    ('Manual In', 'Manual Stock In'),
                    ('Manual Out', 'Manual Stock Out'),
                    ('Adjustment', 'Stock Adjustment'),
                    ('Write-off', 'Stock Write-off'),
                    ('Transfer Discrepancy', 'Transfer Receipt Discrepancy'),
                    ('Opening Balance', 'Opening Balance'),
                    ('Opening Reversal', 'Opening Balance Reversal'),
                    ('Expiry Write-off', 'Expiry Write-off'),
                    ('Customer Return', 'Customer Return (Refund)'),
                    ('Supplier Return', 'Supplier Return (Warehouse Out)'),
                ],
                max_length=30,
            ),
        ),
        migrations.RunPython(
            classify_existing_movements,
            restore_legacy_movement_types,
        ),
    ]
