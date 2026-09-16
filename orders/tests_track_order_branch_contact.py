"""S-10 — the order tracking page named the branch handling an order but
gave no way to actually reach it. This adds a contact block showing the
branch's name and location.

Deliberately the SAFE half only: no self-serve cancel button. Whether an
order can still be changed depends on state the shopper can't see from
here (has staff started preparing it), so cancelling stays something the
branch does, not a button on this page — see the audit's own guardrail
against shipping that half.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from sales.models import SalesTransaction


class TrackOrderBranchContactTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='s10-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Poblacion Branch', location='Rizal St, Baliuag, Bulacan',
            owner=self.owner,
        )
        self.customer = User.objects.create_user(
            username='s10-customer', password='pw', role=User.Role.CUSTOMER
        )
        self.sale = SalesTransaction.objects.create(
            branch=self.branch, customer=self.customer, total_amount=250,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        self.client.force_login(self.customer)

    def _contact_block(self, response):
        """The page footer already lists every branch's name and location
        site-wide (nav_branches), and the header already links to
        /about/#branches — so asserting those strings anywhere on the page
        would pass whether or not this order-specific contact block exists.
        Scope every assertion to the "Questions about this order?" block
        itself."""
        content = response.content.decode()
        start = content.index('Questions about this order?')
        # Up to the next card, generously — this block is short.
        return content[start:start + 600]

    def test_branch_name_and_location_are_shown_in_the_contact_block(self):
        response = self.client.get(reverse('orders:track_order', args=[self.sale.pk]))
        block = self._contact_block(response)
        self.assertIn('Poblacion Branch', block)
        self.assertIn('Rizal St, Baliuag, Bulacan', block)

    def test_contact_block_links_to_the_full_branch_list(self):
        response = self.client.get(reverse('orders:track_order', args=[self.sale.pk]))
        block = self._contact_block(response)
        self.assertIn('/about/#branches', block)

    def test_no_self_serve_cancel_button_is_offered(self):
        response = self.client.get(reverse('orders:track_order', args=[self.sale.pk]))
        self.assertNotContains(response, 'Cancel Order')
        self.assertNotContains(response, 'Cancel order')
