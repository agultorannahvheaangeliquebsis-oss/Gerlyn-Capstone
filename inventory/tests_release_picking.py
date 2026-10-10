"""Move Stock to Floor: scan-to-verify picking.

Regression for "Martys Spicy Vinegar 26g — null of 15 picked".

Root cause: USE_THOUSAND_SEPARATOR rendered a line's pk 4754 as "4,754" in
the release screen's data-item-id and in the hidden item_id the form posts.
The scan endpoint answers with the integer 4754, so the screen could not find
its own row (-> "null of 15 picked", count never moved), and the release POST
keyed on "4,754" matched no line, so the typed count was ignored and the FULL
approved quantity was released.

Every fixture below gives the lines pks >= 1000 on purpose. The older release
tests use tiny pks and post keys straight through the test client, so they
could never see a rendering bug like this.
"""
import re
from datetime import date

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import (
    Batch, Inventory, Product, StockMovement, StockTransfer, StockTransferItem,
    WarehouseStock,
)

# Four digits, so the thousands separator applies.
ITEM_PK_A = 4754
ITEM_PK_B = 4755


class _ReleaseFixture:
    def build(self, approved_a=15, approved_b=8):
        self.owner = User.objects.create_user(
            username='rp-owner', password='pw', role=User.Role.OWNER,
        )
        self.branch = Branch.objects.create(
            owner=self.owner, branch_name='Picking Mart', location='P',
        )
        self.vinegar = self._product('VIN', 'Martys Spicy Vinegar 26g', '4800194152891', 40)
        self.tuna = self._product('TUNA', 'Century Tuna Flakes in Oil 155g', None, 40)

        self.slip = StockTransfer.objects.create(
            warehouse=self.branch.warehouse, source_branch=self.branch,
            destination_branch=self.branch,
            purpose=StockTransfer.Purpose.BRANCH_RESTOCK,
            requested_by=self.owner, approved_by=self.owner,
            status=StockTransfer.Status.APPROVED,
        )
        self.item_a = StockTransferItem.objects.create(
            pk=ITEM_PK_A, transfer=self.slip, product=self.vinegar,
            quantity_requested=approved_a, quantity_approved=approved_a,
        )
        self.item_b = StockTransferItem.objects.create(
            pk=ITEM_PK_B, transfer=self.slip, product=self.tuna,
            quantity_requested=approved_b, quantity_approved=approved_b,
        )
        self.client.force_login(self.owner)

    def _product(self, sku, name, barcode, qty):
        product = Product.objects.create(
            sku_code=sku, product_name=name, unit_price='30.00', barcode=barcode,
        )
        WarehouseStock.objects.create(
            warehouse=self.branch.warehouse, product=product, quantity_on_hand=qty,
        )
        Batch.objects.create(
            product=product, branch=self.branch, location=Batch.Location.WAREHOUSE,
            quantity=qty, received_date=date.today(), expiry_date=date(2030, 1, 1),
        )
        return product

    # --------------------------------------------------------------- helpers
    def scan(self, code, slip=None):
        return self.client.post(
            reverse('transfer_scan', args=[(slip or self.slip).pk]), {'code': code},
        )

    def pick(self, item, quantity, slip=None):
        return self.client.post(
            reverse('transfer_pick', args=[(slip or self.slip).pk]),
            {'item_id': item.pk, 'quantity': quantity},
        )

    def release(self, rows, slip=None):
        """rows: [(item_id, qty, method)] — posted exactly as the form does."""
        return self.client.post(
            reverse('transfer_complete', args=[(slip or self.slip).pk]), {
                'item_id': [r[0] for r in rows],
                'quantity_released': [r[1] for r in rows],
                'release_method': [r[2] for r in rows],
            }, follow=True,
        )

    def warehouse_qty(self, product):
        return WarehouseStock.objects.get(
            warehouse=self.branch.warehouse, product=product,
        ).quantity_on_hand

    def floor_qty(self, product):
        return Inventory.objects.filter(
            branch=self.branch, product=product,
        ).values_list('quantity_on_hand', flat=True).first() or 0

    def assert_nothing_moved(self):
        self.assertEqual(self.warehouse_qty(self.vinegar), 40)
        self.assertEqual(self.warehouse_qty(self.tuna), 40)
        self.assertEqual(self.floor_qty(self.vinegar), 0)
        self.assertFalse(StockMovement.objects.exists())
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.status, StockTransfer.Status.APPROVED)

    @staticmethod
    def flashed(response):
        return ' '.join(str(m) for m in response.context['messages'])


