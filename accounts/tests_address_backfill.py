"""Reading a place out of a saved address's text, and backfilling its codes.

Coverage matches by PSGC code, so a saved address with only text cannot be
checked. ``geo.place_from_text`` reads the text strictly by position (province
last, municipality before it, barangay before that) and ``backfill_address_codes``
records what it can prove into the blank code columns.
"""
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from accounts import geo
from accounts.models import Address, User
from branches import coverage as cov
from branches.models import Branch

POBLACION = geo.resolve_place('Bulacan', 'Baliuag', 'Poblacion')
BULACAN_TOWN = geo.resolve_place('Bulacan', 'Bulacan')
MALOLOS = geo.resolve_place('Bulacan', 'Malolos')


def codes(place):
    return (place.province_code, place.city_code, place.barangay_code)


class PlaceFromTextTests(SimpleTestCase):
    def read(self, text):
        return codes(geo.place_from_text(text))

    def test_the_apps_own_wording(self):
        self.assertEqual(self.read('Brgy. Poblacion, Baliuag, Bulacan'), codes(POBLACION))

    def test_a_street_in_front_and_philippines_behind(self):
        self.assertEqual(
            self.read('14 Rizal St., Brgy. Poblacion, Baliuag, Bulacan, Philippines'),
            codes(POBLACION),
        )

    def test_the_old_spelling_of_baliuag(self):
        self.assertEqual(self.read('Poblacion, Baliwag, Bulacan'), codes(POBLACION))

    def test_a_region_and_a_postal_code_at_the_end_are_ignored(self):
        region = geo.region_name(geo.region_of_province(POBLACION.province_code))
        self.assertEqual(
            self.read(f'Poblacion, Baliuag, Bulacan, {region}, 3006'), codes(POBLACION),
        )

    def test_a_landmark_with_commas_does_not_hide_the_place(self):
        self.assertEqual(
            self.read('Poblacion, Baliuag, Bulacan (Landmark: near the church, by the school)'),
            codes(POBLACION),
        )

    def test_the_municipality_of_bulacan_inside_the_province_of_bulacan(self):
        # The same word is a province and a municipality; position decides.
        self.assertEqual(self.read('Brgy. Poblacion, Bulacan, Bulacan')[:2], codes(BULACAN_TOWN)[:2])

    def test_an_unknown_barangay_leaves_the_barangay_blank(self):
        province, city, barangay = self.read('Nowhere Heights, Baliuag, Bulacan')
        self.assertEqual((province, city), codes(POBLACION)[:2])
        self.assertEqual(barangay, '')

    def test_a_barangay_found_elsewhere_in_the_text_is_not_used(self):
        # "Poblacion" is a real barangay of Baliuag, but not where this text
        # puts the barangay.
        self.assertEqual(self.read('Poblacion, 14 Rizal St., Baliuag, Bulacan')[2], '')

    def test_no_province_means_nothing_is_read(self):
        self.assertEqual(self.read('14 Rizal St., Poblacion, Baliuag'), ('', '', ''))

    def test_a_municipality_that_is_not_in_the_province_is_not_taken(self):
        province, city, barangay = self.read('Poblacion, Manila, Bulacan')
        self.assertEqual((province, city, barangay), (POBLACION.province_code, '', ''))

    def test_nothing_in_nothing_out(self):
        for text in ('', None, '   ', ',,,', 'Home'):
            self.assertEqual(self.read(text), ('', '', ''), text)


class BackfillCommandTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='bf-customer', password='pw', role=User.Role.CUSTOMER
        )

    def address(self, text, **fields):
        return Address.objects.create(user=self.customer, text=text, **fields)

    def run_command(self, *args):
        out = StringIO()
        call_command('backfill_address_codes', *args, stdout=out)
        return out.getvalue()

    def test_a_text_only_address_gets_its_codes_and_region(self):
        address = self.address('14 Rizal St., Brgy. Poblacion, Baliuag, Bulacan')

        self.run_command()

        address.refresh_from_db()
        self.assertEqual((address.province, address.city, address.barangay), codes(POBLACION))
        self.assertEqual(address.region, geo.region_of_province(POBLACION.province_code))
        self.assertEqual(address.text, '14 Rizal St., Brgy. Poblacion, Baliuag, Bulacan')

    def test_the_filled_address_is_now_checkable_against_a_branch(self):
        owner = User.objects.create_user(username='bf-owner', password='pw', role=User.Role.OWNER)
        branch = Branch.objects.create(branch_name='Tarcan', location='Baliuag', owner=owner)
        cov.seed_default_areas([branch])
        address = self.address('Brgy. Poblacion, Baliuag, Bulacan')
        before = geo.resolve_place(address.province, address.city, address.barangay)
        self.assertFalse(cov.check_delivery(branch, before).ok)

        self.run_command()

        address.refresh_from_db()
        after = geo.resolve_place(address.province, address.city, address.barangay)
        self.assertTrue(cov.check_delivery(branch, after).ok)

    def test_dry_run_changes_nothing(self):
        address = self.address('Brgy. Poblacion, Baliuag, Bulacan')

        output = self.run_command('--dry-run')

        address.refresh_from_db()
        self.assertEqual((address.province, address.city, address.barangay, address.region), ('', '', '', ''))
        self.assertIn('would be updated', output)

    def test_a_stored_code_is_never_overwritten(self):
        address = self.address(
            'Brgy. Poblacion, Baliuag, Bulacan',
            province=MALOLOS.province_code, city=MALOLOS.city_code,
        )

        output = self.run_command()

        address.refresh_from_db()
        self.assertEqual((address.province, address.city, address.barangay), (
            MALOLOS.province_code, MALOLOS.city_code, '',
        ))
        self.assertIn(f'address {address.pk}', output)
        self.assertIn('text disagrees with stored codes', output)

    def test_a_missing_barangay_is_filled_when_the_rest_agrees(self):
        address = self.address(
            'Brgy. Poblacion, Baliuag, Bulacan',
            region=geo.region_of_province(POBLACION.province_code),
            province=POBLACION.province_code, city=POBLACION.city_code,
        )

        self.run_command()

        address.refresh_from_db()
        self.assertEqual(address.barangay, POBLACION.barangay_code)

    def test_a_part_read_address_keeps_what_was_proved_and_is_listed(self):
        address = self.address('Nowhere Heights, Baliuag, Bulacan')

        output = self.run_command()

        address.refresh_from_db()
        self.assertEqual((address.province, address.city), codes(POBLACION)[:2])
        self.assertEqual(address.barangay, '')
        self.assertIn(f'address {address.pk}', output)
        self.assertIn('barangay not recognised', output)

    def test_an_unreadable_address_is_listed_and_left_alone(self):
        address = self.address('Home')

        output = self.run_command()

        address.refresh_from_db()
        self.assertEqual((address.province, address.city, address.barangay, address.region), ('', '', '', ''))
        self.assertIn('province not recognised', output)

    def test_the_report_never_prints_address_text(self):
        self.address('221B Secret Lane, Nowhere Heights, Baliuag, Bulacan')
        self.address('Hidden Cottage')

        output = self.run_command()

        self.assertNotIn('Secret', output)
        self.assertNotIn('Hidden', output)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.address('Brgy. Poblacion, Baliuag, Bulacan')
        self.run_command()

        second = self.run_command()

        self.assertIn('0 address(es) were updated', second)

    def test_a_complete_address_is_not_touched(self):
        address = self.address(
            'Somewhere else entirely',
            region='03', province=POBLACION.province_code,
            city=POBLACION.city_code, barangay=POBLACION.barangay_code,
        )

        self.run_command()

        address.refresh_from_db()
        self.assertEqual((address.province, address.city, address.barangay), codes(POBLACION))
