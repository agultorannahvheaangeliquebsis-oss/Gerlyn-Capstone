"""Give every existing branch its placeholder serviceable areas.

Bulacan (whole province) and Baliuag (municipality), both active, with
placeholder delivery fees and NO minimum order, so this never makes a real
order impossible. Idempotent: a row that exists is left alone, which also means
re-running this after the Owner edited or deactivated one changes nothing.
Branches created later start with no rows (delivery unrestricted until the
Owner adds some); ``manage.py seed_service_areas`` gives them the same start.
"""
from decimal import Decimal

from django.db import migrations

# PSGC codes (accounts/data/ph_addresses): stable identifiers, written out so
# this migration does not change if the bundled snapshot is ever refreshed.
BULACAN = ('0314', 'Bulacan')
BALIUAG = ('0314', '031403', 'Baliuag')
NOTE = 'Placeholder - the Owner should confirm this in Administration.'


def seed(apps, schema_editor):
    Branch = apps.get_model('branches', 'Branch')
    ServiceArea = apps.get_model('branches', 'ServiceArea')
    for branch in Branch.objects.all():
        ServiceArea.objects.get_or_create(
            branch=branch, area_type='province', province_code=BULACAN[0],
            city_code='', barangay_code='',
            defaults={'area_name': BULACAN[1], 'is_active': True,
                      'delivery_fee': Decimal('100.00'), 'notes': NOTE},
        )
        ServiceArea.objects.get_or_create(
            branch=branch, area_type='municipality', province_code=BALIUAG[0],
            city_code=BALIUAG[1], barangay_code='',
            defaults={'area_name': BALIUAG[2], 'is_active': True,
                      'delivery_fee': Decimal('50.00'), 'notes': NOTE},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('branches', '0004_service_area'),
    ]

    operations = [
        # Reverse is a no-op: the rows may have been edited by the Owner by
        # then, and dropping the table (0004's reverse) removes them anyway.
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
