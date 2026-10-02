import csv
import io
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from accounts.models import BranchManagerProfile, User
from branches.models import Branch
from inventory.models import Inventory, Product, StockMovement

from .models import PosSalesImport, SalesTransaction


HEADERS = [
    'Invoice/OR Number', 'Transaction Date/Time', 'Terminal ID', 'Cashier ID',
    'Product SKU', 'Barcode', 'Description', 'Quantity', 'Unit Price',
    'Gross Sales', 'Net Sales', 'VATable Amount', 'VAT-Exempt Amount',
    'Zero-Rated Amount', 'Senior Discount', 'PWD Discount',
    'Wholesale Discount', 'Promo Discount', 'Payment Method',
]


class PosSalesImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner', password='pw', role=User.Role.OWNER)
        self.branch = Branch.objects.create(branch_name='Main', location='Baliuag', owner=self.owner)
        self.product = Product.objects.create(
            sku_code='SKU-001', barcode='480000000001', product_name='Rice', unit_price=100,
        )
        self.inventory = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=20, reorder_level=2,
        )
        self.client.force_login(self.owner)

    def row(self, invoice='OR-1001', sku='SKU-001', quantity='2', payment='Cash',
            net='190.00', gross='200.00', promo='10.00'):
        return [
            invoice, '2026-09-28 14:30:00', 'TERM-1', 'CASHIER-7', sku,
            '480000000001' if sku == 'SKU-001' else '', 'Rice', quantity, '100.00',
            gross, net, '190.00', '0.00', '0.00', '0.00', '0.00', '0.00', promo, payment,
        ]

    def csv_upload(self, rows, name='zank-daily.csv'):
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(HEADERS)
        writer.writerows(rows)
        return SimpleUploadedFile(name, stream.getvalue().encode('utf-8'), content_type='text/csv')

    def upload(self, rows):
        return self.client.post(reverse('pos_sales_import'), {
            'branch': self.branch.pk, 'pos_file': self.csv_upload(rows),
        })

    def confirm(self):
        return self.client.post(reverse('pos_sales_import_confirm'))

    def test_valid_csv_import_creates_completed_sale_billing_audit_and_stock_movement(self):
        preview = self.upload([self.row(payment='Maya')])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.context['preview']['summary']['valid_transactions'], 1)

        response = self.confirm()

        self.assertRedirects(response, reverse('pos_sales_import_history'))
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.source, 'ZANK POS')
        self.assertEqual(sale.pos_invoice_number, 'OR-1001')
        self.assertEqual(sale.pos_terminal_id, 'TERM-1')
        self.assertEqual(sale.pos_cashier_id, 'CASHIER-7')
        self.assertEqual(sale.order_status, SalesTransaction.OrderStatus.COMPLETED)
        self.assertEqual(sale.mode_of_payment, 'Maya')
        self.assertEqual(sale.total_amount, Decimal('190.00'))
        self.assertEqual(sale.billing_record.status, 'Paid')
        self.assertEqual(sale.billing_record.payment_method, 'Maya')
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 18)
        self.assertTrue(StockMovement.objects.filter(reference_id=sale.pk, quantity_change=-2).exists())
        history = PosSalesImport.objects.get()
        self.assertEqual(history.successful_transactions, 1)
        self.assertEqual(history.net_total, Decimal('190.00'))

    def test_xlsx_is_supported(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(HEADERS)
        sheet.append(self.row(invoice='OR-XLSX'))
        content = io.BytesIO()
        workbook.save(content)
        upload = SimpleUploadedFile(
            'daily.xlsx', content.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        response = self.client.post(reverse('pos_sales_import'), {
            'branch': self.branch.pk, 'pos_file': upload,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['preview']['summary']['valid_transactions'], 1)

    def test_duplicate_invoice_for_same_branch_is_previewed_and_skipped(self):
        self.upload([self.row()])
        self.confirm()
        self.upload([self.row(), self.row(invoice='OR-1002')])
        preview = self.client.session['pos_sales_import']
        response = self.client.get(reverse('pos_sales_import'))
        self.assertIn('rows', preview)
        # Re-post because GET intentionally starts a fresh upload screen.
        response = self.upload([self.row(), self.row(invoice='OR-1002')])
        self.assertEqual(response.context['preview']['summary']['duplicates'], 1)
        self.assertEqual(response.context['preview']['summary']['valid_transactions'], 1)
        self.confirm()
        self.assertEqual(SalesTransaction.objects.count(), 2)
        latest = PosSalesImport.objects.first()
        self.assertEqual(latest.skipped_duplicates, 1)

    def test_invalid_file_type_has_clear_error(self):
        response = self.client.post(reverse('pos_sales_import'), {
            'branch': self.branch.pk,
            'pos_file': SimpleUploadedFile('daily.txt', b'not a supported file'),
        })
        self.assertContains(response, 'Choose a CSV or Excel (.xlsx) file.')
        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_missing_required_headers_has_clear_error(self):
        upload = SimpleUploadedFile('daily.csv', b'Invoice,SKU\nOR-1,SKU-001\n')
        response = self.client.post(reverse('pos_sales_import'), {
            'branch': self.branch.pk, 'pos_file': upload,
        })
        self.assertContains(response, 'Missing required column(s)')
        self.assertContains(response, 'banner banner-error')
        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_order_console_hosts_closed_upload_modal(self):
        response = self.client.get(reverse('transaction_list'))
        self.assertTemplateUsed(response, 'sales/_pos_import_modal.html')
        self.assertContains(response, 'id="open-pos-import-modal"')
        self.assertContains(response, 'id="pos-import-modal" data-autoopen="0"')

    def test_import_history_hosts_upload_modal(self):
        response = self.client.get(reverse('pos_sales_import_history'))
        self.assertTemplateUsed(response, 'sales/_pos_import_modal.html')
        self.assertContains(response, 'id="open-pos-import-modal"')

    def test_get_import_url_opens_modal_over_order_console(self):
        response = self.client.get(reverse('pos_sales_import'))
        self.assertTemplateUsed(response, 'sales/transaction_list.html')
        self.assertContains(response, 'id="pos-import-modal" data-autoopen="1"')

    def test_failed_upload_reopens_modal_with_error_and_branch_kept(self):
        response = self.client.post(reverse('pos_sales_import'), {
            'branch': self.branch.pk,
            'pos_file': SimpleUploadedFile('daily.txt', b'not a supported file'),
        })
        self.assertTemplateUsed(response, 'sales/transaction_list.html')
        self.assertContains(response, 'id="pos-import-modal" data-autoopen="1"')
        self.assertContains(response, f'<option value="{self.branch.pk}" selected>')

    def test_successful_upload_renders_review_page_not_modal(self):
        response = self.upload([self.row()])
        self.assertTemplateUsed(response, 'sales/pos_import.html')
        self.assertTemplateNotUsed(response, 'sales/_pos_import_modal.html')

    def test_manager_modal_is_pinned_to_their_own_branch(self):
        other = Branch.objects.create(branch_name='Annex', location='Pulilan', owner=self.owner)
        manager = User.objects.create_user(
            username='manager', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        BranchManagerProfile.objects.update_or_create(
            user=manager, defaults={'manager_name': 'manager', 'branch': self.branch},
        )
        self.client.force_login(manager)

        response = self.client.get(reverse('transaction_list'))
        self.assertContains(response, f'<option value="{self.branch.pk}" selected>')
        self.assertNotContains(response, f'<option value="{other.pk}"')

    def test_unassigned_manager_gets_no_import_button_or_modal(self):
        stranded = User.objects.create_user(
            username='stranded', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        self.client.force_login(stranded)

        response = self.client.get(reverse('transaction_list'))
        self.assertTemplateNotUsed(response, 'sales/_pos_import_modal.html')
        self.assertNotContains(response, 'id="open-pos-import-modal"')

    def test_customer_cannot_access_import(self):
        customer = User.objects.create_user(
            username='customer', password='pw', role=User.Role.CUSTOMER,
        )
        self.client.force_login(customer)
        response = self.client.get(reverse('pos_sales_import'))
        self.assertRedirects(response, reverse('transaction_list'), fetch_redirect_response=False)

    def test_unmatched_sku_is_flagged_and_not_created(self):
        response = self.upload([self.row(sku='MISSING')])
        self.assertEqual(response.context['preview']['summary']['invalid_rows'], 1)
        self.assertContains(response, 'No Product matches SKU')
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_malformed_row_is_skipped_but_recorded_in_history(self):
        response = self.upload([
            self.row(invoice='OR-BAD', quantity='two'),
            self.row(invoice='OR-GOOD'),
        ])
        self.assertEqual(response.context['preview']['summary']['invalid_rows'], 1)
        self.confirm()
        self.assertEqual(SalesTransaction.objects.count(), 1)
        history = PosSalesImport.objects.get()
        self.assertEqual(history.failed_rows, 1)
        self.assertEqual(history.validation_errors[0]['invoice'], 'OR-BAD')

    def test_commit_failure_rolls_back_sales_inventory_billing_and_history(self):
        self.upload([self.row(invoice='OR-ONE'), self.row(invoice='OR-TWO')])
        from sales.views import _deduct_stock as real_deduct
        calls = {'count': 0}

        def fail_second(transaction, actor):
            calls['count'] += 1
            if calls['count'] == 2:
                raise RuntimeError('simulated inventory failure')
            return real_deduct(transaction, actor)

        with mock.patch('sales.views._deduct_stock', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.confirm()

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertEqual(PosSalesImport.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 20)

    def test_owner_can_undo_import_and_restore_inventory_atomically(self):
        self.upload([self.row(invoice='OR-UNDO')])
        self.confirm()
        import_batch = PosSalesImport.objects.get()
        sale = SalesTransaction.objects.get()

        response = self.client.post(
            reverse('pos_sales_import_undo', args=[import_batch.pk]),
            {'reason': 'Wrong daily export selected'},
        )

        self.assertRedirects(response, reverse('pos_sales_import_history'))
        import_batch.refresh_from_db()
        sale.refresh_from_db()
        self.inventory.refresh_from_db()
        self.assertIsNotNone(import_batch.reversed_at)
        self.assertEqual(import_batch.reversed_by, self.owner)
        self.assertEqual(import_batch.reversal_reason, 'Wrong daily export selected')
        self.assertEqual(sale.order_status, SalesTransaction.OrderStatus.CANCELLED)
        self.assertIn('Wrong daily export selected', sale.cancel_reason)
        self.assertEqual(sale.billing_record.status, 'Cancelled')
        self.assertEqual(self.inventory.quantity_on_hand, 20)
        self.assertTrue(StockMovement.objects.filter(
            reference_id=sale.pk, quantity_change=2,
        ).exists())

    def test_import_cannot_be_undone_twice(self):
        self.upload([self.row(invoice='OR-ONCE')])
        self.confirm()
        import_batch = PosSalesImport.objects.get()
        url = reverse('pos_sales_import_undo', args=[import_batch.pk])
        self.client.post(url, {'reason': 'Wrong upload'})

        self.client.post(url, {'reason': 'Try again'})

        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 20)
        self.assertEqual(StockMovement.objects.filter(quantity_change=2).count(), 1)

    def test_undo_failure_rolls_back_all_reversals(self):
        self.upload([self.row(invoice='OR-U1'), self.row(invoice='OR-U2')])
        self.confirm()
        import_batch = PosSalesImport.objects.get()
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 16)
        from sales.views import _reverse_stock as real_reverse
        calls = {'count': 0}

        def fail_second(transaction, actor):
            calls['count'] += 1
            if calls['count'] == 2:
                raise RuntimeError('simulated reversal failure')
            return real_reverse(transaction, actor)

        with mock.patch('sales.views._reverse_stock', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('pos_sales_import_undo', args=[import_batch.pk]),
                    {'reason': 'Wrong upload'},
                )

        import_batch.refresh_from_db()
        self.inventory.refresh_from_db()
        self.assertIsNone(import_batch.reversed_at)
        self.assertEqual(self.inventory.quantity_on_hand, 16)
        self.assertFalse(SalesTransaction.objects.exclude(
            order_status=SalesTransaction.OrderStatus.COMPLETED,
        ).exists())
        self.assertEqual(StockMovement.objects.filter(quantity_change__gt=0).count(), 0)
