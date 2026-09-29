"""Verify seed_demo's billing fixture covers the full PaymentStatus set."""
from django.core.management import call_command
from django.test import TransactionTestCase

from billing.models import BillingRecord


class SeedDemoBillingStatusCoverageTests(TransactionTestCase):
    # seed_demo --reset TRUNCATEs, which implicitly commits in MySQL and would
    # tear down a TestCase's surrounding transaction mid-run.
    def test_every_payment_status_has_at_least_one_demo_row(self):
        call_command('seed_demo', '--reset', verbosity=0)
        present = set(BillingRecord.objects.values_list('status', flat=True))
        for status in BillingRecord.PaymentStatus.values:
            with self.subTest(status=status):
                self.assertIn(status, present)
