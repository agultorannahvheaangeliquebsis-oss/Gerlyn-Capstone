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


class ServiceArea(models.Model):
    """A place one branch delivers to (or, when inactive, does not).

    Matching is hierarchical and by PSGC code, never by spelling
    (``branches.coverage``): an active province entry covers every
    municipality and barangay inside it, an active municipality entry covers
    its barangays, and the most specific entry that exists for a place decides
    it, so an INACTIVE barangay or municipality entry excludes a place its
    broader entry would otherwise cover. A more specific entry may also carry
    its own fee and minimum order; a blank one inherits from the broader entry.

    The codes make the parents explicit: a barangay entry names its
    municipality and province too, because "Poblacion" alone names hundreds of
    different places.
    """

    class AreaType(models.TextChoices):
        PROVINCE = 'province', 'Province'
        MUNICIPALITY = 'municipality', 'Municipality'
        BARANGAY = 'barangay', 'Barangay'

    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='service_areas'
    )
    area_type = models.CharField(max_length=12, choices=AreaType.choices)
    # The PSGC name, filled in by the server from the codes below so the label
    # can never disagree with what is actually matched.
    area_name = models.CharField(max_length=120)
    province_code = models.CharField(max_length=10)
    city_code = models.CharField(max_length=10, blank=True)
    barangay_code = models.CharField(max_length=15, blank=True)
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text='Inactive entries are not delivered to. A specific inactive '
                  'entry excludes a place its broader entry would cover.',
    )
    delivery_fee = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        validators=[
            MinValueValidator(Decimal('0')),
            MaxValueValidator(Decimal('5000.00')),
        ],
        help_text='Optional. Shown to shoppers; settled with the branch, not '
                  'added to the order total.',
    )
    minimum_order = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[
            MinValueValidator(Decimal('0')),
            MaxValueValidator(Decimal('100000.00')),
        ],
        help_text='Optional. A delivery order below this amount is refused.',
    )
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['branch_id', 'area_type', 'area_name']
        constraints = [
            models.UniqueConstraint(
                fields=['branch', 'area_type', 'province_code', 'city_code', 'barangay_code'],
                name='service_area_unique_place_per_branch',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(area_type='province', city_code='', barangay_code='')
                    | models.Q(area_type='municipality', barangay_code='') & ~models.Q(city_code='')
                    | models.Q(area_type='barangay') & ~models.Q(city_code='') & ~models.Q(barangay_code='')
                ),
                name='service_area_codes_match_type',
            ),
        ]

    def __str__(self):
        return f'{self.area_name} ({self.get_area_type_display()}) - {self.branch}'
