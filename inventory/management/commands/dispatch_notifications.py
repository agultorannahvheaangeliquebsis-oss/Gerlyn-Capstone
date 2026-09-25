"""Run daily calendar-driven inventory work.

Run once per day in production:

    python manage.py dispatch_notifications

State-transition events are dispatched synchronously by their views/signals;
expiry transitions and calendar-driven notifications need this scheduled
sweep. Expired stock is processed first so it cannot linger in aggregate
floor or warehouse quantities.
"""
from django.core.management.base import BaseCommand

from inventory.alerts import dispatch_near_expiry_notifications
from inventory.services import expire_due_batches
from procurement.services import process_overdue_deliveries


class Command(BaseCommand):
    help = 'Process expired stock and create due time-based notifications.'

    def handle(self, *args, **options):
        expired = expire_due_batches()
        created = dispatch_near_expiry_notifications()
        overdue = process_overdue_deliveries()
        self.stdout.write(self.style.SUCCESS(
            f'Wrote off {expired.batch_count} expired batch'
            f'{"" if expired.batch_count == 1 else "es"} '
            f'({expired.unit_count} unit'
            f'{"" if expired.unit_count == 1 else "s"}).'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'Created {created} near-expiry notification row'
            f'{"" if created == 1 else "s"}.'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'Flagged {overdue.flagged} overdue deliver'
            f'{"y" if overdue.flagged == 1 else "ies"}; '
            f'escalated {overdue.escalated}.'
        ))
