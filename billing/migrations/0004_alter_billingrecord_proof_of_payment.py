import billing.storage
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0003_alter_billingrecord_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='billingrecord',
            name='proof_of_payment',
            field=models.ImageField(
                blank=True,
                help_text='Optional uploaded screenshot/photo of payment',
                null=True,
                storage=billing.storage.PaymentProofStorage(),
                upload_to='payment_proofs/',
            ),
        ),
    ]
