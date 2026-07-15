from django.db import migrations, models


def populate_order_numbers(apps, schema_editor):
    """
    Populate order_number for every existing SalesTransaction row
    before the unique constraint is applied.
    """
    SalesTransaction = apps.get_model('sales', 'SalesTransaction')
    for transaction in SalesTransaction.objects.all().order_by('id'):
        year = transaction.transaction_date.year if transaction.transaction_date else 2025
        transaction.order_number = f"ORD-{year}-{str(transaction.id).zfill(3)}"
        transaction.save(update_fields=['order_number'])


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0001_initial'),
    ]

    operations = [
        # delivery_address is ALREADY in the database from the partial
        # first run — we only record it in Django's state here without
        # touching the actual table (state_operations vs operations).
        # This is the standard Django pattern for recovering from a
        # partially applied migration.
        migrations.SeparateDatabaseAndState(
            database_operations=[],   # don't touch the DB — column exists
            state_operations=[
                migrations.AddField(
                    model_name='salestransaction',
                    name='delivery_address',
                    field=models.TextField(
                        blank=True,
                        help_text='Required for Online orders; blank for Physical'
                    ),
                ),
            ],
        ),

        # order_status — not in DB yet, add normally
        migrations.AddField(
            model_name='salestransaction',
            name='order_status',
            field=models.CharField(
                choices=[
                    ('Pending', 'Pending'),
                    ('Approved', 'Approved'),
                    ('Preparing', 'Preparing'),
                    ('Completed', 'Completed'),
                    ('Cancelled', 'Cancelled'),
                ],
                default='Pending',
                help_text='Order lifecycle status',
                max_length=10,
            ),
        ),

        # order_type — not in DB yet, add normally
        migrations.AddField(
            model_name='salestransaction',
            name='order_type',
            field=models.CharField(
                choices=[('Retail', 'Retail'), ('Wholesale', 'Wholesale')],
                default='Retail',
                help_text='Retail uses unit_price; Wholesale uses wholesale_price',
                max_length=10,
            ),
        ),

        # order_number — add without unique=True first to avoid duplicate
        # empty string error on existing rows
        migrations.AddField(
            model_name='salestransaction',
            name='order_number',
            field=models.CharField(
                blank=True,
                help_text='Auto-generated e.g. ORD-2026-001',
                max_length=20,
                unique=False,
            ),
        ),

        # Populate every existing row with a unique order_number
        migrations.RunPython(
            populate_order_numbers,
            reverse_code=migrations.RunPython.noop,
        ),

        # Now safe to enforce unique=True
        migrations.AlterField(
            model_name='salestransaction',
            name='order_number',
            field=models.CharField(
                blank=True,
                help_text='Auto-generated e.g. ORD-2026-001',
                max_length=20,
                unique=True,
            ),
        ),
    ]