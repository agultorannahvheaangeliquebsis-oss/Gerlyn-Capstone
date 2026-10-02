"""Walk-in Sale is its own page (sales/transaction_form.html) reached from
the Order Console's "New Walk-in Sale" link, not a modal on the console.

These pin down that a recorded sale still creates the same transaction,
items, stock movement and paid billing record, and that a rejected submit
re-renders the form with the basket intact instead of redirecting and
throwing it away.
"""
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from importlib import import_module
from unittest import mock

from django.conf import settings
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connections
from django.db.models import Sum
from django.test import Client, RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import BranchManagerProfile, Notification, StaffProfile, User
from billing.models import BillingRecord
from branches.models import Branch
from core.module_permissions import BILLING, DEFAULT_STAFF_MODULES, SALES_POS
from inventory.models import Batch, Inventory, Product, StockMovement
from sales.models import OrderNumberSequence, Refund, RefundItem, SalesItem, SalesTransaction
from sales.views import transaction_refund

SessionStore = import_module(settings.SESSION_ENGINE).SessionStore


def pos_payment_evidence(method):
    """The reference number / proof a walk-in of `method` must post.

    A remote method settles away from the counter, so transaction_add asks
    BillingRecord.missing_payment_evidence for the same evidence Billing's own
    Record Payment form does — GCash needs both, Card the screenshot, Cash
    neither. Built from the model's own sets so a method moving between them
    does not leave every POS test posting the wrong fields.

    Shared with sales.tests_pos_payment_evidence, which covers the rule itself.
    """
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile

    payload = {'receipt_reference': f'TEST-{method}'}
    if method in BillingRecord.REFERENCE_REQUIRED_METHODS:
        # GCash's own rule is exactly 13 digits (billing.validators).
        payload['reference_number'] = '1234567890123'
    if method in BillingRecord.PROOF_REQUIRED_METHODS:
        # A genuinely valid PNG — the upload check opens it with Pillow.
        from PIL import Image

        buf = io.BytesIO()
        Image.new('RGB', (2, 2), color='white').save(buf, format='PNG')
        payload['proof_of_payment'] = SimpleUploadedFile(
            'proof.png', buf.getvalue(), content_type='image/png'
        )
    return payload


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
        response = self.client.get(reverse('transaction_add'), {'branch': self.branch.pk})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'sales/transaction_form.html')
        self.assertIn(self.product, list(response.context['pos_products']))
        self.assertContains(response, 'id="pos-form"')
        self.assertContains(response, 'Manual Sales Entry')
        self.assertContains(
            response,
            'Use only for transactions not captured in the existing POS import.',
        )
        self.assertContains(response, 'name="receipt_reference"')

    def test_an_owner_sees_no_products_until_a_branch_is_chosen(self):
        """An Owner isn't pinned to one branch, so the product dropdown has
        nowhere to scope to until they pick one — showing every product in
        the system here (possibly thousands) defeats the point of scoping
        it at all. The Items card stays hidden entirely rather than offer
        an unscoped list."""
        response = self.client.get(reverse('transaction_add'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['pos_products']), [])
        self.assertIsNone(response.context['pos_effective_branch'])
        self.assertNotContains(response, 'id="pos-item-rows"')
        self.assertContains(response, 'Choose a branch in the Order Summary to start adding items')

    def test_choosing_a_branch_scopes_the_product_list(self):
        other_branch = Branch.objects.create(
            branch_name='Other Branch', location='Baliuag', owner=self.owner,
        )
        other_product = Product.objects.create(
            sku_code='SKU-OTHER', product_name='Only At Other Branch',
            unit_price=50,
        )
        Inventory.objects.create(
            product=other_product, branch=other_branch, quantity_on_hand=10,
        )

        response = self.client.get(reverse('transaction_add'), {'branch': self.branch.pk})

        products = list(response.context['pos_products'])
        self.assertIn(self.product, products)
        self.assertNotIn(other_product, products)

    def test_menu_renders_a_card_and_a_category_tab_per_carried_product(self):
        """The Walk-in Sale menu is a card grid with category tabs. Each
        card carries the product id and pricing flags the cart JS reads, and
        the tabs list only categories this branch actually has a product in."""
        from inventory.models import ProductCategory
        grains = ProductCategory.objects.create(category_name='Grains')
        ProductCategory.objects.create(category_name='Never Stocked Here')
        self.product.category = grains
        self.product.save(update_fields=['category'])

        response = self.client.get(reverse('transaction_add'), {'branch': self.branch.pk})

        self.assertEqual(
            [c.category_name for c in response.context['pos_categories']], ['Grains'],
        )
        self.assertContains(response, f'data-cat="{grains.pk}"')
        self.assertNotContains(response, 'Never Stocked Here')
        self.assertContains(response, f'data-product-id="{self.product.pk}"')
        self.assertContains(response, 'data-retail="100.00"')
        self.assertContains(response, 'id="pos-item-rows"')

    def test_console_links_to_the_walk_in_sale_page_for_a_user_who_may_sell(self):
        response = self.client.get(reverse('transaction_list'))
        self.assertTrue(response.context['can_pos'])
        self.assertContains(response, reverse('transaction_add'))

    def test_recording_a_sale_still_deducts_stock_and_marks_it_paid(self):
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk,
            'receipt_reference': 'OR-MANUAL-001',
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
        self.assertEqual(txn.source, 'Manual Entry')
        self.assertEqual(txn.pos_invoice_number, 'OR-MANUAL-001')

        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity_on_hand, 47)
        self.assertEqual(txn.billing_record.status, 'Paid')
        self.assertEqual(txn.billing_record.amount_due, 300)

    def test_manual_entry_rejects_an_imported_reference_in_the_same_branch(self):
        SalesTransaction.objects.create(
            branch=self.branch,
            source='ZANK POS',
            pos_invoice_number='OR-IMPORTED-001',
            total_amount=100,
        )

        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk,
            'receipt_reference': 'or-imported-001',
            'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)],
            'quantity': ['1'],
            'amount_tendered': '100',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'That receipt/reference number is already recorded for this branch. '
            'Check the POS import before creating a manual entry.',
            response.context['pos_errors'],
        )
        self.assertEqual(SalesTransaction.objects.count(), 1)

    def test_a_first_time_sale_needs_no_receipt_reference(self):
        """KATRINA-REG-001 / BR-KAT-002: the reference is only for
        reconciling against an imported/external POS transaction. A
        legitimate first-time walk-in sale has nothing to reference and
        must not be blocked for lacking one."""
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk,
            'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)],
            'quantity': ['1'],
            'amount_tendered': '100',
        })

        self.assertEqual(response.status_code, 302)
        txn = SalesTransaction.objects.get()
        self.assertEqual(txn.source, 'Manual Entry')
        # None, not '' — stays outside the branch + pos_invoice_number
        # uniqueness rule, so a second reference-less sale at this branch
        # (below) is not treated as a duplicate of this one.
        self.assertIsNone(txn.pos_invoice_number)

    def test_two_reference_less_sales_at_one_branch_are_not_duplicates(self):
        for _ in range(2):
            response = self.client.post(reverse('transaction_add'), {
                'branch': self.branch.pk,
                'mode_of_payment': 'Cash',
                'product_id': [str(self.product.pk)],
                'quantity': ['1'],
                'amount_tendered': '100',
            })
            self.assertEqual(response.status_code, 302)

        self.assertEqual(SalesTransaction.objects.count(), 2)

    def test_manual_reference_can_be_reused_at_another_branch(self):
        other = Branch.objects.create(
            branch_name='Other', location='Baliuag', owner=self.owner,
        )
        SalesTransaction.objects.create(
            branch=other, source='ZANK POS', pos_invoice_number='OR-SHARED',
            total_amount=100,
        )

        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk,
            'receipt_reference': 'OR-SHARED',
            'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)],
            'quantity': ['1'],
            'amount_tendered': '100',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SalesTransaction.objects.count(), 2)

    def test_cash_walk_in_is_still_auto_paid_and_confirmed_by_the_ringing_user(self):
        """A-1 regression — cash is settled at the counter, so a cash walk-in
        still lands Paid with the ringing user recorded as confirmer. The
        maker/checker rule only touches the methods that clear later."""
        self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'receipt_reference': 'TEST-CASH-CONFIRM',
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
            **pos_payment_evidence('GCash'),
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
            'receipt_reference': 'TEST-BULK',
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
        response = self.client.get(reverse('transaction_add'), {'branch': self.branch.pk})
        self.assertContains(response, 'data-tiers="[[10, 80.0]]"')

    def test_a_product_id_or_price_past_1000_is_not_comma_formatted(self):
        """USE_THOUSAND_SEPARATOR (settings.py) localises every numeric
        template variable by default, including a raw id or Decimal placed
        straight into an HTML attribute — value="1,016" is not a valid
        option value the row JS can match back to pos_stock_map, and
        data-retail="1,270.00" stops parseFloat at the comma, pricing the
        row at ₱1. See core.tests.test_price_over_1000_survives_an_edit for
        the same bug on the product price input. Real once this store's
        product/price numbers cross 1000, which a growing catalog will."""
        big_id_product = Product.objects.create(
            pk=1005, sku_code='SKU-1005', product_name='Past The Comma',
            unit_price=Decimal('1270.00'),
        )
        Inventory.objects.create(
            product=big_id_product, branch=self.branch, quantity_on_hand=5,
        )

        response = self.client.get(reverse('transaction_add'), {'branch': self.branch.pk})

        # The menu card carries the id the cart posts back as product_id.
        self.assertContains(response, 'data-product-id="1005"')
        self.assertNotContains(response, 'data-product-id="1,005"')
        self.assertContains(response, 'data-retail="1270.00"')
        self.assertNotContains(response, 'data-retail="1,270.00"')

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
            'receipt_reference': 'TEST-PINNED-BRANCH',
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

    def test_staff_without_sales_module_is_refused_the_receipt_page(self):
        """transaction_receipt was left out of MODULE_REGISTRY's SALES_POS
        entry — the same class of gap F-07 fixed for the API, just missed
        for this one template url. A revoked Staff account could still open
        the printable receipt directly by URL even though every other page
        in the section (detail, set-status, apply-discount) was gated.
        """
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, total_amount=100,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        response = self._client(self.staff_without).get(
            reverse('transaction_receipt', args=[txn.pk])
        )
        self.assertEqual(response.status_code, 302)


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
            'receipt_reference': 'TEST-PAYMENT-METHOD',
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
            **pos_payment_evidence('GCash'),
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
            'receipt_reference': 'TEST-OWNER-BRANCH',
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
            'receipt_reference': 'TEST-OWNER-BRANCH',
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
            **pos_payment_evidence('GCash'),
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
            'receipt_reference': 'TEST-REPLACE-CANCELLED',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
            'amount_tendered': '100', 'replaces': str(original.pk),
        })
        new_order = SalesTransaction.objects.exclude(pk=original.pk).get()
        self.assertRedirects(response, reverse('transaction_detail', args=[new_order.pk]))
        self.assertEqual(new_order.replaces_id, original.pk)
        # A link that stuck says nothing extra.
        self.assertNotIn(
            'could not be found',
            ' '.join(str(m) for m in get_messages(response.wsgi_request)),
        )

        original.refresh_from_db()
        self.assertEqual(list(original.replaced_by.all()), [new_order])

    def test_replaces_is_dropped_if_the_order_is_not_actually_cancelled(self):
        still_pending = self._pending_order()
        response = self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'receipt_reference': 'TEST-REPLACE-PENDING',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
            'amount_tendered': '100', 'replaces': str(still_pending.pk),
        })
        new_order = SalesTransaction.objects.exclude(pk=still_pending.pk).get()
        self.assertRedirects(response, reverse('transaction_detail', args=[new_order.pk]))
        self.assertIsNone(new_order.replaces_id)
        # ...and the cashier is told, rather than the link vanishing silently.
        self.assertIn(
            'could not be found',
            ' '.join(str(m) for m in get_messages(response.wsgi_request)),
        )

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
            'receipt_reference': 'TEST-REPLACE-OTHER-BRANCH',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
            'amount_tendered': '100', 'replaces': str(other_branch_cancelled.pk),
        })
        new_order = SalesTransaction.objects.get(branch=self.branch)
        self.assertRedirects(response, reverse('transaction_detail', args=[new_order.pk]))
        self.assertIsNone(new_order.replaces_id)
        self.assertIn(
            'could not be found',
            ' '.join(str(m) for m in get_messages(response.wsgi_request)),
        )


