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
from inventory.models import Inventory, Product, StockMovement
from sales.models import SalesItem, SalesReturn, SalesTransaction


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

    def test_cash_walk_in_is_still_auto_paid_and_confirmed_by_the_ringing_user(self):
        """A-1 regression — cash is settled at the counter, so a cash walk-in
        still lands Paid with the ringing user recorded as confirmer. The
        maker/checker rule only touches the methods that clear later."""
        self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'order_type': 'Retail',
            'product_id': [str(self.product.pk)], 'quantity': ['2'],
            'amount_tendered': '200',
        })
        billing = SalesTransaction.objects.get().billing_record
        self.assertEqual(billing.status, 'Paid')
        self.assertEqual(billing.confirmed_by, self.owner)
        self.assertEqual(billing.payment_method, 'Cash')

    def test_non_cash_walk_in_is_left_pending_for_a_separate_check(self):
        """A-1 — a GCash walk-in rung by Staff must clear through
        billing_confirm as its own step: the record stays Pending with no
        confirmer, so it can't be recorded and cleared by the same hand."""
        staff = User.objects.create_user(
            username='rey', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=staff, defaults={'staff_name': 'Rey', 'branch': self.branch}
        )
        self.client.force_login(staff)
        self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'GCash',
            'order_type': 'Retail',
            'product_id': [str(self.product.pk)], 'quantity': ['2'],
        })
        txn = SalesTransaction.objects.get()
        self.assertEqual(txn.staff, staff)
        billing = txn.billing_record
        self.assertEqual(billing.status, 'Pending')
        self.assertIsNone(billing.confirmed_by)
        self.assertEqual(billing.payment_method, 'GCash')

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


