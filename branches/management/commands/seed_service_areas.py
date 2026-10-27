"""Give branches their placeholder delivery areas (Bulacan, Baliuag).

Idempotent and non-destructive: an area that already exists is left exactly as
the Owner last edited it, including one they deactivated. Branches created
after the initial rollout start with no areas (delivery unrestricted), so run
this to give them the same starting point the existing branches got.

    python manage.py seed_service_areas
"""
from django.core.management.base import BaseCommand

from branches.coverage import seed_default_areas
from branches.models import Branch


class Command(BaseCommand):
    help = 'Create the placeholder Bulacan / Baliuag delivery areas for every branch.'

    def handle(self, *args, **options):
        created = seed_default_areas()
        self.stdout.write(
            f'{created} delivery area(s) created across {Branch.objects.count()} branch(es). '
            f'Existing areas were not changed.'
        )