class PaymentGatesOrderCompletionTests(TestCase):
    """An order is only Completed once its payment is verified Paid. A walk-in
    settled in Cash is done at the counter (Completed + Paid); a GCash or Card
    walk-in stays Pending until its payment has been recorded and verified,
    then can be completed like any other order."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='pg-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-PG', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        self.inventory = Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.client.force_login(self.owner)

    def _online_order(self, status='Preparing'):
        return SalesTransaction.objects.create(
            branch=self.branch, order_status=status, total_amount=100,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            mode_of_payment=SalesTransaction.ModeOfPayment.COD,
        )

    def _set_billing(self, txn, status):
        BillingRecord.objects.filter(transaction=txn).update(status=status)

    def _complete(self, txn):
        return self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Completed'])
        )

    def _walk_in(self, method, qty='2', **extra):
        payload = {
            'branch': self.branch.pk, 'mode_of_payment': method,
            'receipt_reference': f'TEST-GATE-{method}',
            'product_id': [str(self.product.pk)], 'quantity': [qty],
        }
        if method == 'Cash':
            payload['amount_tendered'] = '1000'
        # GCash/Card settle away from the counter, so the sale now carries
        # their reference/screenshot the way Billing always demanded.
        payload.update(pos_payment_evidence(method))
        payload.update(extra)
        response = self.client.post(reverse('transaction_add'), payload)
        return SalesTransaction.objects.latest('pk'), response

    # -- the gate ---------------------------------------------------------------

    def test_completing_is_refused_while_payment_is_pending(self):
        txn = self._online_order()
        response = self._complete(txn)

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Preparing')
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('verified as Paid' in m for m in messages_))

    def test_completing_is_refused_while_payment_is_rejected(self):
        txn = self._online_order()
        self._set_billing(txn, BillingRecord.PaymentStatus.REJECTED)
        self._complete(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Preparing')

    def test_completing_works_once_payment_is_paid(self):
        txn = self._online_order()
        self._set_billing(txn, BillingRecord.PaymentStatus.PAID)
        self._complete(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Completed')

    def test_a_refused_completion_takes_no_stock(self):
        txn = self._online_order()
        from sales.models import SalesItem
        SalesItem.objects.create(
            transaction=txn, product=self.product, quantity_sold=5, unit_price=100,
        )
        self._complete(txn)

        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 50)

    def test_the_button_is_disabled_until_paid(self):
        txn = self._online_order()
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertContains(page, 'Payment must be verified as Paid first.')

        self._set_billing(txn, BillingRecord.PaymentStatus.PAID)
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertNotContains(page, 'Payment must be verified as Paid first.')

    # -- walk-in sales ----------------------------------------------------------

    def test_a_cash_walk_in_is_completed_and_paid_at_the_counter(self):
        txn, _ = self._walk_in('Cash')

        self.assertEqual(txn.order_status, 'Completed')
        self.assertEqual(txn.billing_record.status, 'Paid')

    def test_gcash_and_card_walk_ins_stay_pending_until_verified(self):
        for method in ('GCash', 'Card'):
            with self.subTest(method=method):
                txn, response = self._walk_in(method)

                self.assertEqual(txn.order_status, 'Pending')
                self.assertEqual(txn.billing_record.status, 'Pending')
                messages_ = [m.message for m in response.wsgi_request._messages]
                self.assertTrue(any('stays Pending' in m for m in messages_))

    def test_a_pending_walk_in_has_already_left_the_shelf(self):
        self._walk_in('GCash', qty='2')

        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 48)

    def test_a_pending_walk_in_only_offers_complete_or_cancel(self):
        txn, _ = self._walk_in('GCash')
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))

        self.assertEqual(page.context['next_actions'], ['Completed', 'Cancelled'])

    def test_a_pending_walk_in_completes_after_payment_is_verified_without_a_second_deduction(self):
        txn, _ = self._walk_in('GCash', qty='2')

        self._complete(txn)
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Pending')

        self._set_billing(txn, BillingRecord.PaymentStatus.PAID)
        self._complete(txn)
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Completed')
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 48)

    @mock.patch('core.notifications.notify')
    def test_cancelling_a_pending_walk_in_puts_its_stock_back(self, mock_notify):
        txn, _ = self._walk_in('GCash', qty='2')
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 48)

        self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Cancelled']),
            {'cancel_reason': 'Customer never paid'},
        )

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Cancelled')
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 50)


class OrderDetailPaymentVerificationTests(TestCase):
    """Financials UX Audit Phase 1 — Order Detail's Payment Status card
    grows Verify/Reject (and Re-open) controls, so a payment can be checked
    without leaving the order. These only prove the card *draws* the right
    thing for each state; billing_confirm's own tests
    (billing.tests.BillingConfirmReturnToOrderTests and friends) prove the
    transitions, scoping and maker/checker rule it posts to."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='odpv-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='ODPV Branch', location='Baliuag', owner=cls.owner
        )
        cls.staff = User.objects.create_user(
            username='odpv-staff', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=cls.staff, defaults={'staff_name': 'Sam', 'branch': cls.branch}
        )
        User.objects.filter(pk=cls.staff.pk).update(
            allowed_modules=[*DEFAULT_STAFF_MODULES, BILLING]
        )
        cls.staff.refresh_from_db()

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _gcash_order(self, staff=None, reference='', proof=False):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=staff, total_amount=250,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        record = txn.billing_record
        # billing.signals auto-creates the record as Cash regardless of the
        # transaction's own mode_of_payment — set explicitly, or these tests'
        # "reference/proof missing" scenarios would be evaluated against Cash,
        # which needs neither and always reports nothing missing.
        record.payment_method = BillingRecord.PaymentMethod.GCASH
        record.reference_number = reference
        record.save(update_fields=['payment_method', 'reference_number'])
        if proof:
            from billing.tests import _valid_png_file
            record.proof_of_payment.save('proof.png', _valid_png_file(), save=True)
        return txn

    def test_pending_payment_offers_verify_and_reject(self):
        txn = self._gcash_order(reference='GC-1', proof=True)
        page = self._client(self.owner).get(reverse('transaction_detail', args=[txn.pk]))

        self.assertContains(page, 'Verify Payment')
        self.assertContains(page, 'Reject Payment')
        self.assertContains(page, f"action=\"{reverse('billing_confirm', args=[txn.billing_record.pk])}\"")
        self.assertContains(page, 'name="return_to" value="order"')

    def test_self_recorded_payment_shows_inline_message_not_a_dead_button(self):
        txn = self._gcash_order(staff=self.staff, reference='GC-1', proof=True)
        page = self._client(self.staff).get(reverse('transaction_detail', args=[txn.pk]))

        self.assertContains(
            page,
            'This payment was recorded by you. Another staff member or '
            'manager must verify it.',
        )
        self.assertNotContains(page, 'Reject Payment')
        self.assertNotContains(page, '>Verify Payment<')

    def test_missing_evidence_disables_verify_but_still_offers_reject(self):
        txn = self._gcash_order()  # no reference, no proof
        page = self._client(self.owner).get(reverse('transaction_detail', args=[txn.pk]))

        self.assertContains(page, 'Needs its reference number and proof of payment')
        self.assertContains(page, '<button type="button" class="btn btn-primary btn-sm od-btn" disabled>Verify Payment</button>')
        self.assertContains(page, 'Reject Payment')

    def test_pending_pwd_request_blocks_verify(self):
        txn = self._gcash_order(reference='GC-1', proof=True)
        txn.transaction_type = SalesTransaction.TransactionType.ONLINE
        txn.pwd_request_status = SalesTransaction.PwdRequestStatus.PENDING
        txn.save(update_fields=['transaction_type', 'pwd_request_status'])
        page = self._client(self.owner).get(reverse('transaction_detail', args=[txn.pk]))

        self.assertContains(page, 'PWD discount request waiting for verification')
        self.assertContains(page, '<button type="button" class="btn btn-primary btn-sm od-btn" disabled>Verify Payment</button>')

    def test_rejected_payment_offers_reopen(self):
        txn = self._gcash_order(reference='GC-1', proof=True)
        BillingRecord.objects.filter(pk=txn.billing_record.pk).update(status='Rejected')
        page = self._client(self.owner).get(reverse('transaction_detail', args=[txn.pk]))

        self.assertContains(page, 'Re-open Payment')

    def test_verifying_from_order_detail_redirects_back_with_a_success_message(self):
        txn = self._gcash_order(reference='GC-1', proof=True)
        record = txn.billing_record
        client = self._client(self.owner)
        with mock.patch('billing.notifications.notify_payment_decision'):
            response = client.post(
                reverse('billing_confirm', args=[record.pk]),
                {'status': 'Paid', 'return_to': 'order'},
                follow=True,
            )

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        messages_ = [str(m) for m in response.context['messages']]
        self.assertTrue(any('marked as Paid' in m for m in messages_))
        record.refresh_from_db()
        self.assertEqual(record.status, 'Paid')


