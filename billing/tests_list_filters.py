"""Billing Records list — the Order Console's channel/fulfilment dropdowns and
the shared date-range control (_daterange.html)."""
from datetime import timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import User
from billing.models import BillingRecord
from branches.models import Branch
from sales.models import SalesTransaction

T = SalesTransaction.TransactionType
F = SalesTransaction.FulfillmentType


class BillingListFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='bf-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='BF Branch', location='Baliuag', owner=cls.owner
        )
        cls.walk_in = cls._record(T.PHYSICAL, F.PICKUP, 100)
        cls.online_pickup = cls._record(T.ONLINE, F.PICKUP, 200)
        cls.online_delivery = cls._record(T.ONLINE, F.DELIVERY, 400)
        # Unpaid and 60 days old — outside the default Last 30 days window.
        cls.old_unpaid = cls._record(T.ONLINE, F.DELIVERY, 800, days_ago=60)

    @classmethod
    def _record(cls, channel, fulfillment, amount, days_ago=0):
        txn = SalesTransaction.objects.create(
            branch=cls.branch, staff=None, total_amount=amount,
            transaction_type=channel, fulfillment_type=fulfillment,
        )
        record = txn.billing_record
        if days_ago:
            # date_recorded is auto_now_add, so backdate it with update().
            BillingRecord.objects.filter(pk=record.pk).update(
                date_recorded=timezone.now() - timedelta(days=days_ago)
            )
        return record

    def _get(self, **params):
        client = Client()
        client.force_login(self.owner)
        response = client.get('/billing/', params)
        self.assertEqual(response.status_code, 200)
        return response

    def _ids(self, response):
        return {r.pk for r in response.context['records']}

    def test_channel_filter(self):
        response = self._get(channel='Physical', range='all')
        self.assertEqual(self._ids(response), {self.walk_in.pk})
        self.assertEqual(response.context['counts']['all'], 1)

    def test_fulfillment_filter(self):
        response = self._get(fulfillment='Delivery', range='all')
        self.assertEqual(
            self._ids(response), {self.online_delivery.pk, self.old_unpaid.pk}
        )

    def test_channel_and_fulfillment_combine(self):
        response = self._get(channel='Online', fulfillment='Pickup', range='all')
        self.assertEqual(self._ids(response), {self.online_pickup.pk})

    def test_unknown_channel_value_is_ignored_not_empty(self):
        response = self._get(channel='Bogus', range='all')
        self.assertEqual(response.context['channel_filter'], '')
        self.assertEqual(len(self._ids(response)), 4)

    def test_date_range_narrows_list_and_counts(self):
        response = self._get(range='30')
        self.assertNotIn(self.old_unpaid.pk, self._ids(response))
        self.assertEqual(response.context['counts']['all'], 3)
        self.assertEqual(response.context['active_range'], '30')

    def test_outstanding_follows_the_date_range(self):
        """Outstanding totals the Pending bills actually on screen. It used
        to be taken before the range was applied, so a narrowed list showed
        a total for bills it wasn't listing — "Pending 3" over a figure
        summing four. Every record here is Pending: 100 + 200 + 400, with
        the 60-day-old 800 outside Last 30 days."""
        self.assertEqual(self._get(range='30').context['outstanding'], Decimal('700.00'))
        self.assertEqual(self._get(range='all').context['outstanding'], Decimal('1500.00'))

    def test_outstanding_follows_the_dropdowns_too(self):
        response = self._get(channel='Physical', range='all')
        self.assertEqual(response.context['outstanding'], Decimal('100.00'))

    def test_outstanding_ignores_only_the_status_tab(self):
        """The status tabs are the one control that must not move it — on
        the Paid tab the tile still reports what is owed, not ₱0.00."""
        response = self._get(status='Paid', range='all')
        self.assertEqual(self._ids(response), set())
        self.assertEqual(response.context['outstanding'], Decimal('1500.00'))

    def test_custom_range_is_inclusive_of_the_end_day(self):
        today = timezone.localdate().isoformat()
        response = self._get(range='custom', date_from=today, date_to=today)
        self.assertEqual(len(self._ids(response)), 3)

    def test_status_tabs_and_range_keep_the_dropdown_filters(self):
        response = self._get(channel='Online', fulfillment='Delivery', range='all')
        self.assertIn('channel=Online', response.context['filter_qs'])
        self.assertIn('fulfillment=Delivery', response.context['filter_qs'])
        carry = dict(response.context['daterange_carry'])
        self.assertEqual(carry['channel'], 'Online')
        self.assertEqual(carry['fulfillment'], 'Delivery')
        self.assertContains(response, 'class="daterange"')
