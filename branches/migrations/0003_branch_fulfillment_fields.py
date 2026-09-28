from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('branches', '0002_branch_structured_location')]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='is_active',
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text='Inactive branches are excluded from customer fulfillment.',
            ),
        ),
        migrations.AlterField(
            model_name='branch',
            name='latitude',
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                max_digits=9,
                null=True,
                validators=[
                    MinValueValidator(Decimal('-90')),
                    MaxValueValidator(Decimal('90')),
                ],
            ),
        ),
        migrations.AlterField(
            model_name='branch',
            name='longitude',
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                max_digits=9,
                null=True,
                validators=[
                    MinValueValidator(Decimal('-180')),
                    MaxValueValidator(Decimal('180')),
                ],
            ),
        ),
        migrations.AddConstraint(
            model_name='branch',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(latitude__isnull=True, longitude__isnull=True)
                    | models.Q(latitude__isnull=False, longitude__isnull=False)
                ),
                name='branch_coordinate_pair_complete',
            ),
        ),
        migrations.AddConstraint(
            model_name='branch',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(latitude__isnull=True)
                    | models.Q(latitude__gte=-90, latitude__lte=90)
                ),
                name='branch_latitude_in_range',
            ),
        ),
        migrations.AddConstraint(
            model_name='branch',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(longitude__isnull=True)
                    | models.Q(longitude__gte=-180, longitude__lte=180)
                ),
                name='branch_longitude_in_range',
            ),
        ),
    ]
