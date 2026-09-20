"""Run the idempotent batch-expiry sweep on demand."""
from django.core.management.base import BaseCommand, CommandError

from inventory.services import ExpiryProcessingError, expire_due_batches


class Command(BaseCommand):
    help = 'Remove expired floor/warehouse batches and record expiry write-offs.'

    def handle(self, *args, **options):
        try:
            result = expire_due_batches()
        except ExpiryProcessingError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            f'Wrote off {result.batch_count} expired batch'
            f'{"" if result.batch_count == 1 else "es"} '
            f'({result.unit_count} unit'
            f'{"" if result.unit_count == 1 else "s"}): '
            f'{result.floor_unit_count} floor, '
            f'{result.warehouse_unit_count} warehouse.'
        ))
