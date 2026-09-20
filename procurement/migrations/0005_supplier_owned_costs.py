from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('procurement', '0004_procurement_authorized_at'),
    ]

    operations = [
        migrations.RenameField(
            model_name='supplierproduct',
            old_name='default_cost_price',
            new_name='current_cost_price',
        ),
        migrations.RenameField(
            model_name='procurementitem',
            old_name='cost_price',
            new_name='unit_cost_snapshot',
        ),
        migrations.AlterField(
            model_name='supplierproduct',
            name='current_cost_price',
            field=models.DecimalField(
                blank=True, decimal_places=2,
                help_text='Supplier current cost, copied when a PO is confirmed.',
                max_digits=10, null=True,
            ),
        ),
        migrations.AlterField(
            model_name='procurementitem',
            name='unit_cost_snapshot',
            field=models.DecimalField(
                blank=True, decimal_places=2, editable=False,
                help_text='Supplier cost copied when this purchase order is confirmed.',
                max_digits=10, null=True,
            ),
        ),
    ]
