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
from django.utils import timezone

from accounts.models import BranchManagerProfile, StaffProfile, User
from billing.models import BillingRecord
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
            unit_of_measure='Sack', unit_price=100,
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
            'product_id': [str(self.product.pk)], 'quantity': ['2'],
        })
        txn = SalesTransaction.objects.get()
        self.assertEqual(txn.staff, staff)
        billing = txn.billing_record
        self.assertEqual(billing.status, 'Pending')
        self.assertIsNone(billing.confirmed_by)
        self.assertEqual(billing.payment_method, 'GCash')

    def test_a_bulk_quantity_bills_at_the_tier_price(self):
        """The counter prices a line from the product's bulk ladder — there is
        no order-type choice left for the cashier to make, or to get wrong."""
        self.product.price_tiers.create(min_quantity=10, unit_price=80)
        self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)], 'quantity': ['10'],
            'amount_tendered': '800',
        })
        txn = SalesTransaction.objects.get()
        self.assertEqual(txn.items.get().unit_price, 80)
        self.assertEqual(txn.total_amount, 800)

    def test_the_page_carries_each_products_tiers_for_the_live_preview(self):
        """posRecalc reads the ladder off the option's data-tiers, so the
        cashier sees the bulk price before the sale is recorded, not after."""
        self.product.price_tiers.create(min_quantity=10, unit_price=80)
        response = self.client.get(reverse('transaction_add'))
        self.assertContains(response, 'data-tiers="[[10, 80.0]]"')

    def test_no_branch_reopens_the_form_and_keeps_the_basket(self):
        response = self.client.post(reverse('transaction_add'), {
            'branch': '', 'mode_of_payment': 'GCash',
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
            'product_id': [''], 'quantity': ['1'],
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
            'branch': other.pk, 'mode_of_payment': 'Cash',
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
            unit_of_measure='Sack', unit_price=100,
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
        self.assertEqual(response.status_code, 403)

    def test_staff_without_sales_module_is_refused_the_api_detail(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, total_amount=100,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        response = self._client(self.staff_without).get(f'/api/sales/{txn.pk}/')
        self.assertEqual(response.status_code, 403)

    def test_api_and_web_agree_for_a_module_revoked_staff(self):
        api_resp = self._client(self.staff_without).get('/api/sales/')
        web_resp = self._client(self.staff_without).get(reverse('transaction_list'))
        self.assertEqual(api_resp.status_code, 403)
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
    discount_type right beside it was validated. A junk
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
            unit_of_measure='Sack', unit_price=100,
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


class WalkInSaleBranchGuardTests(TestCase):
    """F-13 — _pos_user_branch returned None both for the Owner (unrestricted)
    and for a Staff/Manager account whose branch profile was missing, so a
    half-provisioned cashier was handed the Owner's every-branch selector and
    the POST handler never ran user_can_access_branch on the branch it took
    from the form. Now the two cases are distinct: an unassigned Staff/Manager
    is refused outright, and the resolved branch is scope-checked for every
    role.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.other_branch = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-001', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        Inventory.objects.create(
            product=self.product, branch=self.other_branch,
            quantity_on_hand=50, reorder_level=5,
        )

    _MISSING_BRANCH_MSG = (
        'Your account is not assigned to a branch, so it cannot record '
        'sales. Ask the Owner or an administrator to set your branch.'
    )

    def _branchless_staff(self):
        user = User.objects.create_user(
            username='otis', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=user, defaults={'staff_name': 'Otis', 'branch': None}
        )
        return user

    def _branchless_manager(self):
        user = User.objects.create_user(
            username='mara', password='pw', role=User.Role.BRANCH_MANAGER
        )
        BranchManagerProfile.objects.update_or_create(
            user=user, defaults={'manager_name': 'Mara', 'branch': None}
        )
        return user

    def test_branchless_staff_get_is_refused_not_shown_a_branch_selector(self):
        self.client.force_login(self._branchless_staff())
        response = self.client.get(reverse('transaction_add'), follow=True)
        self.assertTemplateNotUsed(response, 'sales/transaction_form.html')
        self.assertContains(response, self._MISSING_BRANCH_MSG)

    def test_branchless_manager_get_is_refused_not_shown_a_branch_selector(self):
        self.client.force_login(self._branchless_manager())
        response = self.client.get(reverse('transaction_add'), follow=True)
        self.assertTemplateNotUsed(response, 'sales/transaction_form.html')
        self.assertContains(response, self._MISSING_BRANCH_MSG)

    def test_branchless_staff_post_records_nothing_and_is_told_why(self):
        self.client.force_login(self._branchless_staff())
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)], 'quantity': ['2'],
            'amount_tendered': '200',
        }, follow=True)
        self.assertContains(response, self._MISSING_BRANCH_MSG)
        self.assertFalse(SalesTransaction.objects.exists())

    def test_staff_cannot_post_a_transaction_against_a_branch_outside_their_scope(self):
        """The foreign branch id is supplied directly in POST, not via the
        rendered selector (Staff never get one). No sale is booked against it."""
        staff = User.objects.create_user(
            username='ana', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=staff, defaults={'staff_name': 'Ana', 'branch': self.branch}
        )
        self.client.force_login(staff)
        self.client.post(reverse('transaction_add'), {
            'branch': self.other_branch.pk, 'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
            'amount_tendered': '100',
        })
        # Nothing booked against the branch they tried to name...
        self.assertFalse(
            SalesTransaction.objects.filter(branch=self.other_branch).exists()
        )
        # ...the sale, if it lands at all, lands on their own pinned branch.
        for txn in SalesTransaction.objects.all():
            self.assertEqual(txn.branch_id, self.branch.pk)

    def test_owner_can_still_pick_any_branch(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('transaction_add'), {
            'branch': self.other_branch.pk, 'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
            'amount_tendered': '100',
        })
        txn = SalesTransaction.objects.get()
        self.assertEqual(txn.branch_id, self.other_branch.pk)


class VoidPreservesConfirmationTrailTests(TestCase):
    """F-10 — transaction_set_status used to overwrite billing.confirmed_by
    with whoever voided a completed sale, while leaving date_confirmed at the
    original confirmation time. The record then read "confirmed by [voider] on
    [original date]" — a pairing that never happened — and the real confirmer
    was lost. The void now writes its own voided_by / date_voided pair and
    never touches the confirmation trail.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.checker = User.objects.create_user(
            username='checker', password='pw', role=User.Role.BRANCH_MANAGER
        )
        self.product = Product.objects.create(
            sku_code='SKU-001', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )

    def _completed_sale_with_confirmed_payment(self):
        """A completed GCash walk-in whose payment was later confirmed by
        someone other than the owner — mirrors what billing_confirm writes
        (status Paid, confirmed_by = the checker, date_confirmed = now)."""
        self.client.force_login(self.owner)
        self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'GCash',
            'product_id': [str(self.product.pk)], 'quantity': ['2'],
        })
        txn = SalesTransaction.objects.get()
        billing = txn.billing_record
        billing.status = BillingRecord.PaymentStatus.PAID
        billing.confirmed_by = self.checker
        billing.date_confirmed = timezone.now()
        billing.save()
        return txn, billing

    # notify() is patched here for the same reason noted on the tests below
    # in OrderCancellationReasonAndReplacesTests — a known, unrelated,
    # pre-existing column-charset issue on accounts_notification, inside the
    # same atomic block as the cancellation this test actually checks.
    @mock.patch('core.notifications.notify')
    def test_voiding_a_completed_sale_keeps_the_original_confirmation_trail(self, mock_notify):
        txn, billing = self._completed_sale_with_confirmed_payment()
        original_confirmed_by = billing.confirmed_by_id
        original_date_confirmed = billing.date_confirmed

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Cancelled']),
            {'cancel_reason': 'Customer changed their mind'},
        )
        self.assertRedirects(
            response, reverse('transaction_detail', args=[txn.pk])
        )

        billing.refresh_from_db()
        # The billing record followed the order into Cancelled...
        self.assertEqual(billing.status, BillingRecord.PaymentStatus.CANCELLED)
        # ...but the confirmation trail is exactly as the checker left it.
        self.assertEqual(billing.confirmed_by_id, original_confirmed_by)
        self.assertEqual(billing.confirmed_by, self.checker)
        self.assertEqual(billing.date_confirmed, original_date_confirmed)
        # The void is recorded on its own pair.
        self.assertEqual(billing.voided_by, self.owner)
        self.assertIsNotNone(billing.date_voided)
        self.assertGreaterEqual(billing.date_voided, original_date_confirmed)


