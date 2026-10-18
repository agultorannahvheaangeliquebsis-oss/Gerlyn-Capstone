"""Flag and notify overdue purchase-order deliveries.

    python manage.py process_overdue_deliveries

Also run by `dispatch_notifications`, so scheduling that one daily job is
enough. Idempotent; never cancels or otherwise changes an order.
"""
from django.core.management.base import BaseCommand

from procurement.services import process_overdue_deliveries


class Command(BaseCommand):
    help = 'Notify about ordered/incoming orders past their delivery date.'

    def handle(self, *args, **options):
        result = process_overdue_deliveries()
        self.stdout.write(self.style.SUCCESS(
            f'Flagged {result.flagged} overdue deliver'
            f'{"y" if result.flagged == 1 else "ies"}; '
            f'escalated {result.escalated}.'
        ))
