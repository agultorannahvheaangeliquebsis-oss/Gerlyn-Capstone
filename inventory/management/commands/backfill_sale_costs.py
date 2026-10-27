"""Freeze a unit cost on sale lines recorded before costs were tracked.

Every sale line now carries ``unit_cost`` from the moment its stock leaves the
shelf (inventory.costing). Lines from before that have none, and the
Profitability report values them at the product's CURRENT cost price and says
so. Running this once turns that estimate into a frozen figure, using the
batches each line actually drew on where they are known (so a lot bought at
last month's supplier price is costed at that price) and the product's cost
price otherwise.

Only blanks are filled: a cost already frozen on a line is never touched, so it
is safe to run again, and Completed orders only (an order still in the
pipeline takes its stock, and its cost, when it is completed).

    python manage.py backfill_sale_costs --dry-run
    python manage.py backfill_sale_costs
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from audit import context as audit_context
from inventory.costing import backfill_sale_costs


class Command(BaseCommand):
    help = "Freeze a unit cost on completed sale lines recorded before costs were tracked."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Count what would be filled without changing anything.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        # A bookkeeping pass over thousands of lines, not a decision anyone
        # made: nothing here would belong in the audit log (and unit_cost is
        # not an audited field anyway).
        with audit_context.suppressed(), transaction.atomic():
            filled, unknown = backfill_sale_costs(dry_run=dry_run)

        verb = 'Would fill' if dry_run else 'Filled'
        self.stdout.write(self.style.SUCCESS(f'{verb} {filled:,} sale line(s) with a unit cost.'))
        if unknown:
            self.stdout.write(self.style.WARNING(
                f'{unknown:,} line(s) still have no cost: their product has no cost price and none of '
                'their batches trace to a purchase order. Set a cost price on those products '
                '(Product Master List -> Edit); the Profitability report lists them.'
            ))
