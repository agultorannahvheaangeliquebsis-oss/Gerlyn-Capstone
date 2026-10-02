import io

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import (
    Batch, Inventory, OpeningStockImport, Product, StockMovement,
)


def opening_xlsx(rows):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append([
        'Barcode', 'Branch', 'Opening Qty', 'Lot/Batch',
        'Expiry Date', 'Received Date',
    ])
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile(
        'opening-stock.xlsx', buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


class OpeningStockImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='opening-owner', password='pw', role=User.Role.OWNER,
        )
        self.branch = Branch.objects.create(
            owner=self.owner, branch_name='Variety Store', location='Main',
        )
        self.product = Product.objects.create(
            sku_code='SKU-OPEN-001', barcode='480000000001',
            product_name='Opening Product', unit_of_measure='Piece', unit_price=10,
        )
        self.client.force_login(self.owner)

    def _upload(self, quantity=20):
        return self.client.post(reverse('opening_stock_import'), {
            'file': opening_xlsx([[
                self.product.barcode, self.branch.branch_name, quantity,
                'LOT-OPEN-1', '2099-01-31', '2026-01-01',
            ]]),
            'as_of_date': '2026-01-01',
        })

    def test_preview_writes_no_inventory_and_confirm_is_fully_audited(self):
        response = self._upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['preview_summary']['invalid'], 0)
        self.assertFalse(Inventory.objects.exists())
        self.assertFalse(Batch.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

        response = self.client.post(reverse('opening_stock_confirm'))
        self.assertEqual(response.status_code, 302)

        inventory = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inventory.quantity_on_hand, 20)
        batch = Batch.objects.get(opening_import_row__isnull=False)
        self.assertEqual(batch.quantity, 20)
        self.assertEqual(batch.external_lot_code, 'LOT-OPEN-1')
        movement = StockMovement.objects.get(
            movement_type=StockMovement.MovementType.OPENING_BALANCE,
        )
        self.assertEqual((movement.quantity_before, movement.quantity_after), (0, 20))
        self.assertIsNotNone(movement.opening_import_row_id)
        imported = OpeningStockImport.objects.get()
        self.assertEqual(imported.total_quantity, 20)
        self.assertEqual(
            self.client.get(reverse('opening_stock_import_detail', args=[imported.pk])).status_code,
            200,
        )
        self.assertEqual(self.client.get(reverse('opening_stock_import_history')).status_code, 200)

    def test_invalid_row_blocks_confirmation(self):
        response = self.client.post(reverse('opening_stock_import'), {
            'file': opening_xlsx([['NOT-A-PRODUCT', 'Missing Branch', 0, '', '', '']]),
            'as_of_date': '2026-01-01',
        })
        self.assertGreater(response.context['preview_summary']['invalid'], 0)
        response = self.client.post(reverse('opening_stock_confirm'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(OpeningStockImport.objects.exists())
        self.assertFalse(Inventory.objects.exists())

    def test_reversal_removes_only_the_imported_batch_quantity_remaining(self):
        self._upload()
        self.client.post(reverse('opening_stock_confirm'))
        imported = OpeningStockImport.objects.get()
        inventory = Inventory.objects.get(product=self.product, branch=self.branch)
        batch = Batch.objects.get(opening_import_row__import_batch=imported)

        # Simulate five units leaving through a normal audited path: rollback
        # must remove the remaining 15, not subtract the original 20 again.
        inventory.quantity_on_hand -= 5
        inventory.save()
        batch.quantity -= 5
        batch.save()

        response = self.client.post(
            reverse('opening_stock_import_reverse', args=[imported.pk]),
            {'reason': 'Count sheet was duplicated'},
        )
        self.assertEqual(response.status_code, 302)
        inventory.refresh_from_db()
        batch.refresh_from_db()
        imported.refresh_from_db()
        row = imported.rows.get()
        self.assertEqual(inventory.quantity_on_hand, 0)
        self.assertEqual(batch.quantity, 0)
        self.assertEqual(row.reversed_quantity, 15)
        self.assertEqual(row.consumed_quantity, 5)
        self.assertEqual(imported.status, OpeningStockImport.Status.REVERSED)
        self.assertTrue(StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.OPENING_BALANCE_REVERSAL,
            quantity_change=-15,
        ).exists())

    def test_branch_manager_cannot_use_opening_import(self):
        manager = User.objects.create_user(
            username='opening-manager', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        self.client.force_login(manager)
        response = self.client.get(reverse('opening_stock_import'))
        self.assertRedirects(response, reverse('inventory_list'), fetch_redirect_response=False)

    def test_same_normalized_file_cannot_be_committed_twice(self):
        self._upload()
        self.client.post(reverse('opening_stock_confirm'))
        response = self._upload()
        self.assertIsNotNone(response.context['preview_summary']['duplicate_import'])
        self.client.post(reverse('opening_stock_confirm'))
        self.assertEqual(OpeningStockImport.objects.count(), 1)
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.branch).quantity_on_hand,
            20,
        )

    def test_count_correction_increase_updates_total_batch_and_audit_movement(self):
        response = self.client.post(reverse('stock_adjustment_add'), {
            'product_id': self.product.pk,
            'branch_id': self.branch.pk,
            'counted_quantity': '3',
            'reason': 'Three units found during shelf count',
            'expiry_date': '2099-02-01',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.branch).quantity_on_hand,
            3,
        )
        self.assertTrue(Batch.objects.filter(quantity=3, expiry_date='2099-02-01').exists())
        movement = StockMovement.objects.get(
            movement_type=StockMovement.MovementType.ADJUSTMENT,
        )
        self.assertEqual(movement.quantity_before, 0)
        self.assertEqual(movement.quantity_after, 3)
        self.assertEqual(movement.quantity_change, 3)
        self.assertEqual(movement.created_by, self.owner)
        self.assertIn('Three units found during shelf count', movement.notes)

    def test_count_correction_decrease_consumes_batches_and_records_reason(self):
        inventory = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=10,
        )
        batch = Batch.objects.create(
            product=self.product, branch=self.branch,
            location=Batch.Location.FLOOR, quantity=10,
            received_date='2026-01-01',
        )

        response = self.client.post(reverse('stock_adjustment_add'), {
            'product_id': self.product.pk,
            'branch_id': self.branch.pk,
            'counted_quantity': '4',
            'reason': 'Six units missing during physical count',
        })

        self.assertEqual(response.status_code, 302)
        inventory.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(inventory.quantity_on_hand, 4)
        self.assertEqual(batch.quantity, 4)
        movement = StockMovement.objects.get(
            movement_type=StockMovement.MovementType.ADJUSTMENT,
        )
        self.assertEqual(movement.quantity_before, 10)
        self.assertEqual(movement.quantity_after, 4)
        self.assertEqual(movement.quantity_change, -6)
        self.assertIn('Six units missing during physical count', movement.notes)

    def test_count_correction_requires_a_reason(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=2,
        )
        response = self.client.post(reverse('stock_adjustment_add'), {
            'product_id': self.product.pk,
            'branch_id': self.branch.pk,
            'counted_quantity': '3',
            'reason': '   ',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter a reason for the correction.')
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.branch).quantity_on_hand,
            2,
        )
        self.assertFalse(StockMovement.objects.exists())

    def test_count_correction_rejects_malformed_expiry(self):
        response = self.client.post(reverse('stock_adjustment_add'), {
            'product_id': self.product.pk,
            'branch_id': self.branch.pk,
            'counted_quantity': '3',
            'reason': 'Physical shelf count',
            'expiry_date': 'not-a-date',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Inventory.objects.exists())
        self.assertContains(response, 'Enter a valid expiry date.')