class OrderDetailPaymentModuleGateTests(TestCase):
    """Financials UX Audit Phase 1 follow-up — transaction_detail is gated to
    the Sales/Order Console module (SALES_POS), not Billing, so a Staff
    account could reach Order Detail with SALES_POS alone and see live
    Verify/Reject/Re-open/Record Payment buttons that ModulePermissionMiddleware
    would then bounce away from billing_confirm/billing_create (both are
    Billing-module urls — see core.module_permissions.BILLING). These prove
    the Order Detail template now hides those actions for a user who cannot
    actually use them, without changing what the backend accepts — the
    default-STAFF and STAFF+BILLING backend outcomes are already covered by
    billing.tests.BillingAPIModulePermissionTests and
    BillingConfirmReturnToOrderTests; this class is about what the page
    *draws*, plus one test tying the two together."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='odpg-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='ODPG Branch', location='Baliuag', owner=cls.owner
        )
        # Default STAFF: Sales/Order Console but not Billing — exactly the
        # mismatch this audit is about.
        cls.staff_sales_only = User.objects.create_user(
            username='odpg-staff-sales', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=cls.staff_sales_only, defaults={'staff_name': 'Sales', 'branch': cls.branch}
        )
        User.objects.filter(pk=cls.staff_sales_only.pk).update(
            allowed_modules=list(DEFAULT_STAFF_MODULES)  # SALES_POS, no BILLING
        )
        cls.staff_sales_only.refresh_from_db()
        assert SALES_POS in cls.staff_sales_only.allowed_modules
        assert BILLING not in cls.staff_sales_only.allowed_modules

        # Same Staff role, but the Owner has also granted Billing.
        cls.staff_with_billing = User.objects.create_user(
            username='odpg-staff-billing', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=cls.staff_with_billing, defaults={'staff_name': 'Billing', 'branch': cls.branch}
        )
        User.objects.filter(pk=cls.staff_with_billing.pk).update(
            allowed_modules=[*DEFAULT_STAFF_MODULES, BILLING]
        )
        cls.staff_with_billing.refresh_from_db()

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _gcash_order(self, staff=None, reference='GC-1', proof=True):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=staff, total_amount=250,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        record = txn.billing_record
        record.payment_method = BillingRecord.PaymentMethod.GCASH
        record.reference_number = reference
        record.save(update_fields=['payment_method', 'reference_number'])
        if proof:
            from billing.tests import _valid_png_file
            record.proof_of_payment.save('proof.png', _valid_png_file(), save=True)
        return txn

    def test_staff_with_sales_pos_only_sees_no_verify_or_reject(self):
        txn = self._gcash_order()
        page = self._client(self.staff_sales_only).get(
            reverse('transaction_detail', args=[txn.pk])
        )

        self.assertNotContains(page, '>Verify Payment<')
        self.assertNotContains(page, 'Reject Payment')
        # The payment facts (Payment Status card) are still readable — only
        # the actions the user can't complete are gone.
        self.assertContains(page, 'Billing no.')

    def test_staff_with_sales_pos_only_sees_no_reopen(self):
        txn = self._gcash_order()
        BillingRecord.objects.filter(pk=txn.billing_record.pk).update(status='Rejected')
        page = self._client(self.staff_sales_only).get(
            reverse('transaction_detail', args=[txn.pk])
        )

        self.assertNotContains(page, 'Re-open Payment')

    def test_staff_with_sales_pos_only_sees_no_record_payment_link(self):
        # No billing record at all — Record Payment is the button that opens
        # billing_create, also a Billing-module url.
        txn = SalesTransaction.objects.create(
            branch=self.branch, total_amount=250,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        txn.billing_record.delete()
        page = self._client(self.staff_sales_only).get(
            reverse('transaction_detail', args=[txn.pk])
        )

        self.assertNotContains(page, 'Record Payment')

    def test_staff_with_both_modules_still_sees_the_actions(self):
        txn = self._gcash_order()
        page = self._client(self.staff_with_billing).get(
            reverse('transaction_detail', args=[txn.pk])
        )

        self.assertContains(page, '>Verify Payment<')
        self.assertContains(page, 'Reject Payment')

    def test_authorized_verifier_can_still_confirm_from_the_order(self):
        """The Owner (always authorized, module system doesn't apply) can
        still verify from Order Detail — the template gate doesn't get in
        an authorized user's way."""
        txn = self._gcash_order()
        record = txn.billing_record
        with mock.patch('billing.notifications.notify_payment_decision'):
            response = self._client(self.owner).post(
                reverse('billing_confirm', args=[record.pk]),
                {'status': 'Paid', 'return_to': 'order'},
            )

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        record.refresh_from_db()
        self.assertEqual(record.status, 'Paid')

    def test_hiding_the_button_matches_the_backend_still_refusing_the_post(self):
        """Ties the UI gate to the server gate this task must not weaken:
        the same Staff account the page now hides Verify Payment from is
        still refused if it posts to billing_confirm directly."""
        txn = self._gcash_order()
        record = txn.billing_record

        response = self._client(self.staff_sales_only).post(
            reverse('billing_confirm', args=[record.pk]),
            {'status': 'Paid', 'return_to': 'order'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(
            response.url, reverse('transaction_detail', args=[txn.pk])
        )
        record.refresh_from_db()
        self.assertEqual(record.status, 'Pending')
        self.assertIsNone(record.confirmed_by)


class RefundTests(TestCase):
    """Owner/Branch Manager only, on a Completed + Paid order, within
    SalesTransaction.REFUND_WINDOW of completion. Distinct from voiding a
    completed sale: a refund targets specific items and never touches order
    status — the Refund/RefundItem rows are the itemized record. Billing
    status DOES move (Paid -> Partially Refunded -> Refunded), so a refunded
    order is visible from the Billing list without opening each one."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='rf-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.other_branch = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        self.manager = User.objects.create_user(
            username='rf-manager', password='pw', role=User.Role.BRANCH_MANAGER
        )
        BranchManagerProfile.objects.update_or_create(
            user=self.manager, defaults={'manager_name': 'Manny', 'branch': self.branch}
        )
        self.staff = User.objects.create_user(
            username='rf-staff', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=self.staff, defaults={'staff_name': 'Sam', 'branch': self.branch}
        )
        User.objects.filter(pk=self.staff.pk).update(allowed_modules=[SALES_POS])
        self.product = Product.objects.create(
            sku_code='SKU-RF', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        self.inventory = Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.client.force_login(self.owner)

    def _order(self, quantity=5, unit_price=Decimal('100'), completed_at=None, branch=None):
        txn = SalesTransaction.objects.create(
            branch=branch or self.branch,
            order_status=SalesTransaction.OrderStatus.COMPLETED,
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            total_amount=quantity * unit_price,
            completed_at=completed_at or timezone.now(),
        )
        SalesItem.objects.create(
            transaction=txn, product=self.product,
            quantity_sold=quantity, unit_price=unit_price,
        )
        BillingRecord.objects.filter(transaction=txn).update(
            status=BillingRecord.PaymentStatus.PAID
        )
        return txn

    def _refund(self, txn, item, quantity, condition='Sellable',
                reason='Customer changed mind', refund_method='Cash', client=None,
                reason_code='Changed Mind'):
        client = client or self.client
        return client.post(reverse('transaction_refund', args=[txn.pk]), {
            'item_id': [str(item.pk)],
            f'quantity_{item.pk}': str(quantity),
            f'condition_{item.pk}': condition,
            'reason_code': reason_code,
            'reason': reason,
            'refund_method': refund_method,
        })

    def _refund_raw(self, txn, payload, client=None):
        client = client or self.client
        return client.post(reverse('transaction_refund', args=[txn.pk]), payload)

    def _detail(self, txn, client=None):
        client = client or self.client
        return client.get(reverse('transaction_detail', args=[txn.pk]))

    # -- the happy path -----------------------------------------------------

    @mock.patch('core.notifications.notify')
    def test_owner_can_refund_part_of_an_order(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()

        response = self._refund(txn, item, 2)

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        refund = Refund.objects.get()
        self.assertEqual(refund.transaction, txn)
        self.assertEqual(refund.total_amount, Decimal('200.00'))
        self.assertEqual(refund.refunded_by, self.owner)
        line = refund.items.get()
        self.assertEqual((line.sales_item, line.quantity, line.condition, line.amount),
                         (item, 2, 'Sellable', Decimal('200.00')))

    @mock.patch('core.notifications.notify')
    def test_order_status_is_untouched_but_billing_moves_to_partially_refunded(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()

        self._refund(txn, item, 2)

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Completed')
        self.assertEqual(txn.billing_record.status, 'Partially Refunded')

    @mock.patch('core.notifications.notify')
    def test_refunding_every_unit_moves_billing_to_refunded(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()

        self._refund(txn, item, 5)

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Completed')
        self.assertEqual(txn.billing_record.status, 'Refunded')

    @mock.patch('core.notifications.notify')
    def test_a_partially_refunded_order_can_be_refunded_again(self, mock_notify):
        """Partially Refunded must stay refundable for what's left — a
        billing status change from Paid on the first refund must not lock
        the order out of a second one for its remaining quantity."""
        txn = self._order(quantity=5)
        item = txn.items.get()
        self._refund(txn, item, 2)
        txn.refresh_from_db()
        self.assertEqual(txn.billing_record.status, 'Partially Refunded')

        response = self._refund(txn, item, 3)

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        self.assertEqual(Refund.objects.filter(transaction=txn).count(), 2)
        txn.refresh_from_db()
        self.assertEqual(txn.billing_record.status, 'Refunded')

    @mock.patch('core.notifications.notify')
    def test_a_fully_refunded_order_has_nothing_left_to_refund(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()
        self._refund(txn, item, 5)
        txn.refresh_from_db()
        self.assertEqual(txn.billing_record.status, 'Refunded')

        response = self._refund(txn, item, 1)

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        self.assertEqual(Refund.objects.filter(transaction=txn).count(), 1)  # the second attempt did nothing

    @mock.patch('core.notifications.notify')
    def test_a_refunded_order_shows_up_on_the_billing_list_without_opening_it(self, mock_notify):
        """The whole point: Billing (list, filters, report) previously had
        no way to tell a refunded order from any other Paid one short of
        opening it in Sales & Orders."""
        txn = self._order(quantity=5)
        item = txn.items.get()
        self._refund(txn, item, 5)

        response = self.client.get(reverse('billing_list'), {'status': 'Refunded'})

        self.assertContains(response, txn.order_number)
        self.assertEqual(response.context['counts']['refunded'], 1)

    @mock.patch('core.notifications.notify')
    def test_branch_manager_can_refund_their_own_branch(self, mock_notify):
        self.client.force_login(self.manager)
        txn = self._order(quantity=5)
        item = txn.items.get()

        self._refund(txn, item, 1)

        self.assertEqual(Refund.objects.count(), 1)

    # -- stock effects --------------------------------------------------------

    @mock.patch('core.notifications.notify')
    def test_sellable_condition_restocks_and_logs_a_customer_return(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()
        before = self.inventory.quantity_on_hand

        self._refund(txn, item, 2, condition='Sellable')

        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, before + 2)
        movement = StockMovement.objects.get(movement_type='Customer Return')
        self.assertEqual(movement.quantity_change, 2)
        self.assertEqual(movement.reference_id, txn.pk)
        self.assertTrue(Batch.objects.filter(
            product=self.product, branch=self.branch, quantity=2, expiry_date=None,
        ).exists())

    @mock.patch('core.notifications.notify')
    def test_damaged_condition_leaves_stock_untouched(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()
        before = self.inventory.quantity_on_hand

        self._refund(txn, item, 2, condition='Damaged')

        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, before)
        self.assertFalse(StockMovement.objects.filter(movement_type='Customer Return').exists())

    # -- quantity limits --------------------------------------------------------

    def test_cannot_refund_more_than_was_sold(self):
        txn = self._order(quantity=5)
        item = txn.items.get()

        response = self._refund(txn, item, 6)

        self.assertEqual(Refund.objects.count(), 0)
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('can still be refunded' in m for m in messages_))

    def test_a_duplicated_item_id_is_rejected(self):
        """A crafted POST repeating the same item_id used to see the same
        pre-loop 'remaining' figure twice — RefundItem rows for the request
        aren't written until every line has passed — letting one submission
        refund (and restock) more units than were ever sold."""
        txn = self._order(quantity=3)
        item = txn.items.get()
        before = self.inventory.quantity_on_hand

        response = self.client.post(reverse('transaction_refund', args=[txn.pk]), {
            'item_id': [str(item.pk), str(item.pk)],
            f'quantity_{item.pk}': '3',
            f'condition_{item.pk}': 'Sellable',
            'reason_code': 'Changed Mind',
            'reason': 'Duplicate line attempt',
            'refund_method': 'Cash',
        })

        self.assertEqual(Refund.objects.count(), 0)
        self.assertEqual(RefundItem.objects.count(), 0)
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('more than once' in m for m in messages_))
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, before)

    def test_a_duplicated_item_id_cannot_push_a_refund_past_quantity_sold(self):
        """Same defect, sized so that each duplicate individually looks
        valid (half of quantity_sold) but the pair together exceeds it —
        proving the rejection isn't just catching an oversized single line."""
        txn = self._order(quantity=4)
        item = txn.items.get()

        response = self._refund_raw(txn, {
            'item_id': [str(item.pk), str(item.pk)],
            f'quantity_{item.pk}': '3',
            f'condition_{item.pk}': 'Damaged',
            'reason_code': 'Changed Mind',
            'reason': 'Duplicate line attempt',
            'refund_method': 'Cash',
        })

        self.assertEqual(Refund.objects.count(), 0)
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('more than once' in m for m in messages_))

    @mock.patch('core.notifications.notify')
    def test_a_second_refund_cannot_exceed_what_is_left(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()
        self._refund(txn, item, 3)

        response = self._refund(txn, item, 3)

        self.assertEqual(RefundItem.objects.filter(refund__transaction=txn).count(), 1)
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('Only 2 unit' in m for m in messages_))

    @mock.patch('core.notifications.notify')
    def test_two_partial_refunds_can_together_cover_the_whole_line(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()
        self._refund(txn, item, 3)

        response = self._refund(txn, item, 2)

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        self.assertEqual(Refund.objects.filter(transaction=txn).count(), 2)
        self.assertEqual(
            RefundItem.objects.filter(sales_item=item).aggregate(total=Sum('quantity'))['total'],
            5,
        )

    @mock.patch('core.notifications.notify')
    def test_a_fully_refunded_item_no_longer_appears_as_refundable(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()
        self._refund(txn, item, 5)

        page = self._detail(txn)

        self.assertEqual(page.context['refundable_items'], [])
        self.assertNotContains(page, 'Issue refund')

    # -- eligibility ------------------------------------------------------------

    @mock.patch('core.notifications.notify')
    def test_staff_can_request_but_never_approve_their_own_refund(self, mock_notify):
        """P14 — Staff see the request form and can raise a refund, but it
        sits Pending: nothing is restocked, no money is recorded and billing
        is untouched until an Owner/Branch Manager approves it."""
        self.client.force_login(self.staff)
        txn = self._order(quantity=5)
        item = txn.items.get()
        before = self.inventory.quantity_on_hand

        page = self._detail(txn)
        self.assertEqual(len(page.context['refundable_items']), 1)
        self.assertFalse(page.context['can_decide_refunds'])

        self._refund(txn, item, 1)

        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(refund.requested_by, self.staff)
        self.assertIsNone(refund.refunded_by)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, before)
        self.assertEqual(txn.billing_record.__class__.objects.get(transaction=txn).status, 'Paid')

    def test_a_manager_cannot_refund_another_branchs_order(self):
        self.client.force_login(self.manager)
        txn = self._order(quantity=5, branch=self.other_branch)
        item = txn.items.get()

        response = self._refund(txn, item, 1)

        self.assertEqual(Refund.objects.count(), 0)
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('another branch' in m for m in messages_))

    def test_a_pending_order_cannot_be_refunded(self):
        txn = self._order(quantity=5)
        txn.order_status = SalesTransaction.OrderStatus.PENDING
        txn.save(update_fields=['order_status'])
        item = txn.items.get()

        response = self._refund(txn, item, 1)

        self.assertEqual(Refund.objects.count(), 0)
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('not eligible for a refund' in m for m in messages_))

    def test_an_unpaid_order_cannot_be_refunded(self):
        txn = self._order(quantity=5)
        BillingRecord.objects.filter(transaction=txn).update(
            status=BillingRecord.PaymentStatus.PENDING
        )
        item = txn.items.get()

        self._refund(txn, item, 1)

        self.assertEqual(Refund.objects.count(), 0)

    def test_the_refund_window_blocks_an_old_order(self):
        txn = self._order(
            quantity=5,
            completed_at=timezone.now() - SalesTransaction.REFUND_WINDOW - timedelta(days=1),
        )
        item = txn.items.get()

        page = self._detail(txn)
        self.assertEqual(page.context['refundable_items'], [])

        self._refund(txn, item, 1)
        self.assertEqual(Refund.objects.count(), 0)

    @mock.patch('core.notifications.notify')
    def test_within_the_refund_window_still_works(self, mock_notify):
        txn = self._order(
            quantity=5,
            completed_at=timezone.now() - SalesTransaction.REFUND_WINDOW + timedelta(days=1),
        )
        item = txn.items.get()

        self._refund(txn, item, 1)

        self.assertEqual(Refund.objects.count(), 1)

    def test_a_completed_order_with_no_completed_at_on_record_cannot_be_refunded(self):
        """Orders from before completed_at existed, never backfilled (or any
        other row with a null completion date) — no reliable window to
        measure from, so refund_window_expired() treats it as expired rather
        than guessing."""
        txn = self._order(quantity=5)
        SalesTransaction.objects.filter(pk=txn.pk).update(completed_at=None)
        txn.refresh_from_db()
        item = txn.items.get()

        self._refund(txn, item, 1)

        self.assertEqual(Refund.objects.count(), 0)

    # -- form validation --------------------------------------------------------

    def test_a_reason_code_is_required(self):
        txn = self._order(quantity=5)
        item = txn.items.get()

        self._refund(txn, item, 1, reason_code='')

        self.assertEqual(Refund.objects.count(), 0)

    def test_a_refund_method_is_required(self):
        txn = self._order(quantity=5)
        item = txn.items.get()

        self._refund(txn, item, 1, refund_method='')

        self.assertEqual(Refund.objects.count(), 0)

    def test_at_least_one_item_must_be_selected(self):
        txn = self._order(quantity=5)

        response = self.client.post(reverse('transaction_refund', args=[txn.pk]), {
            'reason_code': 'Changed Mind', 'reason': 'Nothing selected', 'refund_method': 'Cash',
        })

        self.assertEqual(Refund.objects.count(), 0)
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('Select at least one item' in m for m in messages_))

    def test_get_is_refused_without_side_effects(self):
        txn = self._order(quantity=5)

        response = self.client.get(reverse('transaction_refund', args=[txn.pk]))

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        self.assertEqual(Refund.objects.count(), 0)

    # -- amount + notification --------------------------------------------------

    @mock.patch('core.notifications.notify')
    def test_the_amount_uses_the_frozen_sale_price_not_the_current_listed_price(self, mock_notify):
        txn = self._order(quantity=2, unit_price=Decimal('80'))  # a discounted-at-sale-time price
        item = txn.items.get()
        self.product.unit_price = Decimal('999')
        self.product.save(update_fields=['unit_price'])

        self._refund(txn, item, 2)

        self.assertEqual(Refund.objects.get().total_amount, Decimal('160.00'))

    @mock.patch('core.notifications.notify')
    def test_a_refund_notifies_the_owner(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()

        self._refund(txn, item, 2)

        self.assertEqual(mock_notify.call_count, 1)
        args, kwargs = mock_notify.call_args
        self.assertTrue(args[2].startswith('Refund issued'))

    @mock.patch('core.notifications.notify')
    def test_refund_history_shows_on_the_order_page(self, mock_notify):
        txn = self._order(quantity=5)
        item = txn.items.get()
        self._refund(txn, item, 2, reason='Wrong size')

        page = self._detail(txn)

        self.assertContains(page, 'Wrong size')
        self.assertContains(page, '₱200.00')


class RefundConcurrencyTests(TransactionTestCase):
    """Two racing refunds against the same line, each individually valid
    (half of quantity_sold) but together exceeding it, must serialise to
    exactly one accepted refund — the same shape as billing's D-1 and
    sales' F-14 concurrency tests.

    TransactionTestCase + RequestFactory, not the test Client: under plain
    TestCase both threads share one uncommitted transaction so the second
    can't see the first's lock; the test Client's handler isn't built for
    concurrent use from threads. RequestFactory runs the real view body
    directly. Skipped on sqlite, where SELECT ... FOR UPDATE takes no lock
    and the race cannot be expressed; runs for real on MySQL/InnoDB.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.owner = User.objects.create_user(
            username='rfc-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='RFC Branch', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-RFC', product_name='Cooking Oil', unit_of_measure='Bottle',
            unit_price=100,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=50, reorder_level=5,
        )

    def _fresh_paid_order(self, quantity=5):
        txn = SalesTransaction.objects.create(
            branch=self.branch,
            order_status=SalesTransaction.OrderStatus.COMPLETED,
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            total_amount=quantity * 100,
            completed_at=timezone.now(),
        )
        item = SalesItem.objects.create(
            transaction=txn, product=self.product, quantity_sold=quantity, unit_price=100,
        )
        BillingRecord.objects.filter(transaction=txn).update(
            status=BillingRecord.PaymentStatus.PAID
        )
        return txn, item

    def _refund(self, pk, item_pk, quantity, results, index, barrier):
        outcome = {'status_code': None, 'messages': [], 'error': None}
        try:
            request = self.factory.post(f'/sales/{pk}/refund/', {
                'item_id': [str(item_pk)],
                f'quantity_{item_pk}': str(quantity),
                f'condition_{item_pk}': 'Damaged',
                'reason_code': 'Changed Mind',
                'reason': 'Racing refund',
                'refund_method': 'Cash',
            })
            request.user = self.owner
            request.session = SessionStore()
            request._messages = FallbackStorage(request)
            barrier.wait(timeout=10)          # both threads enter the view together
            with mock.patch('core.notifications.notify'):
                response = transaction_refund(request, pk)
            outcome['status_code'] = response.status_code
            outcome['messages'] = [str(m.message) for m in request._messages]
        except Exception as exc:              # recorded, re-asserted on the main thread
            outcome['error'] = repr(exc)
        finally:
            connections.close_all()
        results[index] = outcome

    def test_two_racing_refunds_cannot_together_exceed_quantity_sold(self):
        if connections['default'].vendor == 'sqlite':
            self.skipTest(
                'sqlite takes no row lock for SELECT ... FOR UPDATE, so this '
                'contention cannot be exercised. Runs for real on MySQL.'
            )

        # A single race can serialise by luck even with a broken lock; repeat.
        for attempt in range(5):
            txn, item = self._fresh_paid_order(quantity=5)
            results = [None, None]
            barrier = threading.Barrier(2)
            threads = [
                threading.Thread(
                    target=self._refund, args=(txn.pk, item.pk, 3, results, i, barrier)
                )
                for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            ctx = f'(attempt {attempt}) {results}'
            self.assertNotIn(None, results, f'a refund thread never returned {ctx}')

            # A loser may either be cleanly turned away by the remaining-
            # quantity check (302 + message) or have InnoDB abort its whole
            # transaction with a deadlock error (1213) — both are safe
            # outcomes (nothing commits), so either is accepted here. What
            # must never happen is two winners.
            for r in results:
                if r['error'] is not None:
                    self.assertIn('1213', r['error'], f'unexpected error {ctx}')

            # Exactly one racer's 3 units went through; the other, once the
            # lock released (or its transaction was rolled back), never
            # added to it.
            total_refunded = RefundItem.objects.filter(
                sales_item=item
            ).aggregate(total=Sum('quantity'))['total'] or 0
            self.assertEqual(total_refunded, 3, ctx)

            successes = sum(
                1 for r in results
                if any('recorded for' in m for m in r['messages'])
            )
            self.assertEqual(successes, 1, ctx)


class DeletedProductOnAnOldSaleTests(TestCase):
    """A Product can be hard-deleted from the catalog (inventory.views.
    product_delete) long after it was sold — SalesItem.product is SET_NULL
    precisely so the sale survives that. Voiding/cancelling or refunding an
    order with such a line used to crash trying to identify an Inventory row
    for a product that no longer exists (product is a required field on it).
    Reproduces the exact live bug: cancelling a Completed walk-in order whose
    product was deleted afterward raised IntegrityError."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='dp-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-DP', product_name='Discontinued Item',
            unit_of_measure='Piece', unit_price=50,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=20, reorder_level=5,
        )
        self.client.force_login(self.owner)

    def _completed_order_with_deleted_product(self, quantity=2):
        txn = SalesTransaction.objects.create(
            branch=self.branch, order_status=SalesTransaction.OrderStatus.COMPLETED,
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            total_amount=quantity * 50, completed_at=timezone.now(),
        )
        SalesItem.objects.create(
            transaction=txn, product=self.product,
            quantity_sold=quantity, unit_price=Decimal('50'),
        )
        BillingRecord.objects.filter(transaction=txn).update(
            status=BillingRecord.PaymentStatus.PAID
        )
        self.product.delete()  # SalesItem.product is now SET_NULL
        return txn

    @mock.patch('core.notifications.notify')
    def test_voiding_a_completed_sale_does_not_crash_and_says_why(self, mock_notify):
        txn = self._completed_order_with_deleted_product(quantity=2)

        response = self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Cancelled']),
            {'cancel_reason': 'Customer return, product discontinued'},
        )

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Cancelled')
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('removed from the catalog' in m for m in messages_))

    def test_viewing_the_order_does_not_crash(self):
        txn = self._completed_order_with_deleted_product()

        response = self.client.get(reverse('transaction_detail', args=[txn.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '(product removed)')

    @mock.patch('core.notifications.notify')
    def test_refunding_such_a_line_records_the_refund_without_crashing(self, mock_notify):
        txn = self._completed_order_with_deleted_product(quantity=2)
        item = txn.items.get()

        response = self.client.post(reverse('transaction_refund', args=[txn.pk]), {
            'item_id': [str(item.pk)],
            f'quantity_{item.pk}': '2',
            f'condition_{item.pk}': 'Sellable',
            'reason_code': 'Changed Mind',
            'reason': 'Discontinued item returned',
            'refund_method': 'Cash',
        })

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        self.assertEqual(Refund.objects.get().total_amount, Decimal('100.00'))
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('could not be added back to stock' in m for m in messages_))

    def test_completing_an_online_order_with_a_deleted_product_line_fails_cleanly(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch, order_status=SalesTransaction.OrderStatus.PREPARING,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            total_amount=100,
        )
        SalesItem.objects.create(
            transaction=txn, product=self.product, quantity_sold=2, unit_price=Decimal('50'),
        )
        BillingRecord.objects.filter(transaction=txn).update(
            status=BillingRecord.PaymentStatus.PAID
        )
        self.product.delete()

        response = self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Completed'])
        )

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Preparing')  # refused, not crashed
        messages_ = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any('Not enough stock for a product since removed' in m for m in messages_))