class ReleaseScreenRenderingTests(_ReleaseFixture, TestCase):
    def setUp(self):
        self.build()

    def test_line_ids_are_not_thousand_separated(self):
        # The root cause: "4,754" here never equalled the scan response's 4754.
        html = self.client.get(reverse('transfer_complete', args=[self.slip.pk])).content.decode()
        self.assertEqual(
            re.findall(r'data-item-id="([^"]*)"', html), [str(ITEM_PK_A), str(ITEM_PK_B)],
        )
        self.assertEqual(
            re.findall(r'name="item_id" value="([^"]*)"', html),
            [str(ITEM_PK_A), str(ITEM_PK_B)],
        )
        self.assertNotIn('4,754', html)

    def test_picked_quantities_start_at_zero(self):
        html = self.client.get(reverse('transfer_complete', args=[self.slip.pk])).content.decode()
        values = re.findall(
            r'name="quantity_released"[^>]*?value="([^"]*)"', html, re.S,
        )
        self.assertEqual(values, ['0', '0'])

    def test_screen_resumes_from_the_saved_pick_count(self):
        self.scan('4800194152891')
        self.scan('4800194152891')
        html = self.client.get(reverse('transfer_complete', args=[self.slip.pk])).content.decode()
        values = re.findall(
            r'name="quantity_released"[^>]*?value="([^"]*)"', html, re.S,
        )
        self.assertEqual(values, ['2', '0'])

    def test_discrepancy_form_posts_unmangled_item_ids(self):
        # The destination-side form had the same defect: "4,754" would not
        # match any line when the receiving branch flagged a discrepancy.
        self.slip.status = StockTransfer.Status.RELEASED
        self.slip.save()
        self.item_a.quantity_released = 15
        self.item_a.save()
        html = self.client.get(reverse('transfer_detail', args=[self.slip.pk])).content.decode()
        self.assertIn(f'name="item_id" value="{ITEM_PK_A}"', html)
        self.assertNotIn('value="4,754"', html)


class BarcodeScanTests(_ReleaseFixture, TestCase):
    def setUp(self):
        self.build()

    def test_one_scan_adds_exactly_one_and_reports_the_real_count(self):
        payload = self.scan('4800194152891').json()
        self.assertEqual(payload['status'], 'matched')
        self.assertEqual(payload['item_id'], ITEM_PK_A)
        self.assertEqual(payload['quantity_picked'], 1)       # not null
        self.assertEqual(payload['quantity_approved'], 15)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.quantity_picked, 1)

    def test_repeated_scans_accumulate_one_at_a_time(self):
        counts = [self.scan('4800194152891').json()['quantity_picked'] for _ in range(4)]
        self.assertEqual(counts, [1, 2, 3, 4])
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.quantity_picked, 4)

    def test_scans_for_different_products_count_separately(self):
        self.scan('4800194152891')
        self.scan('4800194152891')
        self.scan('TUNA')            # sku fallback: this product has no barcode
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.assertEqual((self.item_a.quantity_picked, self.item_b.quantity_picked), (2, 1))

    def test_scanning_never_exceeds_the_approved_quantity(self):
        StockTransferItem.objects.filter(pk=ITEM_PK_A).update(quantity_approved=2)
        results = [self.scan('4800194152891').json() for _ in range(4)]
        self.assertEqual(
            [r['status'] for r in results], ['matched', 'matched', 'at_limit', 'at_limit'],
        )
        self.assertEqual([r['quantity_picked'] for r in results], [1, 2, 2, 2])
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.quantity_picked, 2)

    def test_scanning_moves_no_stock_and_releases_nothing(self):
        for _ in range(5):
            self.scan('4800194152891')
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.quantity_released, 0)
        self.assert_nothing_moved()

    def test_unknown_barcode_changes_nothing(self):
        payload = self.scan('0000000000000').json()
        self.assertEqual(payload['status'], 'unknown')
        self.assertEqual(
            list(StockTransferItem.objects.values_list('quantity_picked', flat=True)), [0, 0],
        )

    def test_product_not_on_the_slip_is_refused(self):
        Product.objects.create(
            sku_code='STRANGER', product_name='Stranger', unit_price='5.00', barcode='9999999999994',
        )
        payload = self.scan('9999999999994').json()
        self.assertEqual(payload['status'], 'not_on_slip')
        self.assertEqual(
            list(StockTransferItem.objects.values_list('quantity_picked', flat=True)), [0, 0],
        )

    def test_scan_after_release_is_refused(self):
        self.release([(ITEM_PK_A, 3, 'Manual'), (ITEM_PK_B, 0, 'Manual')])
        response = self.scan('4800194152891')
        self.assertEqual(response.status_code, 400)

    def test_staff_cannot_scan(self):
        staff = User.objects.create_user(username='rp-staff', password='pw', role=User.Role.STAFF)
        self.client.force_login(staff)
        response = self.scan('4800194152891')
        self.assertNotEqual(response.status_code, 200)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.quantity_picked, 0)


