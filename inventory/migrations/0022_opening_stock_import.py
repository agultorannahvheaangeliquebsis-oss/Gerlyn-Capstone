import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0021_price_tiers'),
        ('branches', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='OpeningStockImport',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('original_filename', models.CharField(max_length=255)),
                ('raw_sha256', models.CharField(db_index=True, max_length=64)),
                ('canonical_sha256', models.CharField(max_length=64)),
                ('committed_fingerprint', models.CharField(help_text='Canonical normalized-file hash; retained after reversal.', max_length=64, unique=True)),
                ('as_of_date', models.DateField()),
                ('status', models.CharField(choices=[('Committed', 'Committed'), ('Reversed', 'Reversed')], default='Committed', max_length=15)),
                ('row_count', models.PositiveIntegerField(default=0)),
                ('product_count', models.PositiveIntegerField(default=0)),
                ('branch_count', models.PositiveIntegerField(default=0)),
                ('total_quantity', models.PositiveBigIntegerField(default=0)),
                ('warning_count', models.PositiveIntegerField(default=0)),
                ('warnings_acknowledged', models.BooleanField(default=False)),
                ('confirmed_at', models.DateTimeField(auto_now_add=True)),
                ('reversed_at', models.DateTimeField(blank=True, null=True)),
                ('reversal_reason', models.TextField(blank=True)),
                ('uploaded_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='opening_stock_imports', to=settings.AUTH_USER_MODEL)),
                ('reversed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reversed_opening_stock_imports', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-confirmed_at'],
                'indexes': [models.Index(fields=['-confirmed_at'], name='openstock_confirmed_idx')],
            },
        ),
        migrations.CreateModel(
            name='OpeningStockImportRow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('row_number', models.PositiveIntegerField()),
                ('normalized_hash', models.CharField(max_length=64)),
                ('product_name_snapshot', models.CharField(max_length=150)),
                ('barcode_snapshot', models.CharField(max_length=64)),
                ('branch_name_snapshot', models.CharField(max_length=100)),
                ('quantity', models.PositiveIntegerField()),
                ('external_lot_code', models.CharField(blank=True, max_length=100)),
                ('received_date', models.DateField()),
                ('expiry_date', models.DateField(blank=True, null=True)),
                ('previous_quantity', models.PositiveIntegerField()),
                ('resulting_quantity', models.PositiveIntegerField()),
                ('warnings', models.JSONField(blank=True, default=list)),
                ('reversed_quantity', models.PositiveIntegerField(default=0)),
                ('consumed_quantity', models.PositiveIntegerField(default=0)),
                ('branch', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='opening_stock_rows', to='branches.branch')),
                ('import_batch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='rows', to='inventory.openingstockimport')),
                ('product', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='opening_stock_rows', to='inventory.product')),
            ],
            options={
                'ordering': ['row_number'],
                'constraints': [models.UniqueConstraint(fields=('import_batch', 'row_number'), name='unique_opening_import_row_number')],
            },
        ),
        migrations.AddField(
            model_name='batch',
            name='external_lot_code',
            field=models.CharField(blank=True, help_text="Manufacturer/supplier lot code printed on the product. Separate from batch_number, which is the system's own ID.", max_length=100),
        ),
        migrations.AddField(
            model_name='batch',
            name='opening_import_row',
            field=models.ForeignKey(blank=True, help_text='Opening-stock row that created this lot, when applicable.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='batches', to='inventory.openingstockimportrow'),
        ),
        migrations.AddField(
            model_name='stockmovement',
            name='opening_import_row',
            field=models.ForeignKey(blank=True, help_text='Opening-stock row that caused this movement, when applicable.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='movements', to='inventory.openingstockimportrow'),
        ),
        migrations.AlterField(
            model_name='stockmovement',
            name='movement_type',
            field=models.CharField(choices=[('Sale', 'Sale (Floor Out)'), ('Procurement Received', 'Procurement Received (Warehouse In)'), ('Warehouse to Floor', 'Warehouse to Floor (Transfer Out/In)'), ('Floor to Warehouse', 'Floor to Warehouse (Return)'), ('Manual In', 'Manual Stock In'), ('Manual Out', 'Manual Stock Out'), ('Adjustment', 'Stock Adjustment'), ('Opening Balance', 'Opening Balance'), ('Opening Reversal', 'Opening Balance Reversal')], max_length=30),
        ),
    ]
