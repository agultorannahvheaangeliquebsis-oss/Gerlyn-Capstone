from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0027_supplier_business_registration_id'),
        ('inventory', '0030_separate_transfer_writeoff_domains'),
        ('procurement', '0014_optional_supplier_participation'),
    ]

    operations = [
        migrations.AddField(
            model_name='procurement', name='submission_key',
            field=models.CharField(
                blank=True, null=True, unique=True, max_length=64,
                help_text='Client submission token used to prevent duplicate purchase orders.',
            ),
        ),
        migrations.AddField(
            model_name='deliveryreceipt', name='outcome',
            field=models.CharField(
                choices=[('Accepted', 'Accepted / received'), ('Rejected', 'Failed / rejected')],
                default='Accepted', max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='deliveryreceipt', name='rejection_reason',
            field=models.CharField(
                blank=True, max_length=300,
                help_text='Required when the delivery is rejected and no stock is accepted.',
            ),
        ),
        migrations.AddField(
            model_name='deliveryreceipt', name='idempotency_key',
            field=models.CharField(
                blank=True, null=True, unique=True, max_length=64,
                help_text='Client submission token used to apply a delivery event once.',
            ),
        ),
        migrations.CreateModel(
            name='DeliveryReceiptItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity_received', models.PositiveIntegerField()),
                ('batch', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='delivery_receipt_item', to='inventory.batch')),
                ('procurement_item', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='receipt_items', to='procurement.procurementitem')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='delivery_receipt_items', to='inventory.product')),
                ('receipt', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='procurement.deliveryreceipt')),
            ],
            options={
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('quantity_received__gt', 0)), name='deliveryreceiptitem_quantity_positive'),
                ],
            },
        ),
        migrations.AddField(
            model_name='supplierreturnitem', name='source_receipt_item',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='supplier_return_items', to='procurement.deliveryreceiptitem',
                help_text='The original delivery line these returned units came from.',
            ),
        ),
    ]
