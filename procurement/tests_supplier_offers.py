"""P01 — supplier product-offering workflow, plus the supplier's own ability to
remove a product from its catalog.

A supplier's self-add is now a Proposed offer the Owner must approve before it
can appear as a preferred supplier-product link; products that aren't on the
master list are requested and only become real products when the Owner
approves them.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import Notification, User
from branches.models import Branch
from inventory.models import Product, ProductCategory
from procurement.models import Procurement, ProcurementItem, SupplierProduct, SupplierProductRequest
from procurement.services import SupplierCostError, snapshot_supplier_costs, transfer_supplier_products


class OfferBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='p01-owner', password='pw', role=User.Role.OWNER)
        cls.manager = User.objects.create_user(
            username='p01-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        cls.supplier = User.objects.create_user(
            username='p01-supplier', password='pw', role=User.Role.SUPPLIER)
        cls.other_supplier = User.objects.create_user(
            username='p01-other', password='pw', role=User.Role.SUPPLIER)
        cls.branch = Branch.objects.create(
            branch_name='P01 Branch', location='Baliuag', owner=cls.owner)
        cls.rice = Product.objects.create(
            sku_code='P01-RICE', product_name='P01 Rice', unit_price=100)
        cls.sugar = Product.objects.create(
            sku_code='P01-SUGAR', product_name='P01 Sugar', unit_price=60)

    def offer(self, product, cost='42.00', **extra):
        self.client.force_login(self.supplier)
        data = {'product': product.pk, 'current_cost_price': cost}
        data.update(extra)
        return self.client.post(reverse('supplier_product_self_add'), data)

    def link(self, supplier=None, product=None, status=SupplierProduct.Status.APPROVED,
             cost='50.00'):
        return SupplierProduct.objects.create(
            supplier=supplier or self.supplier, product=product or self.rice,
            current_cost_price=Decimal(cost), status=status)

    def po(self, product, status, supplier=None):
        po = Procurement.objects.create(
            branch=self.branch, supplier=supplier or self.supplier,
            order_date=dt.date(2026, 1, 1), status=status, total_cost=0)
        ProcurementItem.objects.create(
            procurement=po, product=product, quantity_ordered=5)
        return po


class OfferTests(OfferBase):
    def test_self_add_creates_a_proposed_offer_with_cost_and_lead_time(self):
        self.offer(self.rice, cost='42.50', lead_time_days='3')
        link = SupplierProduct.objects.get(supplier=self.supplier, product=self.rice)
        self.assertEqual(link.status, SupplierProduct.Status.PROPOSED)
        self.assertEqual(link.current_cost_price, Decimal('42.50'))
        self.assertEqual(link.lead_time_days, 3)

    def test_offer_requires_a_positive_cost(self):
        for bad in ('', '0', '-5', 'abc'):
            self.offer(self.rice, cost=bad)
        self.assertFalse(SupplierProduct.objects.filter(supplier=self.supplier).exists())

    def test_owner_is_notified_of_an_offer(self):
        self.offer(self.rice)
        self.assertTrue(Notification.objects.filter(
            user=self.owner, event_type='supplier_offer_for_review').exists())
        # The supplier never notifies themselves, and Managers aren't routed
        # this event (the Owner alone decides).
        self.assertFalse(Notification.objects.filter(
            user=self.supplier, event_type='supplier_offer_for_review').exists())
        self.assertFalse(Notification.objects.filter(
            user=self.manager, event_type='supplier_offer_for_review').exists())

    def test_proposed_offer_is_not_required_to_manually_order_a_product(self):
        self.offer(self.rice)
        self.client.force_login(self.owner)
        response = self.client.post(reverse('procurement_add'), {
            'branch': self.branch.pk, 'supplier': self.supplier.pk,
            'order_date': '2026-01-01', 'funding_source': 'Cash on Hand',
            'product_id': [str(self.rice.pk)],
            'quantity_ordered': ['10'], 'cost_price': ['50'],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Procurement.objects.get().items.get().product, self.rice)

    def test_proposed_offer_is_absent_from_the_po_catalog_until_approved(self):
        self.offer(self.rice)
        self.client.force_login(self.owner)
        catalog = self.client.get(reverse('procurement_list')).context['supplier_catalog']
        self.assertNotIn(str(self.rice.pk), catalog.get(str(self.supplier.pk), {}))

        link = SupplierProduct.objects.get(supplier=self.supplier, product=self.rice)
        self.client.post(reverse('supplier_offer_review', args=[self.supplier.pk, link.pk]),
                         {'decision': 'approve'})
        catalog = self.client.get(reverse('procurement_list')).context['supplier_catalog']
        self.assertIn(str(self.rice.pk), catalog[str(self.supplier.pk)])

    def test_approve_marks_reviewer_and_notifies_the_supplier(self):
        self.offer(self.rice)
        link = SupplierProduct.objects.get(supplier=self.supplier, product=self.rice)
        self.client.force_login(self.owner)
        self.client.post(reverse('supplier_offer_review', args=[self.supplier.pk, link.pk]),
                         {'decision': 'approve'})
        link.refresh_from_db()
        self.assertEqual(link.status, SupplierProduct.Status.APPROVED)
        self.assertEqual(link.reviewed_by, self.owner)
        self.assertIsNotNone(link.reviewed_at)
        self.assertTrue(Notification.objects.filter(
            user=self.supplier, event_type='supplier_offer_decision').exists())

    def test_reject_keeps_the_note_and_a_reoffer_reopens_it(self):
        self.offer(self.rice, cost='42')
        link = SupplierProduct.objects.get(supplier=self.supplier, product=self.rice)
        self.client.force_login(self.owner)
        self.client.post(reverse('supplier_offer_review', args=[self.supplier.pk, link.pk]),
                         {'decision': 'reject', 'note': 'Too expensive'})
        link.refresh_from_db()
        self.assertEqual(link.status, SupplierProduct.Status.REJECTED)
        self.assertEqual(link.review_note, 'Too expensive')

        self.offer(self.rice, cost='38')
        link.refresh_from_db()
        self.assertEqual(link.status, SupplierProduct.Status.PROPOSED)
        self.assertEqual(link.current_cost_price, Decimal('38'))
        self.assertEqual(link.review_note, '')

    def test_an_offer_can_only_be_decided_once(self):
        link = self.link(status=SupplierProduct.Status.PROPOSED)
        self.client.force_login(self.owner)
        url = reverse('supplier_offer_review', args=[self.supplier.pk, link.pk])
        self.client.post(url, {'decision': 'approve'})
        self.client.post(url, {'decision': 'reject', 'note': 'changed my mind'})
        link.refresh_from_db()
        self.assertEqual(link.status, SupplierProduct.Status.APPROVED)

    def test_only_the_owner_can_review(self):
        link = self.link(status=SupplierProduct.Status.PROPOSED)
        for user in (self.manager, self.supplier):
            self.client.force_login(user)
            self.client.post(
                reverse('supplier_offer_review', args=[self.supplier.pk, link.pk]),
                {'decision': 'approve'})
        link.refresh_from_db()
        self.assertEqual(link.status, SupplierProduct.Status.PROPOSED)

    def test_owner_adding_directly_approves_a_pending_offer(self):
        self.offer(self.rice)
        self.client.force_login(self.owner)
        self.client.post(reverse('supplier_product_add', args=[self.supplier.pk]),
                         {'product': self.rice.pk})
        link = SupplierProduct.objects.get(supplier=self.supplier, product=self.rice)
        self.assertEqual(link.status, SupplierProduct.Status.APPROVED)

    def test_cost_snapshot_ignores_unapproved_offers(self):
        self.link(status=SupplierProduct.Status.PROPOSED)
        po = self.po(self.rice, Procurement.Status.DRAFT)
        with self.assertRaises(SupplierCostError):
            snapshot_supplier_costs(po)

    def test_transfer_moves_only_approved_offers(self):
        self.link(product=self.rice)
        self.link(product=self.sugar, status=SupplierProduct.Status.PROPOSED)
        transfer_supplier_products(
            [self.rice.pk, self.sugar.pk], self.supplier, self.other_supplier)
        self.assertTrue(SupplierProduct.objects.filter(
            supplier=self.other_supplier, product=self.rice).exists())
        self.assertTrue(SupplierProduct.objects.filter(
            supplier=self.supplier, product=self.sugar).exists())

    def test_supplier_detail_lists_pending_offers_for_the_owner(self):
        self.offer(self.rice)
        self.client.force_login(self.owner)
        response = self.client.get(reverse('supplier_detail', args=[self.supplier.pk]))
        self.assertContains(response, 'Awaiting a decision')
        self.assertContains(response, 'P01 Rice')

    def test_portal_narrows_the_picker_to_the_suppliers_category(self):
        from accounts.models import SupplierCategory
        pantry = ProductCategory.objects.create(category_name='P01 Pantry')
        self.rice.category = pantry
        self.rice.save()
        cat = SupplierCategory.objects.create(name='P01 Grocer')
        cat.product_categories.add(pantry)
        profile = self.supplier.supplier_profile
        profile.category = cat
        profile.save()
        self.client.force_login(self.supplier)

        narrowed = self.client.get(reverse('supplier_portal')).context['available_products']
        self.assertEqual(list(narrowed), [self.rice])
        widened = self.client.get(reverse('supplier_portal') + '?all_products=1')
        self.assertIn(self.sugar, list(widened.context['available_products']))


class NewProductRequestTests(OfferBase):
    def request_new(self, **extra):
        data = {'product_name': 'P01 Brand New Noodles', 'unit_of_measure': 'Pack',
                'proposed_cost': '8.00'}
        data.update(extra)
        self.client.force_login(self.supplier)
        return self.client.post(reverse('supplier_product_request_add'), data)

    def test_request_is_created_pending_and_notifies_the_owner(self):
        self.request_new(note='Beef flavour')
        req = SupplierProductRequest.objects.get()
        self.assertEqual(req.status, SupplierProductRequest.Status.PENDING)
        self.assertEqual(req.proposed_cost, Decimal('8.00'))
        self.assertFalse(Product.objects.filter(product_name='P01 Brand New Noodles').exists())
        self.assertTrue(Notification.objects.filter(
            user=self.owner, event_type='supplier_offer_for_review').exists())

    def test_invalid_requests_are_refused(self):
        self.request_new(product_name='')
        self.request_new(unit_of_measure='Furlong')
        self.request_new(proposed_cost='0')
        self.request_new(product_name='p01 rice')  # already on the master list
        self.assertFalse(SupplierProductRequest.objects.exists())

    def test_duplicate_pending_request_is_a_no_op(self):
        self.request_new()
        self.request_new()
        self.assertEqual(SupplierProductRequest.objects.count(), 1)

    def test_approving_creates_the_product_and_an_approved_offer(self):
        self.request_new()
        req = SupplierProductRequest.objects.get()
        cat = ProductCategory.objects.create(category_name='P01 Noodles')
        self.client.force_login(self.owner)
        self.client.post(
            reverse('supplier_request_review', args=[self.supplier.pk, req.pk]),
            {'decision': 'approve', 'unit_price': '12.00', 'category': cat.pk})
        req.refresh_from_db()
        self.assertEqual(req.status, SupplierProductRequest.Status.APPROVED)
        product = req.created_product
        self.assertEqual(product.product_name, 'P01 Brand New Noodles')
        self.assertEqual(product.unit_price, Decimal('12.00'))
        self.assertEqual(product.category, cat)
        self.assertTrue(product.sku_code)
        link = SupplierProduct.objects.get(supplier=self.supplier, product=product)
        self.assertEqual(link.status, SupplierProduct.Status.APPROVED)
        self.assertEqual(link.current_cost_price, Decimal('8.00'))

    def test_approval_needs_a_valid_retail_price(self):
        self.request_new()
        req = SupplierProductRequest.objects.get()
        self.client.force_login(self.owner)
        url = reverse('supplier_request_review', args=[self.supplier.pk, req.pk])
        for bad in ('', '0', '-1', 'x'):
            self.client.post(url, {'decision': 'approve', 'unit_price': bad})
        req.refresh_from_db()
        self.assertEqual(req.status, SupplierProductRequest.Status.PENDING)
        self.assertFalse(Product.objects.filter(product_name='P01 Brand New Noodles').exists())

    def test_rejecting_records_the_reason_and_creates_nothing(self):
        self.request_new()
        req = SupplierProductRequest.objects.get()
        self.client.force_login(self.owner)
        self.client.post(
            reverse('supplier_request_review', args=[self.supplier.pk, req.pk]),
            {'decision': 'reject', 'note': 'We do not stock noodles'})
        req.refresh_from_db()
        self.assertEqual(req.status, SupplierProductRequest.Status.REJECTED)
        self.assertEqual(req.review_note, 'We do not stock noodles')
        self.assertFalse(Product.objects.filter(product_name='P01 Brand New Noodles').exists())

    def test_a_decided_request_cannot_be_decided_again(self):
        self.request_new()
        req = SupplierProductRequest.objects.get()
        self.client.force_login(self.owner)
        url = reverse('supplier_request_review', args=[self.supplier.pk, req.pk])
        self.client.post(url, {'decision': 'approve', 'unit_price': '12'})
        self.client.post(url, {'decision': 'approve', 'unit_price': '13'})
        self.assertEqual(Product.objects.filter(product_name='P01 Brand New Noodles').count(), 1)

    def test_supplier_can_withdraw_only_their_own_pending_request(self):
        self.request_new()
        req = SupplierProductRequest.objects.get()
        self.client.force_login(self.other_supplier)
        self.assertEqual(self.client.post(
            reverse('supplier_product_request_withdraw', args=[req.pk])).status_code, 404)

        self.client.force_login(self.supplier)
        self.client.post(reverse('supplier_product_request_withdraw', args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.status, SupplierProductRequest.Status.WITHDRAWN)

    def test_only_the_owner_can_review_requests(self):
        self.request_new()
        req = SupplierProductRequest.objects.get()
        self.client.force_login(self.manager)
        self.client.post(
            reverse('supplier_request_review', args=[self.supplier.pk, req.pk]),
            {'decision': 'approve', 'unit_price': '12'})
        req.refresh_from_db()
        self.assertEqual(req.status, SupplierProductRequest.Status.PENDING)


class SupplierRemovesProductTests(OfferBase):
    """The supplier's own Remove: it used to be Add + Set Cost only."""

    def remove(self, link, user=None):
        self.client.force_login(user or self.supplier)
        return self.client.post(reverse('supplier_product_self_remove', args=[link.pk]))

    def test_supplier_can_remove_their_own_product(self):
        link = self.link()
        self.remove(link)
        self.assertFalse(SupplierProduct.objects.filter(pk=link.pk).exists())

    def test_supplier_can_withdraw_a_proposed_or_rejected_offer(self):
        for status in (SupplierProduct.Status.PROPOSED, SupplierProduct.Status.REJECTED):
            link = self.link(status=status, product=Product.objects.create(
                sku_code=f'P01-{status}', product_name=f'P01 {status}', unit_price=10))
            self.remove(link)
            self.assertFalse(SupplierProduct.objects.filter(pk=link.pk).exists())

    def test_another_suppliers_link_is_a_404_and_survives(self):
        link = self.link(supplier=self.other_supplier)
        self.assertEqual(self.remove(link).status_code, 404)
        self.assertTrue(SupplierProduct.objects.filter(pk=link.pk).exists())

    def test_non_suppliers_cannot_use_the_supplier_endpoint(self):
        link = self.link()
        self.remove(link, user=self.owner)
        self.assertTrue(SupplierProduct.objects.filter(pk=link.pk).exists())

    def test_get_is_not_allowed(self):
        link = self.link()
        self.client.force_login(self.supplier)
        response = self.client.get(reverse('supplier_product_self_remove', args=[link.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(SupplierProduct.objects.filter(pk=link.pk).exists())

    def test_blocked_while_an_open_order_needs_it(self):
        link = self.link()
        for status in (Procurement.Status.APPROVED, Procurement.Status.PENDING_OWNER_REVIEW,
                       Procurement.Status.TERMS_AGREED, Procurement.Status.INCOMING,
                       Procurement.Status.PARTIALLY_RECEIVED):
            po = self.po(self.rice, status)
            self.remove(link)
            self.assertTrue(
                SupplierProduct.objects.filter(pk=link.pk).exists(),
                f'removal should be blocked by a {status} PO')
            po.delete()

    def test_the_block_message_names_the_order(self):
        link = self.link()
        po = self.po(self.rice, Procurement.Status.INCOMING)
        self.client.force_login(self.supplier)
        response = self.client.post(
            reverse('supplier_product_self_remove', args=[link.pk]), follow=True)
        self.assertContains(response, 'Cannot remove')
        self.assertContains(response, po.po_number)

    def test_allowed_once_the_order_is_finished_or_only_a_draft(self):
        for status in (Procurement.Status.RECEIVED, Procurement.Status.CANCELLED,
                       Procurement.Status.DRAFT):
            link = self.link()
            po = self.po(self.rice, status)
            self.remove(link)
            self.assertFalse(SupplierProduct.objects.filter(pk=link.pk).exists(), status)
            po.delete()

    def test_another_suppliers_open_order_does_not_block(self):
        link = self.link()
        self.po(self.rice, Procurement.Status.INCOMING, supplier=self.other_supplier)
        self.remove(link)
        self.assertFalse(SupplierProduct.objects.filter(pk=link.pk).exists())

    def test_owner_side_removal_has_the_same_guard(self):
        link = self.link()
        self.po(self.rice, Procurement.Status.INCOMING)
        self.client.force_login(self.owner)
        self.client.post(reverse('supplier_product_remove', args=[self.supplier.pk, link.pk]))
        self.assertTrue(SupplierProduct.objects.filter(pk=link.pk).exists())

    def test_removed_product_leaves_the_po_catalog_and_can_be_offered_again(self):
        link = self.link()
        self.remove(link)
        self.client.force_login(self.owner)
        catalog = self.client.get(reverse('procurement_list')).context['supplier_catalog']
        self.assertNotIn(str(self.rice.pk), catalog.get(str(self.supplier.pk), {}))
        self.offer(self.rice)
        self.assertEqual(
            SupplierProduct.objects.get(supplier=self.supplier, product=self.rice).status,
            SupplierProduct.Status.PROPOSED)

    def test_portal_shows_status_and_a_remove_control(self):
        self.link()
        self.link(product=self.sugar, status=SupplierProduct.Status.PROPOSED)
        self.client.force_login(self.supplier)
        response = self.client.get(reverse('supplier_portal'))
        self.assertContains(response, 'Awaiting approval')
        self.assertContains(response, 'Remove')
        self.assertContains(response, 'Withdraw')
