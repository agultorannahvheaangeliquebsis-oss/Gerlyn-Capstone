"""Filing a damaged-product report as staff: quantity rules, photo rules,
branch pinning, and that filing alone never touches stock.

    python manage.py test damage_reports.tests_staff_reports
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Notification
from damage_reports import photos, services
from damage_reports.errors import DamageReportError, DamageReportForbidden
from damage_reports.models import DamageReport, DamageReportEvent, DamageReportPhoto
from damage_reports.testing import (
    FLOOR, WAREHOUSE, DamageFixtures, blank_file, image_bytes, jpg, padded_png, png,
    upload,
)
from inventory.models import Inventory, StockMovement

Status = DamageReport.Status


class QuantityValidationTests(DamageFixtures, TestCase):
    """The reported quantity can never exceed what is on hand."""

    def setUp(self):
        self.make_world()

    def refuses(self, **overrides):
        with self.assertRaises(DamageReportError) as ctx:
            self.file_report(**overrides)
        self.assertFalse(DamageReport.objects.exists())
        return str(ctx.exception)

    def test_a_valid_report_is_filed_pending_and_moves_no_stock(self):
        before = (self.on_hand(FLOOR), self.batch_qty(self.floor_batch))

        report = self.file_report()

        self.assertEqual(report.status, Status.PENDING)
        self.assertEqual(report.quantity, 5)
        self.assertEqual(report.branch, self.branch)
        self.assertEqual(report.reported_by, self.staff)
        self.assertEqual(report.reporter_type, 'staff')
        self.assertEqual(report.estimated_unit_cost, Decimal('20.00'))
        self.assertRegex(report.report_number, r'^DR-\d{4}-\d{3,}$')
        self.assertEqual((self.on_hand(FLOOR), self.batch_qty(self.floor_batch)), before)
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(report.events.get().to_status, Status.PENDING)

    def test_quantity_equal_to_the_batch_is_allowed(self):
        self.assertEqual(self.file_report(quantity='30').quantity, 30)

    def test_more_than_the_batch_holds_is_refused(self):
        self.assertIn('only 30 units', self.refuses(quantity='31'))

    def test_more_than_the_aggregate_stock_is_refused_with_no_batch(self):
        message = self.refuses(batch_id='', quantity='31')
        self.assertIn('only 30 units', message)
        self.assertIn('floor stock', message)

    def test_warehouse_quantity_is_checked_against_warehouse_stock(self):
        self.file_report(location=WAREHOUSE, batch_id=self.wh_batch.pk, quantity='40')
        DamageReport.objects.all().delete()
        self.assertIn('only 40 units', self.refuses(
            location=WAREHOUSE, batch_id=self.wh_batch.pk, quantity='41'))

    def test_zero_negative_and_non_numeric_quantities_are_refused(self):
        for bad in ('0', '-3', '2.5', 'abc', '', '1e3', '٣'):
            with self.subTest(quantity=bad):
                self.refuses(quantity=bad)

    def test_an_absurdly_long_number_is_refused_not_a_500(self):
        self.refuses(quantity='9' * 5000)

    def test_a_quantity_over_the_global_maximum_is_refused(self):
        self.refuses(quantity='100001')

    def test_a_product_with_no_stock_in_that_location_is_refused(self):
        self.refuses(product_id=self.product2.pk, batch_id='', quantity='1')

    def test_open_reports_hold_their_units(self):
        self.file_report(quantity='25')

        message = self.refuses_second(quantity='6')

        self.assertIn('Only 5 units', message)
        self.assertIn('already part of an open damage report', message)
        # ...and exactly the remainder is still reportable.
        self.assertEqual(self.file_report(quantity='5', description='Rest.').quantity, 5)

    def refuses_second(self, **overrides):
        before = DamageReport.objects.count()
        with self.assertRaises(DamageReportError) as ctx:
            self.file_report(**overrides)
        self.assertEqual(DamageReport.objects.count(), before)
        return str(ctx.exception)

    def test_a_rejected_report_gives_its_units_back(self):
        first = self.file_report(quantity='30')
        self.refuses_second(quantity='1')

        services.reject_report(first.pk, self.manager, reason='Not damaged.')

        self.assertEqual(self.file_report(quantity='30', description='Again.').quantity, 30)

    def test_the_form_shows_how_much_can_still_be_reported(self):
        self.file_report(quantity='25')
        options = services.form_stock_options(self.branch)
        floor = next(g for g in options if g['location'] == FLOOR)
        entry = floor['products'][0]
        self.assertEqual(entry['on_hand'], 30)
        self.assertEqual(entry['reportable'], 5)
        self.assertEqual(entry['batches'][0]['reportable'], 5)


class FormInputTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()

    def test_batch_must_belong_to_the_product_branch_and_location(self):
        other = self.add_stock(self.other_branch, self.product, FLOOR, 10)
        wrong_product = self.add_stock(self.branch, self.product2, FLOOR, 10)
        for batch_id, location in (
            (other.pk, FLOOR),                 # another branch's lot
            (wrong_product.pk, FLOOR),         # another product's lot
            (self.wh_batch.pk, FLOOR),         # right product, wrong location
        ):
            with self.subTest(batch=batch_id, location=location):
                with self.assertRaises(DamageReportError):
                    self.file_report(batch_id=batch_id, location=location)
        self.assertFalse(DamageReport.objects.exists())

    def test_a_non_numeric_batch_is_refused(self):
        with self.assertRaises(DamageReportError):
            self.file_report(batch_id='abc')

    def test_unknown_location_type_and_product_are_refused(self):
        for overrides in (
            {'location': 'Attic'}, {'damage_type': 'customer'},
            {'damage_type': 'lost'}, {'product_id': 999999}, {'product_id': ''},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(DamageReportError):
                    self.file_report(**overrides)

    def test_description_is_required_and_bounded(self):
        for text in ('', '   ', 'x' * 1001):
            with self.subTest(length=len(text)):
                with self.assertRaises(DamageReportError):
                    self.file_report(description=text)

    def test_batch_is_optional_and_a_report_without_one_has_no_batch(self):
        self.assertIsNone(self.file_report(batch_id='').inventory_batch)

    def test_a_batch_that_came_in_on_a_purchase_order_links_it(self):
        po = self.make_po()
        batch = self.add_stock(self.branch, self.product, WAREHOUSE, 6, procurement=po)
        report = self.file_report(location=WAREHOUSE, batch_id=batch.pk, quantity='2')
        self.assertEqual(report.procurement, po)

    def test_a_named_purchase_order_must_be_this_branchs_and_carry_the_product(self):
        po = self.make_po()
        self.assertEqual(
            self.file_report(procurement_id=po.pk, damage_type='arrival').procurement, po)
        other_po = self.make_po(branch=self.other_branch)
        with self.assertRaises(DamageReportError):
            self.file_report(procurement_id=other_po.pk)
        unrelated = self.make_po(product=self.product2)
        with self.assertRaises(DamageReportError):
            self.file_report(procurement_id=unrelated.pk)

    def test_the_form_lists_only_a_purchase_orders_own_lots(self):
        po = self.make_po()
        lot = self.add_stock(self.branch, self.product, WAREHOUSE, 6, procurement=po)

        options = services.form_stock_options(self.branch, po)

        products = [p for g in options for p in g['products']]
        self.assertEqual([p['key'] for p in products], [f'{WAREHOUSE}|{self.product.pk}'])
        self.assertEqual(products[0]['on_hand'], 6)
        self.assertEqual([b['id'] for b in products[0]['batches']], [lot.pk])


class BranchPinningTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()

    def test_staff_always_file_for_their_own_branch(self):
        self.assertEqual(self.file_report().branch, self.branch)

    def test_staff_cannot_name_another_branch(self):
        with self.assertRaises(DamageReportForbidden):
            self.file_report(branch_id=self.other_branch.pk)

    def test_an_unassigned_account_cannot_file_at_all(self):
        from accounts.models import StaffProfile
        StaffProfile.objects.filter(user=self.staff).update(branch=None)
        self.staff.refresh_from_db()
        with self.assertRaises(DamageReportError):
            self.file_report()

    def test_stock_in_another_branch_cannot_be_reported(self):
        theirs = self.add_stock(self.other_branch, self.product, FLOOR, 10)
        with self.assertRaises(DamageReportError):
            self.file_report(batch_id=theirs.pk)

    def test_an_owner_must_choose_the_branch(self):
        with self.assertRaises(DamageReportError):
            self.file_report(user=self.owner, branch_id='')
        report = self.file_report(user=self.owner, branch_id=self.branch.pk)
        self.assertEqual(report.branch, self.branch)

    def test_a_supplier_or_customer_cannot_file_a_staff_report(self):
        for user in (self.customer,):
            with self.subTest(role=user.role):
                with self.assertRaises(DamageReportForbidden):
                    self.file_report(user=user)


class PhotoValidationTests(DamageFixtures, TestCase):
    """JPG/PNG only, 5 MB each, at most 5 per report."""

    def setUp(self):
        self.make_world()

    def refuses(self, *files, expect=''):
        with self.assertRaises(DamageReportError) as ctx:
            self.file_report(photos=list(files))
        self.assertFalse(DamageReport.objects.exists())
        self.assertFalse(DamageReportPhoto.objects.exists())
        if expect:
            self.assertIn(expect, str(ctx.exception))

    def test_jpeg_and_png_are_accepted_and_stored_under_their_real_type(self):
        report = self.file_report(photos=[png('a.png'), jpg('b.jpg')])

        names = [p.image.name for p in report.photos.all()]
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].endswith('.png'))
        self.assertTrue(names[1].endswith('.jpg'))
        self.assertTrue(all(n.startswith('damage_reports/') for n in names))

    def test_the_uploaders_file_name_is_never_kept(self):
        report = self.file_report(photos=[png('../../evil name.png')])
        self.assertNotIn('evil', report.photos.get().image.name)

    def test_a_jpeg_named_dot_png_is_stored_as_the_jpeg_it_is(self):
        report = self.file_report(photos=[upload('looks-like.png', image_bytes('JPEG'), 'image/png')])
        self.assertTrue(report.photos.get().image.name.endswith('.jpg'))

    def test_other_image_formats_are_refused(self):
        self.refuses(upload('a.webp', image_bytes('WEBP'), 'image/webp'), expect='JPG or PNG')
        self.refuses(upload('a.gif', image_bytes('GIF'), 'image/gif'), expect='JPG or PNG')
        self.refuses(upload('a.bmp', image_bytes('BMP'), 'image/bmp'), expect='JPG or PNG')

    def test_a_page_or_svg_dressed_up_as_a_photo_is_refused(self):
        self.refuses(upload('a.jpg', b'<html><script>alert(1)</script></html>', 'image/jpeg'),
                     expect='not a valid image')
        self.refuses(upload('a.png', b'<svg xmlns="http://www.w3.org/2000/svg"/>', 'image/png'),
                     expect='not a valid image')

    def test_an_empty_or_truncated_file_is_refused(self):
        self.refuses(upload('a.png', b'', 'image/png'), expect='empty')
        self.refuses(upload('a.png', image_bytes('PNG')[:30], 'image/png'),
                     expect='not a valid image')

    def test_a_file_over_five_megabytes_is_refused(self):
        too_big = padded_png(photos.MAX_PHOTO_BYTES + 1)
        self.refuses(upload('big.png', too_big), expect='larger than 5 MB')

    def test_exactly_five_megabytes_is_accepted(self):
        exact = padded_png(photos.MAX_PHOTO_BYTES)
        report = self.file_report(photos=[upload('edge.png', exact)])
        self.assertEqual(report.photos.count(), 1)

    def test_an_enormous_picture_that_compresses_small_is_refused(self):
        from PIL import Image
        import io
        out = io.BytesIO()
        Image.new('1', (8000, 8000)).save(out, 'PNG')   # 64 megapixels
        self.assertLess(len(out.getvalue()), photos.MAX_PHOTO_BYTES)
        self.refuses(upload('huge.png', out.getvalue()), expect='too many pixels')

    def test_five_photos_are_the_most_a_report_can_hold(self):
        report = self.file_report(photos=[png(f'p{i}.png') for i in range(5)])
        self.assertEqual(report.photos.count(), 5)
        DamageReport.objects.all().delete()
        self.refuses(*[png(f'p{i}.png') for i in range(6)], expect='at most 5 photos')

    def test_one_bad_photo_rejects_the_whole_report(self):
        self.refuses(png(), upload('bad.png', b'nope'))

    def test_a_staff_report_may_have_no_photo(self):
        report = self.file_report(photos=[])
        self.assertEqual(report.photos.count(), 0)

    def test_validate_photos_enforces_a_minimum(self):
        with self.assertRaises(DamageReportError):
            photos.validate_photos([], minimum=1)
        with self.assertRaises(DamageReportError):
            photos.validate_photos([png()], already_attached=5)


class FilingNotificationTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()

    def recipients(self):
        return set(
            Notification.objects.filter(event_type='damage_report_submitted')
            .values_list('user__username', flat=True))

    def test_the_owner_and_branch_manager_are_told_but_not_other_staff(self):
        report = self.file_report()

        self.assertEqual(self.recipients(), {'dr-owner', 'dr-manager'})
        note = Notification.objects.get(
            user=self.manager, event_type='damage_report_submitted')
        self.assertEqual(note.reference_id, str(report.pk))
        self.assertEqual(note.action_url, reverse('damage_report_detail', args=[report.pk]))
        self.assertIn(report.report_number, note.title)
        self.assertEqual(note.branch, self.branch)

    def test_the_other_branchs_manager_is_not_told(self):
        self.file_report()
        self.assertNotIn('dr-manager2', self.recipients())

    def test_a_manager_filing_their_own_report_is_not_told_about_it(self):
        self.file_report(user=self.manager)
        self.assertEqual(self.recipients(), {'dr-owner'})

    def test_a_manager_without_the_module_is_not_told(self):
        from accounts.models import User
        User.objects.filter(pk=self.manager.pk).update(allowed_modules=['sales_pos'])
        self.file_report()
        self.assertEqual(self.recipients(), {'dr-owner'})


class StaffFormViewTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.client.force_login(self.staff)
        self.url = reverse('damage_report_create')

    def test_the_form_lists_the_branchs_stock_and_batches(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Canned Sardines 155g')
        self.assertContains(response, self.floor_batch.batch_number)
        self.assertContains(response, 'multiple')
        self.assertContains(response, 'accept="image/jpeg,image/png"')

    def test_a_stock_row_link_prefills_the_batch(self):
        response = self.client.get(f'{self.url}?batch={self.wh_batch.pk}')
        self.assertContains(response, f'value="{WAREHOUSE}|{self.product.pk}"')
        html = response.content.decode()
        self.assertRegex(html, rf'<option value="{self.wh_batch.pk}"[^>]*selected')

    def test_posting_files_the_report_and_redirects_to_it(self):
        response = self.client.post(self.url, self.post_data(photos=[png(), jpg()]))

        report = DamageReport.objects.get()
        self.assertRedirects(response, reverse('damage_report_detail', args=[report.pk]))
        self.assertEqual(report.photos.count(), 2)
        self.assertEqual(report.reported_by, self.staff)

    def test_a_refused_post_redraws_the_form_with_the_message_and_keeps_input(self):
        response = self.client.post(self.url, self.post_data(quantity='99', description='Keep me'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'only 30 units')
        self.assertContains(response, 'Keep me')
        self.assertFalse(DamageReport.objects.exists())

    def test_a_bad_photo_is_refused_through_the_view_too(self):
        response = self.client.post(self.url, self.post_data(
            photos=[upload('a.png', b'<html></html>')]))
        self.assertContains(response, 'not a valid image')
        self.assertFalse(DamageReport.objects.exists())

    def test_a_posted_branch_id_is_ignored_for_staff(self):
        response = self.client.post(self.url, self.post_data(branch_id=self.other_branch.pk))
        self.assertEqual(DamageReport.objects.get().branch, self.branch)
        self.assertEqual(response.status_code, 302)

    def test_an_owner_is_asked_for_a_branch_first(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        self.assertContains(response, 'Which branch is this report for?')
        response = self.client.get(f'{self.url}?branch={self.branch.pk}')
        self.assertContains(response, 'Canned Sardines 155g')

    def test_an_owner_files_for_the_chosen_branch(self):
        self.client.force_login(self.owner)
        data = self.post_data(branch_id=self.branch.pk)
        self.client.post(self.url, data)
        self.assertEqual(DamageReport.objects.get().branch, self.branch)

    def test_a_customer_cannot_open_the_staff_form(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, self.post_data()).status_code, 403)
        self.assertFalse(DamageReport.objects.exists())

    def test_anonymous_visitors_are_sent_to_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_a_staff_account_without_the_add_grant_is_refused(self):
        from accounts.models import User
        User.objects.filter(pk=self.staff.pk).update(
            module_actions={'damage_reports': []})
        self.client.force_login(User.objects.get(pk=self.staff.pk))
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, self.post_data()).status_code, 403)


class FilingInAutocommitTests(DamageFixtures, TransactionTestCase):
    """The view runs with no wrapping transaction in production (there is no
    ATOMIC_REQUESTS), which a plain TestCase hides: a select_for_update outside
    transaction.atomic() would raise only here."""

    def setUp(self):
        self.make_world()

    def test_staff_can_file_through_the_real_view(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('damage_report_create'), self.post_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DamageReport.objects.get().quantity, 5)

    def test_an_owner_can_file_through_the_real_view(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('damage_report_create'), self.post_data(branch_id=self.branch.pk))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DamageReport.objects.get().branch, self.branch)
