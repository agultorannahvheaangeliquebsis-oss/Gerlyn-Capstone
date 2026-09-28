"""P19 — every purchase order records where its money comes from. Required at
creation (web and API), editable until the terms are agreed, then locked."""
import json

from django.test import TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, User
from branches.models import Branch
from inventory.models import Product
from procurement.models import Procurement, SupplierProduct
from procurement.services import FundingError, clean_funding, set_funding

P = Procurement.Status


class FundingSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='p19-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='P19 Home', location='Baliuag', owner=cls.owner)
        cls.other_branch = Branch.objects.create(
            branch_name='P19 Other', location='Makinabang', owner=cls.owner)
        cls.manager = User.objects.create_user(
            username='p19-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        BranchManagerProfile.objects.update_or_create(
            user=cls.manager,
            defaults={'branch': cls.branch, 'manager_name': 'M', 'contact_number': '0'})
        cls.supplier = User.objects.create_user(
            username='p19-supplier', password='pw', role=User.Role.SUPPLIER)
        cls.product = Product.objects.create(
            sku_code='P19-1', product_name='P19 Rice', unit_price=100)
        SupplierProduct.objects.create(
            supplier=cls.supplier, product=cls.product, current_cost_price=50)

    def _form(self, **extra):
        data = {
            'branch': self.branch.pk, 'supplier': self.supplier.pk,
            'order_date': '2026-01-01',
            'product_id': [str(self.product.pk)], 'quantity_ordered': ['3'],
        }
        data.update(extra)
        return data

    def _po(self, status=P.APPROVED, branch=None, **extra):
        return Procurement.objects.create(
            branch=branch or self.branch, supplier=self.supplier,
            order_date='2026-01-01', status=status, total_cost=0, **extra)

    # ---- creation: web -------------------------------------------------------

    def test_web_creation_stores_source_and_note(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('procurement_add'), self._form(
            funding_source='Bank Transfer', funding_note='BDO ending 1234'))
        po = Procurement.objects.get()
        self.assertEqual(po.funding_source, 'Bank Transfer')
        self.assertEqual(po.funding_note, 'BDO ending 1234')

    def test_web_creation_requires_a_valid_source(self):
        self.client.force_login(self.owner)
        for bad in ('', 'Bitcoin'):
            response = self.client.post(
                reverse('procurement_add'), self._form(funding_source=bad))
            self.assertContains(response, 'where the money for this order comes from')
        self.assertFalse(Procurement.objects.exists())

    def test_other_needs_a_note(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('procurement_add'), self._form(funding_source='Other'))
        self.assertContains(response, 'Describe the funding source')
        self.assertFalse(Procurement.objects.exists())
        self.client.post(reverse('procurement_add'), self._form(
            funding_source='Other', funding_note='Loan from a relative'))
        self.assertEqual(Procurement.objects.get().funding_source, 'Other')

    def test_a_manager_raised_draft_records_the_source_too(self):
        self.client.force_login(self.manager)
        self.client.post(reverse('procurement_add'), self._form(funding_source='GCash'))
        po = Procurement.objects.get()
        self.assertEqual(po.status, P.DRAFT)
        self.assertEqual(po.funding_source, 'GCash')

    def test_form_offers_every_choice(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('procurement_list'))
        for label in ('Cash on Hand', 'Bank Transfer', 'GCash', "Owner&#x27;s Funds", 'Other'):
            self.assertContains(response, label)

    # ---- creation: API -------------------------------------------------------

    def _api(self, **extra):
        body = {
            'branchId': self.branch.pk, 'supplierId': self.supplier.pk,
            'orderDate': '2026-01-01',
            'items': [{'productId': self.product.pk, 'quantityOrdered': 1}],
        }
        body.update(extra)
        self.client.force_login(self.owner)
        return self.client.post(
            '/api/procurement/', data=json.dumps(body), content_type='application/json')

    def test_api_requires_a_source(self):
        response = self._api()
        self.assertEqual(response.status_code, 400)
        self.assertIn('fundingSource', response.json())

    def test_api_stores_source_and_enforces_the_other_rule(self):
        self.assertEqual(self._api(fundingSource='Other').status_code, 400)
        created = self._api(fundingSource='Cash on Hand', fundingNote='petty cash')
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()['fundingSource'], 'Cash on Hand')
        self.assertEqual(Procurement.objects.get().funding_note, 'petty cash')

    def test_api_rejects_an_unknown_source(self):
        self.assertEqual(self._api(fundingSource='Bitcoin').status_code, 400)

    # ---- editing and locking ---------------------------------------------------

    def test_editable_before_terms_are_agreed(self):
        self.client.force_login(self.owner)
        for status in (P.DRAFT, P.APPROVED, P.PENDING_OWNER_REVIEW, P.PENDING_SUPPLIER_REVIEW):
            po = self._po(status=status, funding_source='Cash on Hand')
            self.client.post(reverse('procurement_set_funding', args=[po.pk]),
                             {'funding_source': 'GCash'})
            po.refresh_from_db()
            self.assertEqual(po.funding_source, 'GCash', status)

    def test_locked_once_terms_are_agreed_or_later(self):
        self.client.force_login(self.owner)
        for status in (P.TERMS_AGREED, P.INCOMING, P.PARTIALLY_RECEIVED, P.RECEIVED, P.CANCELLED):
            po = self._po(status=status, funding_source='Cash on Hand')
            self.client.post(reverse('procurement_set_funding', args=[po.pk]),
                             {'funding_source': 'GCash'})
            po.refresh_from_db()
            self.assertEqual(po.funding_source, 'Cash on Hand', status)

    def test_detail_page_shows_the_form_only_while_editable(self):
        self.client.force_login(self.owner)
        open_po = self._po(status=P.APPROVED, funding_source='GCash')
        locked_po = self._po(status=P.INCOMING, funding_source='GCash')
        self.assertContains(self.client.get(reverse('procurement_detail', args=[open_po.pk])),
                            'Save funding source')
        page = self.client.get(reverse('procurement_detail', args=[locked_po.pk]))
        self.assertNotContains(page, 'Save funding source')
        self.assertContains(page, 'GCash')

    def test_legacy_order_without_a_source_can_be_filled_in(self):
        self.client.force_login(self.owner)
        po = self._po(status=P.APPROVED)
        self.assertContains(self.client.get(reverse('procurement_detail', args=[po.pk])),
                            'Not recorded')
        self.client.post(reverse('procurement_set_funding', args=[po.pk]),
                         {'funding_source': "Owner's Funds"})
        po.refresh_from_db()
        self.assertEqual(po.funding_source, "Owner's Funds")

    def test_manager_is_limited_to_their_own_branch(self):
        foreign = self._po(branch=self.other_branch, funding_source='Cash on Hand')
        own = self._po(funding_source='Cash on Hand')
        self.client.force_login(self.manager)
        self.client.post(reverse('procurement_set_funding', args=[foreign.pk]),
                         {'funding_source': 'GCash'})
        self.client.post(reverse('procurement_set_funding', args=[own.pk]),
                         {'funding_source': 'GCash'})
        foreign.refresh_from_db()
        own.refresh_from_db()
        self.assertEqual(foreign.funding_source, 'Cash on Hand')
        self.assertEqual(own.funding_source, 'GCash')

    def test_suppliers_cannot_change_it(self):
        po = self._po(funding_source='Cash on Hand')
        self.client.force_login(self.supplier)
        self.client.post(reverse('procurement_set_funding', args=[po.pk]),
                         {'funding_source': 'GCash'})
        po.refresh_from_db()
        self.assertEqual(po.funding_source, 'Cash on Hand')

    def test_get_is_not_allowed(self):
        po = self._po()
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse('procurement_set_funding', args=[po.pk])).status_code, 405)

    def test_invalid_edits_change_nothing(self):
        po = self._po(funding_source='Cash on Hand')
        self.client.force_login(self.owner)
        url = reverse('procurement_set_funding', args=[po.pk])
        self.client.post(url, {'funding_source': 'Bitcoin'})
        self.client.post(url, {'funding_source': 'Other', 'funding_note': ''})
        po.refresh_from_db()
        self.assertEqual(po.funding_source, 'Cash on Hand')

    def test_documents_print_the_source(self):
        po = self._po(funding_source='Bank Transfer', funding_note='BDO')
        self.client.force_login(self.owner)
        response = self.client.get(reverse('procurement_print', args=[po.pk]))
        self.assertContains(response, 'Funding: Bank Transfer (BDO)')

    # ---- service ---------------------------------------------------------------

    def test_service_helpers(self):
        self.assertEqual(clean_funding(' GCash ', ' x '), ('GCash', 'x'))
        with self.assertRaises(FundingError):
            clean_funding('Other', '   ')
        with self.assertRaises(FundingError):
            clean_funding('GCash', 'x' * 201)
        po = self._po(status=P.TERMS_AGREED, funding_source='GCash')
        with self.assertRaises(FundingError):
            set_funding(po, 'Cash on Hand', '')
