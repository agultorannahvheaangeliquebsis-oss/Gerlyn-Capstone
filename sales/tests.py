"""Walk-in Sale is its own page (sales/transaction_form.html) reached from
the Order Console's "New Walk-in Sale" link, not a modal on the console.

These pin down that a recorded sale still creates the same transaction,
items, stock movement and paid billing record, and that a rejected submit
re-renders the form with the basket intact instead of redirecting and
throwing it away.
"""
import threading
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.db import connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import StaffProfile, User
from branches.models import Branch
from core.module_permissions import DEFAULT_STAFF_MODULES, SALES_POS
from inventory.models import Inventory, Product
from sales.models import OrderNumberSequence, SalesTransaction


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

    def test_a_product_id_that_no_longer_exists_is_refused_without_a_500(self):
        """F-08b — a product deleted (or never real) after the form loaded
        used to raise an uncaught Product.DoesNotExist when the cart was
        priced. It is collected as a form error and the basket re-rendered
        instead, the same way a missing branch is — no 500, nothing written."""
        ghost_id = self.product.pk + 9999
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'order_type': 'Retail',
            'product_id': [str(ghost_id)], 'quantity': ['1'],
            'amount_tendered': '100',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'sales/transaction_form.html')
        self.assertTrue(any(
            'no longer available' in e for e in response.context['pos_errors']
        ))
        # The row still comes back so the cashier can see which line was wrong.
        self.assertEqual(
            response.context['pos_restore_rows'],
            [{'product_id': str(ghost_id), 'quantity': '1'}],
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


class SalesAPIModulePermissionTests(TestCase):
    """F-07 — MODULE_REGISTRY listed only transaction_* (the template url
    names), so ModulePermissionMiddleware turned a Staff account whose Owner
    revoked the Sales / Order Console module away from /sales/ but let the
    same account reach /api/sales/ directly. The api-sales-* names are now
    in the SALES_POS entry, so one gate covers both — a redirect away, the
    same refusal the template view already gave.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='f7s-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='F7S Branch', location='Baliuag', owner=cls.owner
        )
        cls.product = Product.objects.create(
            sku_code='F7S-1', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100, wholesale_price=80,
        )
        Inventory.objects.create(
            product=cls.product, branch=cls.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        cls.staff_with = User.objects.create_user(
            username='f7s-staff-with', password='pw', role=User.Role.STAFF
        )
        cls.staff_without = User.objects.create_user(
            username='f7s-staff-without', password='pw', role=User.Role.STAFF
        )
        for user, name in ((cls.staff_with, 'Wren'), (cls.staff_without, 'Otis')):
            StaffProfile.objects.update_or_create(
                user=user, defaults={'staff_name': name, 'branch': cls.branch}
            )
        # SALES_POS is a default Staff module, so revoking it means listing
        # every default EXCEPT it.
        User.objects.filter(pk=cls.staff_without.pk).update(
            allowed_modules=[c for c in DEFAULT_STAFF_MODULES if c != SALES_POS]
        )

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_staff_without_sales_module_is_refused_the_api_list(self):
        response = self._client(self.staff_without).get('/api/sales/')
        self.assertEqual(response.status_code, 302)

    def test_staff_without_sales_module_is_refused_the_api_detail(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, total_amount=100,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        response = self._client(self.staff_without).get(f'/api/sales/{txn.pk}/')
        self.assertEqual(response.status_code, 302)

    def test_api_and_web_agree_for_a_module_revoked_staff(self):
        api_resp = self._client(self.staff_without).get('/api/sales/')
        web_resp = self._client(self.staff_without).get(reverse('transaction_list'))
        self.assertEqual(api_resp.status_code, 302)
        self.assertEqual(web_resp.status_code, 302)

    def test_staff_with_sales_module_still_reaches_the_api_list(self):
        response = self._client(self.staff_with).get('/api/sales/')
        self.assertEqual(response.status_code, 200)

    def test_owner_is_never_module_gated_on_the_sales_api(self):
        response = self._client(self.owner).get('/api/sales/')
        self.assertEqual(response.status_code, 200)


class WalkInSalePaymentMethodValidationTests(TestCase):
    """F-05 — transaction_add read mode_of_payment straight from POST and wrote
    it to the column with no check against ModeOfPayment.choices, while
    order_type and discount_type right beside it were both validated. A junk
    value was persisted verbatim (breaking the Billing report's payment
    breakdown) AND slipped the amount-tendered guard, since only the literal
    'Cash' triggers it — so a sale could be booked with a garbage method, no
    cash captured, and its billing record stuck Pending.

    The fix matches the fall-back-to-default pattern the sibling fields already
    use in this view: an unrecognised value becomes Cash.
    """

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

    def _post(self, **extra):
        payload = {
            'branch': self.branch.pk,
            'mode_of_payment': 'NOT A METHOD',
            'order_type': 'Retail',
            'product_id': [str(self.product.pk)],
            'quantity': ['3'],
        }
        payload.update(extra)
        return self.client.post(reverse('transaction_add'), payload)

    def test_a_bogus_method_cannot_bypass_the_amount_tendered_requirement(self):
        # No amount_tendered supplied. Pre-fix the junk method skipped the
        # cash check and the sale went through; now it falls back to Cash, so
        # the check fires and nothing is recorded.
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'sales/transaction_form.html')
        self.assertIn(
            'Enter the amount tendered for this cash sale.',
            response.context['pos_errors'],
        )
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_bogus_method_is_recorded_as_cash_never_persisted_verbatim(self):
        response = self._post(amount_tendered='300')
        txn = SalesTransaction.objects.get()
        self.assertRedirects(
            response, reverse('transaction_detail', args=[txn.pk])
        )
        self.assertEqual(txn.mode_of_payment, SalesTransaction.ModeOfPayment.CASH)
        self.assertNotEqual(txn.mode_of_payment, 'NOT A METHOD')
        # Cash walk-in: settled at the counter, so the billing record is Paid,
        # not left dangling Pending.
        self.assertEqual(txn.billing_record.payment_method, 'Cash')
        self.assertEqual(txn.billing_record.status, 'Paid')

    def test_a_valid_non_cash_method_is_still_kept_as_chosen(self):
        # Validation must not be so blunt it rewrites every method to Cash.
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
        self.assertEqual(txn.mode_of_payment, 'GCash')
        self.assertEqual(txn.billing_record.payment_method, 'GCash')


class OrderNumberYearScopedTests(TestCase):
    """F-14 — SalesTransaction.order_number is ORD-{year}-{NNN}. NNN used to be
    the global auto-increment pk, which never resets, so the first order of a
    new year came out as ORD-2027-104 — reading as if a hundred orders had
    gone missing at the year boundary. NNN now comes from a per-year counter
    (OrderNumberSequence) and restarts at 001 each January.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='on-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='ON Branch', location='Baliuag', owner=self.owner
        )

    @staticmethod
    def _at_year(year):
        # save() reads django.utils.timezone.now().year to pick the ORD- year;
        # pin it so these assertions don't depend on the wall clock.
        return mock.patch(
            'django.utils.timezone.now',
            return_value=datetime(year, 6, 1, 12, 0, tzinfo=dt_timezone.utc),
        )

    def _order(self):
        return SalesTransaction.objects.create(
            branch=self.branch, total_amount=100
        )

    def test_orders_within_one_year_number_sequentially_from_001(self):
        with self._at_year(2026):
            numbers = [self._order().order_number for _ in range(3)]
        self.assertEqual(
            numbers, ['ORD-2026-001', 'ORD-2026-002', 'ORD-2026-003']
        )

    def test_the_sequence_restarts_at_001_after_a_year_rollover(self):
        with self._at_year(2026):
            self._order()
            self._order()

        # First order of the new year: pre-fix this was ORD-2027-003 (pk=3).
        with self._at_year(2027):
            first = self._order().order_number
            second = self._order().order_number
        self.assertEqual(first, 'ORD-2027-001')
        self.assertEqual(second, 'ORD-2027-002')

        # The 2026 run is untouched and keeps counting from where it stopped.
        with self._at_year(2026):
            self.assertEqual(self._order().order_number, 'ORD-2026-003')

        self.assertEqual(
            OrderNumberSequence.objects.get(year=2026).last_value, 3
        )
        self.assertEqual(
            OrderNumberSequence.objects.get(year=2027).last_value, 2
        )

    def test_order_number_is_written_on_the_insert_never_left_blank(self):
        # The row is created with its real number in one write — it never sits
        # under the UNIQUE index carrying order_number='' waiting on a second
        # write, which is what two concurrent creates used to collide on.
        with self._at_year(2026):
            txn = self._order()
        self.assertEqual(txn.order_number, 'ORD-2026-001')
        txn.refresh_from_db()
        self.assertEqual(txn.order_number, 'ORD-2026-001')
        self.assertFalse(
            SalesTransaction.objects.filter(order_number='').exists()
        )