class CustomerReturnFlowTests(TestCase):
    """A return is per line item against a COMPLETED sale — distinct from
    transaction_set_status's whole-order void. These cover the full
    Pending -> Approved -> Refunded path (and -> Rejected), the quantity
    cap against what a line already had returned, and that only Owner/
    Branch Manager may decide one (Staff may only request it)."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-001', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=10, reorder_level=5,
        )
        self.txn = SalesTransaction.objects.create(
            branch=self.branch, order_status=SalesTransaction.OrderStatus.COMPLETED,
            total_amount=500,
        )
        self.item = SalesItem.objects.create(
            transaction=self.txn, product=self.product,
            quantity_sold=5, unit_price=100,
        )
        self.client.force_login(self.owner)

    def _post_new_return(self, qty=2, condition='Resellable', reason='Defective',
                          refund_method='Cash'):
        return self.client.post(reverse('return_create'), {
            'order_number': self.txn.order_number,
            'sales_item_id': [str(self.item.pk)],
            'quantity_returned': [str(qty)],
            'reason': reason,
            'condition': condition,
            'refund_method': refund_method,
            'notes': '',
        })

    def test_creating_a_return_computes_the_refund_and_leaves_stock_untouched(self):
        response = self._post_new_return(qty=2)
        sales_return = SalesReturn.objects.get()
        self.assertRedirects(response, reverse('return_detail', args=[sales_return.pk]))
        self.assertEqual(sales_return.status, 'Pending')
        self.assertEqual(sales_return.refund_amount, 200)
        self.assertEqual(sales_return.items.get().quantity_returned, 2)
        # Nothing moves until a manager approves it.
        self.assertEqual(Inventory.objects.get(pk=self.branch.inventory.get().pk).quantity_on_hand, 10)

    def test_cannot_return_more_than_was_sold(self):
        response = self._post_new_return(qty=99)
        self.assertFalse(SalesReturn.objects.exists())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'sales/return_list.html')

    def test_a_second_return_is_capped_by_what_the_first_already_claimed(self):
        self._post_new_return(qty=4)
        response = self._post_new_return(qty=2)  # only 1 unit left returnable
        self.assertEqual(SalesReturn.objects.count(), 1)
        self.assertEqual(response.status_code, 200)

    def test_approving_a_resellable_return_restocks_the_floor_and_logs_it(self):
        self._post_new_return(qty=2, condition='Resellable')
        sales_return = SalesReturn.objects.get()
        response = self.client.post(reverse('return_approve', args=[sales_return.pk]))
        self.assertRedirects(response, reverse('return_detail', args=[sales_return.pk]))

        sales_return.refresh_from_db()
        self.assertEqual(sales_return.status, 'Approved')
        self.assertEqual(sales_return.decided_by, self.owner)
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity_on_hand, 12)
        self.assertTrue(
            StockMovement.objects.filter(
                reference_id=sales_return.pk, movement_type='Customer Return'
            ).exists()
        )

    def test_approving_a_write_off_return_never_touches_stock(self):
        self._post_new_return(qty=2, condition='Write-off')
        sales_return = SalesReturn.objects.get()
        self.client.post(reverse('return_approve', args=[sales_return.pk]))

        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity_on_hand, 10)
        self.assertFalse(
            StockMovement.objects.filter(reference_id=sales_return.pk).exists()
        )

    def test_full_lifecycle_to_refunded(self):
        self._post_new_return(qty=1)
        sales_return = SalesReturn.objects.get()
        self.client.post(reverse('return_approve', args=[sales_return.pk]))
        response = self.client.post(reverse('return_mark_refunded', args=[sales_return.pk]))
        self.assertRedirects(response, reverse('return_detail', args=[sales_return.pk]))
        sales_return.refresh_from_db()
        self.assertEqual(sales_return.status, 'Refunded')
        self.assertIsNotNone(sales_return.refunded_at)

    def test_a_pending_return_can_be_rejected_with_a_reason(self):
        self._post_new_return(qty=1)
        sales_return = SalesReturn.objects.get()
        self.client.post(reverse('return_reject', args=[sales_return.pk]), {
            'decision_notes': 'Past the return window',
        })
        sales_return.refresh_from_db()
        self.assertEqual(sales_return.status, 'Rejected')
        self.assertEqual(sales_return.decision_notes, 'Past the return window')
        # A rejected return releases its claim — the full quantity is
        # returnable again by a later request.
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity_on_hand, 10)

    def test_cannot_mark_refunded_before_approval(self):
        self._post_new_return(qty=1)
        sales_return = SalesReturn.objects.get()
        self.client.post(reverse('return_mark_refunded', args=[sales_return.pk]))
        sales_return.refresh_from_db()
        self.assertEqual(sales_return.status, 'Pending')

    def test_staff_can_request_but_not_approve(self):
        staff = User.objects.create_user(
            username='ana', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=staff, defaults={'staff_name': 'Ana', 'branch': self.branch}
        )
        self.client.force_login(staff)
        self._post_new_return(qty=1)
        sales_return = SalesReturn.objects.get()
        self.assertEqual(sales_return.requested_by, staff)

        response = self.client.post(reverse('return_approve', args=[sales_return.pk]))
        self.assertEqual(response.status_code, 403)
        sales_return.refresh_from_db()
        self.assertEqual(sales_return.status, 'Pending')

    def test_a_return_cannot_be_requested_against_another_branch(self):
        other_branch = Branch.objects.create(branch_name='Makinabang', owner=self.owner)
        other_txn = SalesTransaction.objects.create(
            branch=other_branch, order_status=SalesTransaction.OrderStatus.COMPLETED,
            total_amount=100,
        )
        SalesItem.objects.create(
            transaction=other_txn, product=self.product,
            quantity_sold=1, unit_price=100,
        )
        staff = User.objects.create_user(
            username='ben', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=staff, defaults={'staff_name': 'Ben', 'branch': self.branch}
        )
        self.client.force_login(staff)
        response = self.client.post(reverse('return_create'), {
            'order_number': other_txn.order_number,
            'sales_item_id': ['999'], 'quantity_returned': ['1'],
            'reason': 'Defective', 'condition': 'Resellable', 'refund_method': 'Cash',
        })
        self.assertFalse(SalesReturn.objects.exists())
        self.assertEqual(response.status_code, 200)


class TransactionQuantityValidationTests(TestCase):
    """A cart line's quantity must stay within core.validation.MAX_QUANTITY —
    a line over the ceiling is dropped the same way an unparseable one
    already was, rather than being saved as-is."""

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
            quantity_on_hand=500000, reorder_level=5,
        )
        self.client.force_login(self.owner)

    def test_quantity_over_the_ceiling_is_dropped_not_saved(self):
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk,
            'mode_of_payment': 'Cash',
            'order_type': 'Retail',
            'product_id': [str(self.product.pk)],
            'quantity': ['100001'],
            'amount_tendered': '10000001',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('Add at least one item with a quantity.', response.context['pos_errors'])
        self.assertFalse(SalesTransaction.objects.exists())
