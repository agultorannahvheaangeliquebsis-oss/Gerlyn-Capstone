"""Walk-in Sale is its own page (sales/transaction_form.html) reached from
the Order Console's "New Walk-in Sale" link, not a modal on the console.

These pin down that a recorded sale still creates the same transaction,
items, stock movement and paid billing record, and that a rejected submit
re-renders the form with the basket intact instead of redirecting and
throwing it away.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import StaffProfile, User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


class WalkInSaleFormTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-001', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100, wholesale_price=80,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.client.force_login(self.owner)

    def test_get_renders_the_walk_in_sale_page(self):
        response = self.client.get(reverse('transaction_add'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'sales/transaction_form.html')
        self.assertIn(self.product, list(response.context['pos_products']))
        self.assertContains(response, 'id="pos-form"')

    def test_console_links_to_the_walk_in_sale_page_for_a_user_who_may_sell(self):
        response = self.client.get(reverse('transaction_list'))
        self.assertTrue(response.context['can_pos'])
        self.assertContains(response, reverse('transaction_add'))

    def test_recording_a_sale_still_deducts_stock_and_marks_it_paid(self):
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk,
            'mode_of_payment': 'Cash',
            'order_type': 'Retail',
            'product_id': [str(self.product.pk)],
            'quantity': ['3'],
            'amount_tendered': '300',
        })
        txn = SalesTransaction.objects.get()
        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        self.assertEqual(txn.total_amount, 300)
        self.assertEqual(txn.order_status, 'Completed')
        self.assertEqual(txn.items.count(), 1)
        self.assertEqual(txn.items.first().unit_price, self.product.unit_price)

        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity_on_hand, 47)
        self.assertEqual(txn.billing_record.status, 'Paid')
        self.assertEqual(txn.billing_record.amount_due, 300)

    def test_wholesale_still_bills_at_the_wholesale_price(self):
        self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'order_type': 'Wholesale',
            'product_id': [str(self.product.pk)], 'quantity': ['2'],
            'amount_tendered': '160',
        })
        txn = SalesTransaction.objects.get()
        self.assertEqual(txn.order_type, 'Wholesale')
        self.assertEqual(txn.total_amount, 160)

    def test_no_branch_reopens_the_form_and_keeps_the_basket(self):
        response = self.client.post(reverse('transaction_add'), {
            'branch': '', 'mode_of_payment': 'GCash', 'order_type': 'Retail',
            'product_id': [str(self.product.pk)], 'quantity': ['4'],
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'sales/transaction_form.html')
        self.assertIn(
            'Please choose a branch for this sale.', response.context['pos_errors']
        )
        # The basket comes back so the cashier does not re-ring it.
        self.assertEqual(
            response.context['pos_restore_rows'],
            [{'product_id': str(self.product.pk), 'quantity': '4'}],
        )
        self.assertContains(response, 'pos-restore-rows')
        # ...and the payment method they had already picked is still selected.
        self.assertContains(response, '<option value="GCash" selected>')
        self.assertFalse(SalesTransaction.objects.exists())

    def test_an_empty_basket_is_refused_without_recording_anything(self):
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'order_type': 'Retail', 'product_id': [''], 'quantity': ['1'],
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'sales/transaction_form.html')
        self.assertIn(
            'Add at least one item with a quantity.', response.context['pos_errors']
        )
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_zero_quantity_row_comes_back_visible(self):
        """It fails the digit/>0 check, so the sale is refused — but the row
        still returns, or the cashier cannot see which line was wrong."""
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'order_type': 'Retail',
            'product_id': [str(self.product.pk)], 'quantity': ['0'],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['pos_restore_rows'],
            [{'product_id': str(self.product.pk), 'quantity': '0'}],
        )
        self.assertFalse(SalesTransaction.objects.exists())

    def test_staff_are_pinned_to_their_own_branch(self):
        staff = User.objects.create_user(
            username='ana', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=staff, defaults={'staff_name': 'Ana', 'branch': self.branch}
        )
        self.client.force_login(staff)

        response = self.client.get(reverse('transaction_add'))
        self.assertEqual(response.context['pos_user_branch'], self.branch)
        self.assertEqual(list(response.context['pos_branches']), [])

        # A posted branch is ignored for staff — their own branch wins.
        other = Branch.objects.create(branch_name='Makinabang', owner=self.owner)
        self.client.post(reverse('transaction_add'), {
            'branch': other.pk, 'mode_of_payment': 'Cash', 'order_type': 'Retail',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
            'amount_tendered': '100',
        })
        txn = SalesTransaction.objects.get()
        self.assertEqual(txn.branch_id, self.branch.pk)
        self.assertEqual(txn.staff, staff)

    def test_a_role_that_cannot_sell_gets_no_form_and_cannot_post(self):
        supplier = User.objects.create_user(
            username='century', password='pw', role=User.Role.SUPPLIER
        )
        self.client.force_login(supplier)
        self.assertEqual(
            self.client.get(reverse('transaction_list')).status_code, 302
        )
        self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'order_type': 'Retail',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
        })
        self.assertFalse(SalesTransaction.objects.exists())
