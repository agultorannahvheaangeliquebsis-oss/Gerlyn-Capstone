from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from accounts.models import User


class Branch(models.Model):
    owner = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'OWNER'}, related_name='branches'
    )
    branch_name = models.CharField(max_length=100)
    location = models.TextField()
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Inactive branches are excluded from customer fulfillment.",
    )
    region = models.CharField(max_length=10, blank=True)
    province = models.CharField(max_length=10, blank=True)
    city = models.CharField(max_length=10, blank=True)
    barangay = models.CharField(max_length=15, blank=True)
    street_detail = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal('-90')),
            MaxValueValidator(Decimal('90')),
        ],
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal('-180')),
            MaxValueValidator(Decimal('180')),
        ],
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(latitude__isnull=True, longitude__isnull=True)
                    | models.Q(latitude__isnull=False, longitude__isnull=False)
                ),
                name='branch_coordinate_pair_complete',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(latitude__isnull=True)
                    | models.Q(latitude__gte=-90, latitude__lte=90)
                ),
                name='branch_latitude_in_range',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(longitude__isnull=True)
                    | models.Q(longitude__gte=-180, longitude__lte=180)
                ),
                name='branch_longitude_in_range',
            ),
        ]

    def clean(self):
        super().clean()
        if (self.latitude is None) != (self.longitude is None):
            raise ValidationError({
                'latitude': 'Latitude and longitude must be provided together.',
                'longitude': 'Latitude and longitude must be provided together.',
            })

    def __str__(self):
        return self.branch_name
