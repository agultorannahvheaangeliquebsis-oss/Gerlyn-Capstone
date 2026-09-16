"""O-5 — the Order Console had no Fulfilment column or filter: Delivery and
Pickup orders were indistinguishable in the list, even though
SalesTransaction.fulfillment_type has carried that since F-11.

O-3 — the status tabs built their href by hand-listing filters (q, channel,
branch), so `type` was already silently dropped on every tab switch, and
Fulfilment would have been dropped the same way. _transaction_list_context
now builds `filter_qs` once, and both the toolbar's select and the tabs
read off it, so no filter can fall out of the tab links again.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from sales.models import SalesTransaction


class OrderConsoleFulfilmentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='console-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Console Branch', location='Baliuag', owner=self.owner
        )
        self.client.force_login(self.owner)

        self.delivery_txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, total_amount=100,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
            fulfillment_type=SalesTransaction.FulfillmentType.DELIVERY,
        )
        self.pickup_txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, total_amount=100,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
            fulfillment_type=SalesTransaction.FulfillmentType.PICKUP,
        )

    def test_fulfilment_column_shows_on_the_console(self):
        response = self.client.get(reverse('transaction_list'))
        self.assertContains(response, 'Fulfilment')
        self.assertContains(response, '<span class="tag">Delivery</span>')
        self.assertContains(response, '<span class="tag">Pickup</span>')

    def test_fulfilment_filter_narrows_to_delivery_only(self):
        response = self.client.get(reverse('transaction_list'), {'fulfillment': 'Delivery'})
        transactions = list(response.context['transactions'])
        self.assertIn(self.delivery_txn, transactions)
        self.assertNotIn(self.pickup_txn, transactions)

    def test_fulfilment_filter_narrows_to_pickup_only(self):
        response = self.client.get(reverse('transaction_list'), {'fulfillment': 'Pickup'})
        transactions = list(response.context['transactions'])
        self.assertIn(self.pickup_txn, transactions)
        self.assertNotIn(self.delivery_txn, transactions)

    def test_status_tab_preserves_the_fulfilment_filter(self):
        # Before O-3, the tabs were hand-built with only q/channel/branch, so
        # switching status here would have silently dropped `fulfillment`
        # from every tab's href. (This test used to also carry a `type`
        # filter alongside it — SalesTransaction.order_type is gone along
        # with retail/wholesale pricing, replaced by per-line quantity
        # tiers, so there is no longer a `type` filter for a tab to drop.)
        response = self.client.get(reverse('transaction_list'), {
            'fulfillment': 'Delivery',
        })
        self.assertContains(response, 'fulfillment=Delivery')
        # And specifically on a status tab link, not just anywhere on the page.
        self.assertContains(
            response,
            'href="?fulfillment=Delivery&amp;status=Pending"'
        )
