"""P0 — Enforce Positive Prices and Costs (inventory side).

Product.unit_price used to be checked for "not empty" on the web forms and
"not negative" on the spreadsheet import, both of which let a free (₱0) or
negative retail price through. This module covers every path that can set
it — the Add/Edit Product web forms, the DRF write serializer, and the
Excel import — plus the database CheckConstraint that now backstops all of
them, and the data migration that safely corrects any row that predates
this rule.
"""
import importlib
import io
import json
from decimal import Decimal

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import Product, ProductCategory


class ProductAddRetailPriceFormTests(TestCase):
    """product_add — inventory.views."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='price-owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)

    def _add(self, unit_price):
        return self.client.post(reverse('product_add'), {
            'product_name': 'Free Sample Product',
            'unit_of_measure': Product.UnitOfMeasure.PIECE,
            'unit_price': unit_price,
        })

    def _assert_refused(self, unit_price, fragment):
        response = self._add(unit_price)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            any(fragment in e for e in response.context['add_errors']),
            response.context['add_errors'],
        )
        self.assertFalse(
            Product.objects.filter(product_name='Free Sample Product').exists()
        )

    def test_zero_retail_price_is_refused(self):
        self._assert_refused('0', 'more than ₱0')

    def test_negative_retail_price_is_refused(self):
        self._assert_refused('-5.00', 'more than ₱0')

    def test_non_numeric_retail_price_is_refused(self):
        self._assert_refused('abc', 'valid number')

    def test_a_positive_retail_price_is_accepted(self):
        response = self._add('12.50')
        self.assertRedirects(response, reverse('product_list'))
        product = Product.objects.get(product_name='Free Sample Product')
        self.assertEqual(product.unit_price, Decimal('12.50'))


class ProductEditRetailPriceFormTests(TestCase):
    """product_edit — same rule, an existing product must not be re-saved
    with a free or negative retail price either."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='price-edit-owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)
        self.product = Product.objects.create(
            sku_code='SKU-PRICE-1', product_name='Corned Beef',
            unit_price=Decimal('65.00'),
        )

    def _edit(self, unit_price):
        return self.client.post(
            reverse('product_edit', args=[self.product.pk]), {
                'product_name': 'Corned Beef',
                'unit_of_measure': Product.UnitOfMeasure.CAN,
                'unit_price': unit_price,
            }
        )

    def test_zero_retail_price_is_refused_and_price_unchanged(self):
        response = self._edit('0')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            any('more than ₱0' in e for e in response.context['edit_errors'])
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.unit_price, Decimal('65.00'))

    def test_negative_retail_price_is_refused_and_price_unchanged(self):
        response = self._edit('-1.00')
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.unit_price, Decimal('65.00'))


class ProductWriteSerializerAPITests(TestCase):
    """The same-origin DRF endpoint (ProductWriteSerializer) is a second,
    independent door into Product.unit_price and must refuse a non-positive
    retailPrice on its own — a client that skips the web form entirely
    (or a future frontend) must not be able to bypass the form's rule."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='price-api-owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)
        self.category = ProductCategory.objects.create(category_name='Canned Goods')

    def _post(self, retail_price):
        return self.client.post(
            '/api/products/',
            data=json.dumps({
                'sku': 'SKU-API-1',
                'name': 'API Product',
                'categoryId': self.category.id,
                'unit': Product.UnitOfMeasure.PIECE,
                'retailPrice': retail_price,
            }),
            content_type='application/json',
        )

    def test_zero_retail_price_is_rejected(self):
        response = self._post('0.00')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('retailPrice', response.json())
        self.assertFalse(Product.objects.filter(sku_code='SKU-API-1').exists())

    def test_negative_retail_price_is_rejected(self):
        response = self._post('-10.00')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Product.objects.filter(sku_code='SKU-API-1').exists())

    def test_positive_retail_price_is_accepted(self):
        response = self._post('20.00')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            Product.objects.get(sku_code='SKU-API-1').unit_price, Decimal('20.00')
        )


def _xlsx_upload(filename, header, data_rows):
    """Build an in-memory .xlsx for POSTing through product_import_upload —
    mirrors inventory.tests._xlsx_upload."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for row in data_rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return SimpleUploadedFile(
        filename, buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


