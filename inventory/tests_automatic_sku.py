"""Automatic CAT-NNNN SKUs.

New products get a category-prefixed SKU assigned server-side. Legacy SKUs
(SKU-001, DEMO-003, ...) are never touched, and the SKU is read-only on every
edit surface. Covers the model helper, the Add Product form, the bulk import,
supplier-request approval, the category prefix field, and the DRF endpoint.
"""
import json
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from core.module_permissions import ADD, EDIT, PRODUCTS, default_module_actions
from inventory import tests as base_tests
from inventory.models import Product, ProductCategory, suggest_sku_prefix


def _make_product(sku, category=None, name=None, **extra):
    return Product.objects.create(
        sku_code=sku, product_name=name or f'Item {sku}', category=category,
        unit_price=Decimal('10.00'), **extra,
    )


class SkuPrefixGenerationTests(TestCase):
    def test_prefix_is_first_three_letters_uppercased(self):
        self.assertEqual(suggest_sku_prefix('Canned Goods'), 'CAN')
        self.assertEqual(suggest_sku_prefix('  rice & grains'), 'RIC')

    def test_non_letters_are_skipped(self):
        self.assertEqual(suggest_sku_prefix('7-Up Drinks'), 'UPD')
        self.assertEqual(suggest_sku_prefix('Ox'), 'OX')

    def test_name_without_letters_falls_back_to_gen(self):
        self.assertEqual(suggest_sku_prefix('123 !!'), 'GEN')

    def test_first_sku_and_sequence_for_a_category(self):
        cat = ProductCategory.objects.create(category_name='Canned Goods')
        self.assertEqual(Product.next_sku_code(cat), 'CAN-0001')
        _make_product('CAN-0001', cat)
        _make_product('CAN-0007', cat)
        self.assertEqual(Product.next_sku_code(cat), 'CAN-0008')

    def test_custom_prefix_overrides_the_name_derived_one(self):
        cat = ProductCategory.objects.create(category_name='Canned Goods', sku_prefix='TIN')
        self.assertEqual(Product.next_sku_code(cat), 'TIN-0001')

    def test_uncategorised_uses_gen(self):
        self.assertEqual(Product.next_sku_code(None), 'GEN-0001')
        self.assertEqual(Product.next_sku_code(), 'GEN-0001')
        _make_product('GEN-0004')
        self.assertEqual(Product.next_sku_code(None), 'GEN-0005')

    def test_two_categories_sharing_a_prefix_share_one_sequence(self):
        a = ProductCategory.objects.create(category_name='Canned Goods')
        b = ProductCategory.objects.create(category_name='Candy', sku_prefix='CAN')
        _make_product('CAN-0001', a)
        self.assertEqual(Product.next_sku_code(b), 'CAN-0002')

    def test_prefix_validator_rejects_lowercase_digits_and_length(self):
        field = ProductCategory._meta.get_field('sku_prefix')
        for bad in ('can', 'C4N', 'C N', 'ABCDEF'):
            with self.assertRaises(ValidationError, msg=bad):
                field.clean(bad, None)
        self.assertEqual(field.clean('CAN', None), 'CAN')
        self.assertEqual(field.clean('', None), '')  # blank = derive from the name


class SkuCollisionRetryTests(TestCase):
    def test_retries_when_the_first_candidate_was_taken_concurrently(self):
        cat = ProductCategory.objects.create(category_name='Canned Goods')
        # First call returns a code another request has just taken; the
        # retry recomputes and gets the next free one.
        _make_product('CAN-0001', cat)
        real = Product.next_sku_code.__func__
        calls = iter(['CAN-0001'])

        def racing(klass, category=None):
            return next(calls, None) or real(klass, category)

        with mock.patch.object(Product, 'next_sku_code', classmethod(racing)):
            product = Product.create_with_sku(
                category=cat, product_name='Sardines', unit_price=Decimal('25.00'),
            )
        self.assertEqual(product.sku_code, 'CAN-0002')

    def test_unrelated_integrity_errors_are_not_swallowed(self):
        _make_product('GEN-0001', barcode='DUPE')
        with self.assertRaises(IntegrityError):
            Product.create_with_sku(
                product_name='Clash', unit_price=Decimal('5.00'), barcode='DUPE',
            )
        self.assertEqual(Product.objects.count(), 1)


class LegacySkuTests(TestCase):
    def test_legacy_skus_are_left_alone_and_do_not_disturb_new_ones(self):
        legacy = _make_product('SKU-001')
        other = _make_product('DEMO-003')
        cat = ProductCategory.objects.create(category_name='Canned Goods')
        created = Product.create_with_sku(
            category=cat, product_name='New', unit_price=Decimal('9.00'),
        )
        self.assertEqual(created.sku_code, 'CAN-0001')
        legacy.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((legacy.sku_code, other.sku_code), ('SKU-001', 'DEMO-003'))

    def test_saving_or_moving_an_existing_product_never_reassigns_its_sku(self):
        cat = ProductCategory.objects.create(category_name='Canned Goods')
        p = _make_product('SKU-042')
        p.category = cat
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.sku_code, 'SKU-042')


class ProductFormSkuTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='sku-owner', password='pw', role=User.Role.OWNER)
        self.client.force_login(self.owner)
        self.cat = ProductCategory.objects.create(category_name='Canned Goods')

    def _add(self, **extra):
        data = {
            'product_name': 'Sardines', 'unit_of_measure': 'Can',
            'unit_price': '25.00', 'category': self.cat.pk,
        }
        data.update(extra)
        return self.client.post(reverse('product_add'), data)

    def test_add_assigns_a_category_sku(self):
        self._add()
        self._add(product_name='Tuna')
        skus = sorted(Product.objects.values_list('sku_code', flat=True))
        self.assertEqual(skus, ['CAN-0001', 'CAN-0002'])

    def test_add_ignores_a_posted_sku(self):
        self._add(sku_code='HACK-1', sku='HACK-2')
        self.assertEqual(Product.objects.get().sku_code, 'CAN-0001')

    def test_add_without_a_category_uses_gen(self):
        self._add(category='')
        self.assertEqual(Product.objects.get().sku_code, 'GEN-0001')

    def test_add_page_shows_hint_and_per_category_prefix(self):
        ProductCategory.objects.create(category_name='Drinks', sku_prefix='BEV')
        response = self.client.get(reverse('product_add'))
        self.assertContains(response, 'Assigned on save (e.g. GEN-####)')
        self.assertContains(response, 'data-sku-prefix="CAN"')
        self.assertContains(response, 'data-sku-prefix="BEV"')



    def test_edit_keeps_the_sku_locked(self):
        p = _make_product('SKU-007', self.cat, name='Old')
        response = self.client.post(reverse('product_edit', args=[p.pk]), {
            'product_name': 'New', 'unit_of_measure': 'Can',
            'unit_price': '30.00', 'category': self.cat.pk,
            'sku_code': 'HACK-9',
        })
        self.assertEqual(response.status_code, 302)
        p.refresh_from_db()
        self.assertEqual((p.sku_code, p.product_name), ('SKU-007', 'New'))
        page = self.client.get(reverse('product_edit', args=[p.pk]))
        self.assertContains(page, 'Locked')


class CategoryPrefixFormTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='cat-owner', password='pw', role=User.Role.OWNER)
        self.client.force_login(self.owner)

    def test_owner_sets_prefix_and_typed_lowercase_is_uppercased(self):
        self.client.post(reverse('category_add'), {
            'category_name': 'Canned Goods', 'sku_prefix': 'tin'})
        self.assertEqual(ProductCategory.objects.get().sku_prefix, 'TIN')

    def test_blank_prefix_is_allowed(self):
        self.client.post(reverse('category_add'), {'category_name': 'Snacks'})
        self.assertEqual(ProductCategory.objects.get().sku_prefix, '')

    def test_invalid_prefix_is_refused_and_modal_reopens(self):
        response = self.client.post(reverse('category_add'), {
            'category_name': 'Snacks', 'sku_prefix': 'S4'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SKU prefix must be 1-5 uppercase letters')
        self.assertFalse(ProductCategory.objects.exists())

    def test_edit_updates_and_clears_prefix(self):
        cat = ProductCategory.objects.create(category_name='Snacks', sku_prefix='SNK')
        self.client.post(reverse('category_edit', args=[cat.pk]), {
            'category_name': 'Snacks', 'sku_prefix': 'CHP'})
        cat.refresh_from_db()
        self.assertEqual(cat.sku_prefix, 'CHP')
        self.client.post(reverse('category_edit', args=[cat.pk]), {
            'category_name': 'Snacks', 'sku_prefix': ''})
        cat.refresh_from_db()
        self.assertEqual(cat.sku_prefix, '')

    def _manager(self, username, extra_actions=()):
        manager = User.objects.create_user(
            username=username, password='pw', role=User.Role.BRANCH_MANAGER)
        manager.allowed_modules = [PRODUCTS]
        actions = default_module_actions()
        actions[PRODUCTS] = list(set(actions[PRODUCTS]) | set(extra_actions))
        manager.module_actions = actions
        manager.save(update_fields=['allowed_modules', 'module_actions'])
        return manager

    def test_default_manager_cannot_set_a_prefix(self):
        self.client.force_login(self._manager('cat-mgr'))
        self.client.post(reverse('category_add'), {
            'category_name': 'Snacks', 'sku_prefix': 'SNK'})
        self.assertFalse(ProductCategory.objects.exists())

    def test_granted_manager_can_set_a_prefix(self):
        self.client.force_login(self._manager('cat-mgr2', [ADD, EDIT]))
        self.client.post(reverse('category_add'), {
            'category_name': 'Snacks', 'sku_prefix': 'SNK'})
        self.assertEqual(ProductCategory.objects.get().sku_prefix, 'SNK')


class BulkImportSkuTests(TestCase):
    HEADER = base_tests.ProductImportExportTests.HEADER
    MAPPING = base_tests.ProductImportExportTests.MAPPING

    def test_import_assigns_category_skus_and_leaves_updated_rows_alone(self):
        owner = User.objects.create_user(
            username='imp-owner', password='pw', role=User.Role.OWNER)
        self.client.force_login(owner)
        cat = ProductCategory.objects.create(category_name='Snacks', sku_prefix='SNK')
        _make_product('SKU-EXISTING', cat, name='Old', barcode='EXBAR')
        rows = [
            ['Chips', 'B1', 'Snacks', 'Piece', '10', '', '', '', '', ''],
            ['Crackers', 'B2', 'Snacks', 'Piece', '12', '', '', '', '', ''],
            ['Loose Item', 'B3', '', 'Piece', '5', '', '', '', '', ''],
            ['Renamed', 'EXBAR', 'Snacks', 'Piece', '11', '', '', '', '', ''],
            ['Brand New Cat', 'B4', 'Frozen Foods', 'Piece', '99', '', '', '', '', ''],
        ]
        self.client.post(reverse('product_import_upload'), {
            'file': base_tests._xlsx_upload('i.xlsx', self.HEADER, rows)})
        self.client.post(reverse('product_import_map'), self.MAPPING)
        self.client.get(reverse('product_import_preview'), follow=True)
        self.client.post(reverse('product_import_preview'))

        by_barcode = dict(Product.objects.values_list('barcode', 'sku_code'))
        self.assertEqual(by_barcode['B1'], 'SNK-0001')
        self.assertEqual(by_barcode['B2'], 'SNK-0002')
        self.assertEqual(by_barcode['B3'], 'GEN-0001')
        self.assertEqual(by_barcode['B4'], 'FRO-0001')
        self.assertEqual(by_barcode['EXBAR'], 'SKU-EXISTING')


class SupplierRequestApprovalSkuTests(TestCase):
    def test_approval_mints_a_category_sku(self):
        from procurement.models import SupplierProductRequest
        from procurement.services import review_product_request
        owner = User.objects.create_user(
            username='rq-owner', password='pw', role=User.Role.OWNER)
        supplier = User.objects.create_user(
            username='rq-sup', password='pw', role=User.Role.SUPPLIER)
        cat = ProductCategory.objects.create(category_name='Beverages')
        req = SupplierProductRequest.objects.create(
            supplier=supplier, product_name='Cola', proposed_cost=Decimal('8.00'))
        req = review_product_request(
            req, owner, approve=True, unit_price=Decimal('15.00'), category=cat)
        self.assertEqual(req.created_product.sku_code, 'BEV-0001')


class ProductApiSkuTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='api-sku-owner', password='pw', role=User.Role.OWNER)
        self.client.force_login(self.owner)
        self.cat = ProductCategory.objects.create(category_name='Canned Goods')

    def _post(self, **extra):
        payload = {
            'name': 'API Product', 'categoryId': self.cat.id,
            'unit': 'Piece', 'retailPrice': '20.00',
        }
        payload.update(extra)
        return self.client.post(
            '/api/products/', data=json.dumps(payload),
            content_type='application/json')

    def test_create_generates_the_sku(self):
        response = self._post()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Product.objects.get().sku_code, 'CAN-0001')

    def test_client_supplied_sku_is_rejected_on_create(self):
        response = self._post(sku='MINE-1')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('sku', response.json())
        self.assertFalse(Product.objects.exists())

    def test_blank_or_null_sku_is_also_rejected_on_create(self):
        for value in ('', None):
            response = self._post(sku=value)
            self.assertEqual(response.status_code, 400, response.content)
            self.assertIn('sku', response.json())
        self.assertFalse(Product.objects.exists())

    def test_client_supplied_sku_is_rejected_on_put_and_patch(self):
        p = _make_product('SKU-LEGACY', self.cat, name='Legacy')
        body = {
            'name': 'Renamed', 'categoryId': self.cat.id,
            'unit': 'Piece', 'retailPrice': '10.00',
        }
        for method in (self.client.put, self.client.patch):
            for sku in ('HACK-1', 'SKU-LEGACY'):  # even echoing the current SKU
                response = method(
                    f'/api/products/{p.pk}/',
                    data=json.dumps(dict(body, sku=sku)),
                    content_type='application/json')
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn('sku', response.json())
        p.refresh_from_db()
        self.assertEqual((p.sku_code, p.product_name), ('SKU-LEGACY', 'Legacy'))

    def test_put_and_patch_without_sku_still_work_and_keep_the_sku(self):
        p = _make_product('SKU-LEGACY', self.cat, name='Legacy')
        body = {
            'name': 'Renamed', 'categoryId': self.cat.id,
            'unit': 'Piece', 'retailPrice': '10.00',
        }
        response = self.client.put(
            f'/api/products/{p.pk}/', data=json.dumps(body),
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        response = self.client.patch(
            f'/api/products/{p.pk}/', data=json.dumps({'name': 'Again'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        p.refresh_from_db()
        self.assertEqual((p.sku_code, p.product_name), ('SKU-LEGACY', 'Again'))

    def test_serializers_mark_sku_read_only(self):
        from inventory.serializers import (
            ProductSerializer, ProductWriteSerializer,
        )
        self.assertTrue(ProductSerializer().fields['sku'].read_only)
        self.assertTrue(ProductWriteSerializer().fields['sku'].read_only)
