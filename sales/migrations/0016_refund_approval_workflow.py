# P14 — refund approval workflow: a Refund becomes a request that an Owner or
# Branch Manager approves or rejects, with reason codes, an append-only event
# log (RefundEvent), an order status trail (OrderStatusHistory) and a running
# SalesTransaction.refunded_amount.
#
# Every Refund that exists before this migration was issued instantly by an
# Owner/Branch Manager and its stock/billing effects already happened, so each
# is carried over as Approved, requested and approved by whoever issued it.
# The new non-null columns are added with the value that is true for those
# rows (Approved / Other / Partial) and then have that default dropped or
# reset to what a NEW row should start as.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import Sum


def carry_over_existing_refunds(apps, schema_editor):
    Refund = apps.get_model('sales', 'Refund')
    RefundEvent = apps.get_model('sales', 'RefundEvent')
    RefundItem = apps.get_model('sales', 'RefundItem')
    SalesItem = apps.get_model('sales', 'SalesItem')
    SalesTransaction = apps.get_model('sales', 'SalesTransaction')

    for refund in Refund.objects.all():
        sold = SalesItem.objects.filter(transaction_id=refund.transaction_id).aggregate(
            n=Sum('quantity_sold')
        )['n'] or 0
        returned = RefundItem.objects.filter(refund_id=refund.pk).aggregate(
            n=Sum('quantity')
        )['n'] or 0
        # Full only when this one refund returned everything the order sold;
        # anything less (including the last of several) was a partial refund.
        Refund.objects.filter(pk=refund.pk).update(
            requested_by_id=refund.refunded_by_id,
            requested_at=refund.refunded_at,
            refund_type='Full' if sold and returned == sold else 'Partial',
        )
        event = RefundEvent.objects.create(
            refund_id=refund.pk, from_status='', to_status='Approved',
            actor_id=refund.refunded_by_id,
            note='Issued before approvals existed — carried over as approved.',
        )
        RefundEvent.objects.filter(pk=event.pk).update(created_at=refund.refunded_at)

    # Every carried-over refund is Approved, so an order's refunded_amount is
    # simply the sum of its refunds.
    totals = Refund.objects.values('transaction_id').annotate(n=Sum('total_amount'))
    for row in totals:
        SalesTransaction.objects.filter(pk=row['transaction_id']).update(
            refunded_amount=row['n']
        )


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0015_salestransaction_completed_at_refund_refunditem'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='salestransaction',
            name='refunded_amount',
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=12,
                help_text="Sum of this order's approved refunds. total_amount is "
                          "never reduced; net_amount is total_amount minus this.",
            ),
        ),
        migrations.AlterModelOptions(
            name='refund',
            options={'ordering': ['-requested_at', '-id']},
        ),
        # Existing rows are Approved; a NEW refund starts Pending (AlterField below).
        migrations.AddField(
            model_name='refund',
            name='status',
            field=models.CharField(
                choices=[('Pending', 'Pending approval'), ('Approved', 'Approved'),
                         ('Rejected', 'Rejected')],
                default='Approved', max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name='refund',
            name='status',
            field=models.CharField(
                choices=[('Pending', 'Pending approval'), ('Approved', 'Approved'),
                         ('Rejected', 'Rejected')],
                default='Pending', max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='refund',
            name='reason_code',
            field=models.CharField(
                choices=[
                    ('Defective', 'Defective / damaged item'),
                    ('Wrong Item', 'Wrong item supplied'),
                    ('Expired', 'Expired / spoiled'),
                    ('Not As Described', 'Not as described'),
                    ('Pricing Error', 'Pricing / billing error'),
                    ('Changed Mind', 'Customer changed mind'),
                    ('Other', 'Other (explain below)'),
                ],
                default='Other', max_length=20,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='refund',
            name='refund_type',
            field=models.CharField(
                choices=[('Full', 'Full refund'), ('Partial', 'Partial refund')],
                default='Partial', max_length=7,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='refund',
            name='requested_by',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='Whoever raised this refund.',
                limit_choices_to={'role__in': ['OWNER', 'BRANCH_MANAGER', 'STAFF']},
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='refunds_requested',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='refund',
            name='requested_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='refund',
            name='decision_note',
            field=models.CharField(
                blank=True, max_length=255,
                help_text='Required when a request is rejected — why.',
            ),
        ),
        migrations.AlterField(
            model_name='refund',
            name='reason',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name='refund',
            name='refunded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='refund',
            name='refunded_by',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='The Owner or Branch Manager who approved this refund.',
                limit_choices_to={'role__in': ['OWNER', 'BRANCH_MANAGER']},
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='refunds_issued',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name='RefundEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('from_status', models.CharField(blank=True, max_length=10)),
                ('to_status', models.CharField(
                    choices=[('Pending', 'Pending approval'), ('Approved', 'Approved'),
                             ('Rejected', 'Rejected')],
                    max_length=10,
                )),
                ('note', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='refund_events', to=settings.AUTH_USER_MODEL,
                )),
                ('refund', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='events', to='sales.refund',
                )),
            ],
            options={'ordering': ['created_at', 'id']},
        ),
        migrations.CreateModel(
            name='OrderStatusHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('from_status', models.CharField(blank=True, max_length=10)),
                ('to_status', models.CharField(
                    choices=[('Pending', 'Pending'), ('Approved', 'Approved'),
                             ('Preparing', 'Preparing'), ('Completed', 'Completed'),
                             ('Cancelled', 'Cancelled')],
                    max_length=10,
                )),
                ('note', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='order_status_changes', to=settings.AUTH_USER_MODEL,
                )),
                ('transaction', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='status_history', to='sales.salestransaction',
                )),
            ],
            options={
                'verbose_name_plural': 'Order status history',
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.RunPython(carry_over_existing_refunds, migrations.RunPython.noop),
    ]
