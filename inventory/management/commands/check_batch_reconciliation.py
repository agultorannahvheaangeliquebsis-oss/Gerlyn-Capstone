"""O-1 — surface any Inventory/WarehouseStock row whose quantity_on_hand has
drifted from the sum of its own Batches. See inventory.alerts.
batch_inventory_mismatches for what counts as a mismatch and why a row with
no batches at all is not one.

Read-only: this only reports, it never corrects a mismatch it finds — fixing
one means deciding which number is actually right, a call this command has
no way to make on its own.

Exits 1 when it finds anything, so it can be wired into a scheduled check
without a human needing to read the output every time.

    python manage.py check_batch_reconciliation
"""
from django.core.management.base import BaseCommand, CommandError

from inventory.alerts import batch_inventory_mismatches


class Command(BaseCommand):
    help = "Flag Inventory/WarehouseStock rows whose quantity_on_hand disagrees with their Batches."

    def handle(self, *args, **options):
        mismatches = batch_inventory_mismatches()

        if not mismatches:
            self.stdout.write(self.style.SUCCESS('No batch/inventory mismatches found.'))
            return

        self.stdout.write(self.style.ERROR(
            f'{len(mismatches)} batch/inventory mismatch(es) found:'
        ))
        for row in mismatches:
            self.stdout.write(
                f"  [{row['location']}] {row['product'].product_name} @ "
                f"{row['branch'].branch_name}: quantity_on_hand="
                f"{row['quantity_on_hand']}, batches sum to "
                f"{row['batch_total']} (difference {row['difference']:+d})"
            )
        raise CommandError(f'{len(mismatches)} batch/inventory mismatch(es) found.')
