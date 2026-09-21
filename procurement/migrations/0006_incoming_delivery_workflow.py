from django.db import migrations, models
from django.db.models import Q


def migrate_ordered_statuses(apps, schema_editor):
    Procurement = apps.get_model('procurement', 'Procurement')
    Procurement.objects.filter(
        status='Ordered', delivery_date__isnull=True,
    ).update(status='Approved')
    Procurement.objects.filter(
        status='Ordered', delivery_date__isnull=False,
    ).update(status='Incoming')


def restore_ordered_statuses(apps, schema_editor):
    Procurement = apps.get_model('procurement', 'Procurement')
    Procurement.objects.filter(
        Q(status='Approved') | Q(status='Incoming') | Q(status='Partially Received')
    ).update(status='Ordered')


class Migration(migrations.Migration):

    dependencies = [
        ('procurement', '0005_supplier_owned_costs'),
    ]

    operations = [
        migrations.AddField(
            model_name='procurement',
            name='actual_receipt_date',
            field=models.DateField(
                blank=True,
                help_text='Date goods were most recently received against this PO.',
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name='procurement',
            name='status',
            field=models.CharField(
                choices=[
                    ('Draft', 'Draft'),
                    ('Approved', 'Approved'),
                    ('Incoming', 'Incoming'),
                    ('Partially Received', 'Partially Received'),
                    ('Received', 'Received'),
                    ('Cancelled', 'Cancelled'),
                ],
                default='Draft',
                max_length=20,
            ),
        ),
        migrations.RunPython(migrate_ordered_statuses, restore_ordered_statuses),
    ]
