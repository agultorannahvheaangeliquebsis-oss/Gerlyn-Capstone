from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('procurement', '0013_supplier_returns'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='procurement',
            name='status',
            field=models.CharField(
                choices=[
                    ('Draft', 'Draft'), ('Approved', 'Approved'),
                    ('Pending Owner Review', 'Pending Owner Review'),
                    ('Pending Supplier Review', 'Pending Supplier Review'),
                    ('Terms Agreed', 'Terms Agreed'),
                    ('Communicated/Ordered', 'Communicated/Ordered'),
                    ('Incoming', 'Incoming'),
                    ('Partially Received', 'Partially Received'),
                    ('Received', 'Received'), ('Cancelled', 'Cancelled'),
                ],
                default='Draft', max_length=24,
            ),
        ),
        migrations.AddField(
            model_name='procurement', name='communication_method',
            field=models.CharField(
                blank=True,
                choices=[
                    ('Phone', 'Phone call'), ('Message', 'Messaging / chat'),
                    ('Email', 'Email'), ('Other', 'Other'),
                ],
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name='procurement', name='communication_note',
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name='procurement', name='communicated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='procurement', name='communicated_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='communicated_procurements', to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='procurement', name='supplier_acknowledged_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
