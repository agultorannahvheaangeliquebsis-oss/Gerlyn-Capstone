# Generated manually for the operational notification matrix.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0020_user_deleted_at_user_deleted_by_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='notification',
            name='event_type',
            field=models.CharField(
                blank=True,
                choices=[
                    ('', 'General'),
                    ('low_stock', 'Low stock'),
                    ('near_expiry', 'Near expiry'),
                    ('po_approved', 'Purchase order approved'),
                    ('incoming_delivery', 'Incoming delivery'),
                    ('order_delivered', 'Order delivered'),
                    ('payment_verified', 'Payment verified'),
                    ('payment_rejected', 'Payment rejected'),
                ],
                db_index=True,
                default='',
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name='notification',
            name='read_at',
            field=models.DateTimeField(
                blank=True,
                help_text='When the recipient first marked this notification read.',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='notification',
            name='recipient_role',
            field=models.CharField(
                blank=True,
                choices=[
                    ('OWNER', 'Owner'),
                    ('BRANCH_MANAGER', 'Branch Manager'),
                    ('STAFF', 'Store Staff'),
                    ('SUPPLIER', 'Supplier'),
                    ('CUSTOMER', 'Customer'),
                ],
                default='',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='notification',
            name='reference_id',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='notification',
            name='reference_type',
            field=models.CharField(blank=True, default='', max_length=40),
        ),
    ]