class OrderNumberConcurrencyTests(TransactionTestCase):
    """F-14 — two near-simultaneous creates must get distinct order numbers
    with no IntegrityError. The old code INSERTed every row with
    order_number='' under a UNIQUE index and patched it in a second write, so
    two creates landing together collided on the shared empty string; the
    number is now allocated from a select_for_update-locked per-year counter
    and written on the INSERT itself.

    Same shape as billing's D-1 concurrency test: real threads, real commits
    (TransactionTestCase), driven straight through the ORM rather than the
    test Client, and skipped on sqlite where SELECT ... FOR UPDATE takes no
    lock so the race can't be expressed. Runs for real on MySQL/InnoDB.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='onc-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='ONC Branch', location='Baliuag', owner=self.owner
        )

    def _create(self, results, index, barrier):
        outcome = {'order_number': None, 'error': None}
        try:
            barrier.wait(timeout=10)          # both threads create together
            txn = SalesTransaction.objects.create(
                branch=self.branch, total_amount=100
            )
            outcome['order_number'] = txn.order_number
        except Exception as exc:              # recorded, re-asserted on main thread
            outcome['error'] = repr(exc)
        finally:
            connections.close_all()
        results[index] = outcome

    def test_two_racing_creates_get_distinct_numbers_with_no_integrityerror(self):
        if connections['default'].vendor == 'sqlite':
            self.skipTest(
                'sqlite takes no row lock for SELECT ... FOR UPDATE, so this '
                'contention cannot be exercised. Runs for real on MySQL.'
            )

        # A single race can serialise by luck even with a broken lock; repeat.
        for attempt in range(5):
            results = [None, None]
            barrier = threading.Barrier(2)
            threads = [
                threading.Thread(target=self._create, args=(results, i, barrier))
                for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            ctx = f'(attempt {attempt}) {results}'
            self.assertNotIn(None, results, f'a create thread never returned {ctx}')
            self.assertEqual([r['error'] for r in results], [None, None], ctx)
            numbers = {r['order_number'] for r in results}
            self.assertEqual(
                len(numbers), 2, f'expected two distinct order numbers {ctx}'
            )