class ManualPickTests(_ReleaseFixture, TestCase):
    def setUp(self):
        self.build()

    def test_hand_count_for_a_product_without_a_barcode(self):
        payload = self.pick(self.item_b, 5).json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['quantity_picked'], 5)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.quantity_picked, 5)
        self.assertEqual(self.item_b.release_method, StockTransferItem.ReleaseMethod.MANUAL)
        self.assert_nothing_moved()

    def test_hand_count_can_be_corrected_downwards(self):
        self.pick(self.item_b, 6)
        self.pick(self.item_b, 2)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.quantity_picked, 2)

    def test_hand_count_cannot_exceed_approved(self):
        response = self.pick(self.item_b, 9)
        self.assertEqual(response.status_code, 400)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.quantity_picked, 0)

    def test_exactly_the_approved_quantity_is_accepted(self):
        self.assertTrue(self.pick(self.item_b, 8).json()['ok'])

    def test_garbage_quantities_are_rejected(self):
        for bad in ('-1', '1.5', 'abc', '1e3', '9' * 40):
            self.assertEqual(self.pick(self.item_b, bad).status_code, 400, bad)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.quantity_picked, 0)

    def test_blank_is_zero(self):
        self.pick(self.item_b, 4)
        self.assertTrue(self.pick(self.item_b, '').json()['ok'])
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.quantity_picked, 0)

    def test_cannot_set_a_line_belonging_to_another_slip(self):
        other = StockTransfer.objects.create(
            warehouse=self.branch.warehouse, source_branch=self.branch,
            destination_branch=self.branch,
            purpose=StockTransfer.Purpose.BRANCH_RESTOCK,
            requested_by=self.owner, approved_by=self.owner,
            status=StockTransfer.Status.APPROVED,
        )
        response = self.pick(self.item_a, 3, slip=other)
        self.assertEqual(response.status_code, 400)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.quantity_picked, 0)


