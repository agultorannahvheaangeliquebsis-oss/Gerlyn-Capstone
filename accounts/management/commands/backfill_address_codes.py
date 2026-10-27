"""One-off: record the province / municipality / barangay codes on saved
addresses that only have text.

Delivery coverage (branches.coverage) matches by PSGC code, never by spelling.
Addresses saved through the address picker already carry codes; older ones,
and any registered through the API with free text, do not, so checkout cannot
tell where they are and asks the shopper to pick the place from the lists.
This reads the text with accounts.geo.place_from_text - strictly by position,
a level filled only when exactly one PSGC name matches - and fills the BLANK
code columns it can prove. It never overwrites a code that is already there
and never edits the address text. An address whose text disagrees with its
own stored codes is left alone and listed as a conflict.

Writes go through queryset.update(), so no audit rows are written: only
derived columns change, nothing the customer typed.

Safe to re-run. It reports address and user ids only, never the address text.

    python manage.py backfill_address_codes --dry-run
    python manage.py backfill_address_codes
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts import geo
from accounts.models import Address

LEVELS = (('province', 'province_code'), ('city', 'city_code'), ('barangay', 'barangay_code'))


def _gap(found):
    """Why the text could not be read any further."""
    if not found.province_code:
        return 'province not recognised'
    if not found.city_code:
        return 'municipality not recognised'
    return 'barangay not recognised'


class Command(BaseCommand):
    help = (
        "Fill blank province / municipality / barangay codes on saved addresses "
        "by reading their text. Never overwrites a code or changes the text."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be filled without changing anything.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        counts = Counter()
        needs_customer = []   # (address id, user id, why)

        with transaction.atomic():
            for address in Address.objects.order_by('pk').iterator():
                if address.province and address.city and address.barangay:
                    counts['already complete'] += 1
                    continue

                found = geo.place_from_text(address.text)
                update = {}
                conflict = False
                for field, attr in LEVELS:
                    code = getattr(found, attr)
                    have = getattr(address, field)
                    if code and have and have != code:
                        conflict = True
                    elif code and not have:
                        update[field] = code
                if conflict:
                    counts['conflict'] += 1
                    needs_customer.append((address.pk, address.user_id, 'text disagrees with stored codes'))
                    continue

                final = {field: update.get(field) or getattr(address, field) for field, _ in LEVELS}
                if final['city'] and not final['province']:
                    final['province'] = update['province'] = geo.province_of_city(final['city'])
                if final['province'] and not address.region:
                    update['region'] = geo.region_of_province(final['province'])

                if update and not dry_run:
                    Address.objects.filter(pk=address.pk).update(**update)
                if all(final.values()):
                    counts['filled'] += 1
                elif any(field in update for field, _ in LEVELS):
                    counts['filled in part'] += 1
                    needs_customer.append((address.pk, address.user_id, _gap(found)))
                else:
                    counts['could not read'] += 1
                    needs_customer.append((address.pk, address.user_id, _gap(found)))

        for address_id, user_id, why in needs_customer:
            self.stdout.write(f'address {address_id} (user {user_id}): {why}')
        verb = 'would be' if dry_run else 'were'
        for label in ('filled', 'filled in part', 'could not read', 'conflict', 'already complete'):
            self.stdout.write(f'{counts[label]:>6}  {label}')
        self.stdout.write(self.style.SUCCESS(
            f'{counts["filled"] + counts["filled in part"]} address(es) {verb} updated; '
            f'{len(needs_customer)} still need the customer to pick the place.'
        ))
