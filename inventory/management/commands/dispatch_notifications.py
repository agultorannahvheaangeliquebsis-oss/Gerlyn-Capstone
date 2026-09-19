"""Materialize time-based notification events.

Run once per day in production:

    python manage.py dispatch_notifications

State-transition events are dispatched synchronously by their views/signals;
only calendar-driven events need this scheduled sweep.
"""
from django.core.management.base import BaseCommand

from inventory.alerts import dispatch_near_expiry_notifications


class Command(BaseCommand):
    help = 'Create due time-based notifications without duplicating earlier alerts.'

    def handle(self, *args, **options):
        created = dispatch_near_expiry_notifications()
        self.stdout.write(self.style.SUCCESS(
            f'Created {created} near-expiry notification row'
            f'{"" if created == 1 else "s"}.'
        ))
