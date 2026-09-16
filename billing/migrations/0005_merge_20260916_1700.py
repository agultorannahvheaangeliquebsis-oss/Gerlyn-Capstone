from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0004_alter_billingrecord_proof_of_payment'),
        ('billing', '0004_billingrecord_date_voided_billingrecord_voided_by'),
    ]

    operations = []