class ProductImportRetailPriceTests(TestCase):
    """The Excel import (inventory.views._parse_import_rows) is a third
    door — a spreadsheet with a zero or negative Retail Price column must
    flag that row as an error and skip it on commit, not silently create a
    free product."""

    HEADER = ['Item Name', 'SRP']
    MAPPING = {'map_product_name': 'Item Name', 'map_unit_price': 'SRP'}

    def setUp(self):
        self.owner = User.objects.create_user(
            username='price-import-owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)

    def _upload_and_map(self, price_cell):
        upload = self.client.post(
            reverse('product_import_upload'), {
                'file': _xlsx_upload(
                    'products.xlsx', self.HEADER,
                    [['Imported Widget', price_cell]],
                )
            },
        )
        self.assertRedirects(
            upload, f"{reverse('product_list')}?import=map",
            fetch_redirect_response=False,
        )
        self.client.post(reverse('product_import_map'), self.MAPPING)

    def test_zero_retail_price_row_is_flagged_and_not_committed(self):
        self._upload_and_map(0)
        preview = self.client.get(reverse('product_import_preview'), follow=True)
        self.assertEqual(preview.context['import_counts']['error'], 1)
        self.client.post(reverse('product_import_preview'))
        self.assertFalse(
            Product.objects.filter(product_name='Imported Widget').exists()
        )

    def test_negative_retail_price_row_is_flagged_and_not_committed(self):
        self._upload_and_map(-5)
        preview = self.client.get(reverse('product_import_preview'), follow=True)
        self.assertEqual(preview.context['import_counts']['error'], 1)
        self.client.post(reverse('product_import_preview'))
        self.assertFalse(
            Product.objects.filter(product_name='Imported Widget').exists()
        )

    def test_positive_retail_price_row_is_committed(self):
        self._upload_and_map(19.99)
        self.client.post(reverse('product_import_preview'))
        product = Product.objects.get(product_name='Imported Widget')
        self.assertEqual(product.unit_price, Decimal('19.99'))


class ProductUnitPricePositiveConstraintTests(TestCase):
    """The database-level backstop — CheckConstraint
    product_unit_price_positive — for any write path that reaches the ORM
    without going through a form or serializer at all (bulk scripts, the
    Django admin, a future migration)."""

    def test_zero_unit_price_is_rejected_by_the_database(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Product.objects.create(
                    sku_code='SKU-CONSTRAINT-1', product_name='Bad Product',
                    unit_price=Decimal('0'),
                )

    def test_negative_unit_price_is_rejected_by_the_database(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Product.objects.create(
                    sku_code='SKU-CONSTRAINT-2', product_name='Bad Product',
                    unit_price=Decimal('-1.00'),
                )

    def test_a_positive_unit_price_is_accepted(self):
        product = Product.objects.create(
            sku_code='SKU-CONSTRAINT-3', product_name='Good Product',
            unit_price=Decimal('1.00'),
        )
        self.assertEqual(product.unit_price, Decimal('1.00'))


class InvalidHistoricalPriceMigrationTests(TransactionTestCase):
    """0027_enforce_positive_prices's data-fix step
    (fix_non_positive_unit_prices) must safely correct any Product row that
    predates this rule — written before the CheckConstraint existed, e.g.
    by a script or an older code path — rather than the migration crashing
    a deploy the moment MySQL validates existing rows against the new
    constraint.

    The CheckConstraint is now live on this very test database, so a
    negative unit_price can no longer be written through the ORM at all —
    exactly the protection this ticket asked for. To reproduce the
    "already-invalid row from before the constraint existed" scenario the
    migration actually has to cope with, the constraint is dropped for the
    duration of the test (mirroring the database's own state one migration
    earlier) and restored afterwards.
    """

    def _drop_constraint(self):
        constraint = next(
            c for c in Product._meta.constraints
            if c.name == 'product_unit_price_positive'
        )
        with connection.schema_editor() as editor:
            editor.remove_constraint(Product, constraint)
        return constraint

    def _restore_constraint(self, constraint):
        with connection.schema_editor() as editor:
            editor.add_constraint(Product, constraint)

    def test_non_positive_rows_are_corrected_to_the_minimum_valid_price(self):
        migration = importlib.import_module(
            'inventory.migrations.0027_enforce_positive_prices'
        )

        from django.apps import apps as real_apps

        constraint = self._drop_constraint()
        try:
            product = Product.objects.create(
                sku_code='SKU-MIGRATE-1', product_name='Legacy Product',
                unit_price=Decimal('1.00'),
            )
            # Reproduces how a pre-existing bad row would have gotten there
            # (a raw UPDATE, or data written before this rule existed) —
            # full_clean() is never called by .update().
            Product.objects.filter(pk=product.pk).update(unit_price=Decimal('-5.00'))

            # The fix must run — and succeed — before the constraint can be
            # restored, exactly the order the real migration applies them in.
            migration.fix_non_positive_unit_prices(real_apps, None)
        finally:
            self._restore_constraint(constraint)

        product.refresh_from_db()
        self.assertEqual(product.unit_price, Decimal('0.01'))

    def test_valid_rows_are_left_untouched(self):
        migration = importlib.import_module(
            'inventory.migrations.0027_enforce_positive_prices'
        )
        product = Product.objects.create(
            sku_code='SKU-MIGRATE-2', product_name='Fine Product',
            unit_price=Decimal('42.00'),
        )

        from django.apps import apps as real_apps

        migration.fix_non_positive_unit_prices(real_apps, None)

        product.refresh_from_db()
        self.assertEqual(product.unit_price, Decimal('42.00'))