class ConfirmedReleaseTests(_ReleaseFixture, TestCase):
    def setUp(self):
        self.build()

    def test_release_moves_exactly_what_was_picked_and_nothing_before(self):
        self.scan('4800194152891')
        self.scan('4800194152891')
        self.scan('4800194152891')
        self.assert_nothing_moved()                      # scans alone move nothing

        response = self.release([(ITEM_PK_A, 3, 'Scan'), (ITEM_PK_B, 0, 'Manual')])
        self.assertIn('moved', self.flashed(response))

        self.slip.refresh_from_db()
        self.item_a.refresh_from_db()
        self.assertEqual(self.slip.status, StockTransfer.Status.RELEASED)
        self.assertEqual(self.item_a.quantity_released, 3)    # not the approved 15
        self.assertEqual(self.item_a.release_method, StockTransferItem.ReleaseMethod.SCAN)
        self.assertEqual(self.warehouse_qty(self.vinegar), 37)
        self.assertEqual(self.floor_qty(self.vinegar), 3)
        self.assertEqual(self.warehouse_qty(self.tuna), 40)   # unpicked line untouched

    def test_complete_picking_releases_the_full_approved_quantity(self):
        self.release([(ITEM_PK_A, 15, 'Manual'), (ITEM_PK_B, 8, 'Manual')])
        self.assertEqual(self.warehouse_qty(self.vinegar), 25)
        self.assertEqual(self.warehouse_qty(self.tuna), 32)
        self.assertEqual(self.floor_qty(self.tuna), 8)

    def test_a_second_submit_cannot_double_move(self):
        rows = [(ITEM_PK_A, 3, 'Scan'), (ITEM_PK_B, 0, 'Manual')]
        self.release(rows)
        self.release(rows)
        self.assertEqual(self.warehouse_qty(self.vinegar), 37)
        self.assertEqual(self.floor_qty(self.vinegar), 3)

    def test_picking_more_than_approved_is_refused(self):
        response = self.release([(ITEM_PK_A, 16, 'Manual'), (ITEM_PK_B, 0, 'Manual')])
        self.assertIn('approved', self.flashed(response))
        self.assert_nothing_moved()

    def test_nothing_picked_is_refused(self):
        response = self.release([(ITEM_PK_A, 0, 'Manual'), (ITEM_PK_B, 0, 'Manual')])
        self.assertIn('Nothing has been picked', self.flashed(response))
        self.assert_nothing_moved()

    def test_a_mangled_item_id_is_refused_instead_of_releasing_the_approved_quantity(self):
        # The original bug's second half: ids that match no line used to fall
        # through to "release what was approved" — 15 units for a pick of 3.
        response = self.release([('4,754', 3, 'Scan'), ('4,755', 0, 'Manual')])
        self.assertIn('missing from the form', self.flashed(response))
        self.assert_nothing_moved()

    def test_a_line_omitted_from_the_form_is_refused(self):
        response = self.release([(ITEM_PK_A, 3, 'Scan')])
        self.assertIn('missing from the form', self.flashed(response))
        self.assert_nothing_moved()

    def test_rendered_form_round_trips_to_the_right_quantities(self):
        # GET the real screen, scan, then submit what the page would submit.
        self.scan('4800194152891')
        self.scan('4800194152891')
        html = self.client.get(reverse('transfer_complete', args=[self.slip.pk])).content.decode()
        ids = re.findall(r'name="item_id" value="([^"]*)"', html)
        qtys = re.findall(r'name="quantity_released"[^>]*?value="([^"]*)"', html, re.S)
        methods = re.findall(r'name="release_method"[^>]*?value="([^"]*)"', html, re.S)
        self.assertEqual(qtys, ['2', '0'])
        self.release(list(zip(ids, qtys, methods)))
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.quantity_released, 2)
        self.assertEqual(self.item_a.release_method, StockTransferItem.ReleaseMethod.SCAN)
        self.assertEqual(self.floor_qty(self.vinegar), 2)

    def test_a_bare_post_still_releases_what_was_approved(self):
        # Backwards compatibility for the pre-picking-screen button.
        self.client.post(reverse('transfer_complete', args=[self.slip.pk]), {})
        self.assertEqual(self.floor_qty(self.vinegar), 15)
        self.assertEqual(self.floor_qty(self.tuna), 8)

    def test_stock_shortfall_at_release_rolls_everything_back(self):
        WarehouseStock.objects.filter(product=self.vinegar).update(quantity_on_hand=2)
        Batch.objects.filter(product=self.vinegar).update(quantity=2)
        response = self.release([(ITEM_PK_A, 3, 'Scan'), (ITEM_PK_B, 4, 'Manual')])
        self.assertIn('Not enough warehouse stock', self.flashed(response))
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.status, StockTransfer.Status.APPROVED)
        self.assertEqual(self.warehouse_qty(self.tuna), 40)   # the good line rolled back too
        self.assertFalse(StockMovement.objects.exists())
