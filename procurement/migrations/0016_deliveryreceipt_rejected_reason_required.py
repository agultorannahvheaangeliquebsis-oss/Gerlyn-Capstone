from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('procurement', '0015_receipt_integrity_and_traceability'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='deliveryreceipt',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(outcome='Accepted')
                    | ~models.Q(rejection_reason='')
                ),
                name='deliveryreceipt_rejected_reason_required',
            ),
        ),
    ]
