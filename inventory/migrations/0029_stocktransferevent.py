from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0028_supplier_returns'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='StockTransferEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('from_status', models.CharField(blank=True, max_length=15)),
                ('to_status', models.CharField(choices=[('Pending', 'Pending'), ('Approved', 'Approved'), ('Rejected', 'Rejected'), ('Released', 'Released'), ('Acknowledged', 'Acknowledged'), ('Discrepancy', 'Discrepancy')], max_length=15)),
                ('note', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='stock_transfer_events', to=settings.AUTH_USER_MODEL)),
                ('transfer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='inventory.stocktransfer')),
            ],
            options={'ordering': ['created_at', 'pk']},
        ),
    ]