class OrderCancellationReasonAndReplacesTests(TestCase):
    """Line items are immutable once an order exists — correcting one means
    cancelling it (with a reason) and ringing up a new order linked back to
    it via `replaces`, never editing the original."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='oc-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.other_branch = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-OC', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.client.force_login(self.owner)

    def _pending_order(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch, order_status=SalesTransaction.OrderStatus.PENDING,
            total_amount=100, transaction_type=SalesTransaction.TransactionType.ONLINE,
        )
        return txn

    def test_cancelling_without_a_reason_is_refused(self):
        txn = self._pending_order()
        response = self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Cancelled'])
        )
        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Pending')
        self.assertEqual(txn.cancel_reason, '')

    # notify() is patched in the two tests below only — cancelling an order
    # sends one, and its message includes a peso-sign amount that a known,
    # pre-existing, unrelated column-charset issue on accounts_notification
    # currently chokes on. Both calls happen inside transaction_set_status's
    # own atomic block, so an unhandled error there would roll back the
    # cancellation itself and make these tests fail for a reason that has
    # nothing to do with what they're actually checking.
    @mock.patch('core.notifications.notify')
    def test_cancelling_with_a_reason_saves_it(self, mock_notify):
        txn = self._pending_order()
        response = self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Cancelled']),
            {'cancel_reason': 'Customer requested a different product'},
        )
        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Cancelled')
        self.assertEqual(txn.cancel_reason, 'Customer requested a different product')

    @mock.patch('core.notifications.notify')
    def test_a_new_sale_can_link_to_the_cancelled_order_it_replaces(self, mock_notify):
        original = self._pending_order()
        self.client.post(
            reverse('transaction_set_status', args=[original.pk, 'Cancelled']),
            {'cancel_reason': 'Wrong item rung up'},
        )

        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
            'amount_tendered': '100', 'replaces': str(original.pk),
        })
        new_order = SalesTransaction.objects.exclude(pk=original.pk).get()
        self.assertRedirects(response, reverse('transaction_detail', args=[new_order.pk]))
        self.assertEqual(new_order.replaces_id, original.pk)

        original.refresh_from_db()
        self.assertEqual(list(original.replaced_by.all()), [new_order])

    def test_replaces_is_dropped_if_the_order_is_not_actually_cancelled(self):
        still_pending = self._pending_order()
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
            'amount_tendered': '100', 'replaces': str(still_pending.pk),
        })
        new_order = SalesTransaction.objects.exclude(pk=still_pending.pk).get()
        self.assertRedirects(response, reverse('transaction_detail', args=[new_order.pk]))
        self.assertIsNone(new_order.replaces_id)

    def test_replaces_is_dropped_if_the_cancelled_order_is_a_different_branch(self):
        Inventory.objects.create(
            product=self.product, branch=self.other_branch,
            quantity_on_hand=10, reorder_level=1,
        )
        other_branch_cancelled = SalesTransaction.objects.create(
            branch=self.other_branch, order_status=SalesTransaction.OrderStatus.CANCELLED,
            total_amount=100, transaction_type=SalesTransaction.TransactionType.ONLINE,
        )
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
            'amount_tendered': '100', 'replaces': str(other_branch_cancelled.pk),
        })
        new_order = SalesTransaction.objects.get(branch=self.branch)
        self.assertRedirects(response, reverse('transaction_detail', args=[new_order.pk]))
        self.assertIsNone(new_order.replaces_id)
