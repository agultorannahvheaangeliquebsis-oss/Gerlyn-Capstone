"""Serviceable areas: the matching rule, the Owner's page, and the audit trail.

The rule (branches.coverage), in one paragraph: a branch with no rows is not
restricted; otherwise the MOST SPECIFIC entry that exists for a place decides
it (active covers, inactive excludes), a place with no entry is outside, and
fee / minimum order come from the most specific ACTIVE entry that sets one.
Places are matched by PSGC code, so spelling does not matter.
"""
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts import geo
from accounts.models import BranchManagerProfile, StaffProfile, User
from audit.models import AuditLog
from branches import coverage as cov
from branches.models import Branch, ServiceArea

Type = ServiceArea.AreaType

# Real PSGC places from the bundled snapshot.
BULACAN = geo.resolve_place('Bulacan')
BALIUAG = geo.resolve_place('Bulacan', 'Baliuag')
POBLACION = geo.resolve_place('Bulacan', 'Baliuag', 'Poblacion')
TARCAN = geo.resolve_place('Bulacan', 'Baliuag', 'Tarcan')
MALOLOS = geo.resolve_place('Bulacan', 'Malolos', 'Mojon')
PAMPANGA = geo.resolve_place('Pampanga', 'Angeles', 'Balibago')
_BINONDO = geo.resolve_place('Ncr, City Of Manila, First District', 'Binondo')
MANILA = geo.resolve_place(
    _BINONDO.province_code, _BINONDO.city_code, geo.barangays(_BINONDO.city_code)[0]['code'],
)


def area(branch, place, kind, *, active=True, fee=None, minimum=None, name=None):
    """Add one entry for ``place`` at the level ``kind``."""
    codes = {
        Type.PROVINCE: (place.province_code, '', ''),
        Type.MUNICIPALITY: (place.province_code, place.city_code, ''),
        Type.BARANGAY: (place.province_code, place.city_code, place.barangay_code),
    }[kind]
    default_name = {
        Type.PROVINCE: place.province_name,
        Type.MUNICIPALITY: place.city_name,
        Type.BARANGAY: place.barangay_name,
    }[kind]
    return ServiceArea.objects.create(
        branch=branch, area_type=kind, area_name=name or default_name,
        province_code=codes[0], city_code=codes[1], barangay_code=codes[2],
        is_active=active, delivery_fee=fee, minimum_order=minimum,
    )


class CoverageFixture(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='sa-owner', password='pw', role=User.Role.OWNER)
        self.branch = Branch.objects.create(branch_name='Tarcan', location='Baliuag', owner=self.owner)
        self.other = Branch.objects.create(branch_name='Makinabang', location='Baliuag', owner=self.owner)

    def check(self, place, total=None, branch=None):
        return cov.check_delivery(branch or self.branch, place, order_total=total)


class PlaceResolutionTests(SimpleTestCase):
    def test_codes_and_words_resolve_to_the_same_place(self):
        by_words = geo.resolve_place('Bulacan', 'Baliuag', 'Poblacion')
        by_codes = geo.resolve_place(
            by_words.province_code, by_words.city_code, by_words.barangay_code)
        self.assertEqual(by_words, by_codes)

    def test_the_common_spelling_baliwag_is_baliuag(self):
        self.assertEqual(geo.resolve_place('', 'Baliwag').city_code, BALIUAG.city_code)

    def test_a_municipality_from_another_province_is_not_accepted(self):
        place = geo.resolve_place('Pampanga', BALIUAG.city_code)
        self.assertEqual(place.city_code, '')

    def test_the_province_is_filled_in_from_the_municipality(self):
        self.assertEqual(geo.resolve_place('', 'Baliuag').province_code, BULACAN.province_code)

    def test_unknown_words_are_kept_as_text_not_guessed(self):
        place = geo.resolve_place('Bulacan', 'Nowhereville', 'Brgy. X')
        self.assertEqual(place.city_code, '')
        self.assertEqual(place.city_text, 'Nowhereville')


class HierarchicalMatchingTests(CoverageFixture):
    def test_a_branch_with_no_areas_is_not_restricted(self):
        result = self.check(PAMPANGA)
        self.assertEqual(result.status, cov.OPEN)
        self.assertTrue(result.ok)

    def test_an_active_province_covers_every_municipality_and_barangay_in_it(self):
        area(self.branch, BULACAN, Type.PROVINCE)

        for place in (POBLACION, TARCAN, MALOLOS):
            with self.subTest(place=place.label()):
                result = self.check(place)
                self.assertEqual(result.status, cov.COVERED)
                self.assertEqual(result.area.area_type, Type.PROVINCE)

    def test_an_active_municipality_covers_its_barangays_but_not_its_neighbours(self):
        area(self.branch, BALIUAG, Type.MUNICIPALITY)

        self.assertTrue(self.check(POBLACION).ok)
        self.assertTrue(self.check(TARCAN).ok)
        self.assertEqual(self.check(MALOLOS).status, cov.OUTSIDE)

    def test_a_barangay_entry_covers_only_that_barangay(self):
        area(self.branch, TARCAN, Type.BARANGAY)

        self.assertTrue(self.check(TARCAN).ok)
        self.assertEqual(self.check(POBLACION).status, cov.OUTSIDE)

    def test_places_outside_bulacan_are_rejected(self):
        area(self.branch, BULACAN, Type.PROVINCE)

        for place in (PAMPANGA, MANILA):
            with self.subTest(place=place.label()):
                result = self.check(place)
                self.assertEqual(result.status, cov.OUTSIDE)
                self.assertFalse(result.covered)
                self.assertIn('outside', result.message)

    def test_an_inactive_municipality_excludes_it_from_an_active_province(self):
        area(self.branch, BULACAN, Type.PROVINCE)
        area(self.branch, BALIUAG, Type.MUNICIPALITY, active=False)

        result = self.check(POBLACION)
        self.assertEqual(result.status, cov.OUTSIDE)
        self.assertFalse(result.area.is_active)
        self.assertIn('does not deliver', result.message)
        self.assertTrue(self.check(MALOLOS).ok)

    def test_an_inactive_barangay_excludes_only_that_barangay(self):
        area(self.branch, BALIUAG, Type.MUNICIPALITY)
        area(self.branch, TARCAN, Type.BARANGAY, active=False)

        self.assertEqual(self.check(TARCAN).status, cov.OUTSIDE)
        self.assertTrue(self.check(POBLACION).ok)

    def test_an_active_barangay_can_be_reincluded_inside_an_inactive_municipality(self):
        area(self.branch, BULACAN, Type.PROVINCE)
        area(self.branch, BALIUAG, Type.MUNICIPALITY, active=False)
        area(self.branch, TARCAN, Type.BARANGAY)

        self.assertTrue(self.check(TARCAN).ok)
        self.assertFalse(self.check(POBLACION).ok)

    def test_an_inactive_province_leaves_nothing_covered(self):
        area(self.branch, BULACAN, Type.PROVINCE, active=False)

        self.assertEqual(self.check(POBLACION).status, cov.OUTSIDE)

    def test_a_branchs_areas_do_not_cover_another_branch(self):
        area(self.branch, BULACAN, Type.PROVINCE)
        area(self.other, MALOLOS, Type.MUNICIPALITY)

        self.assertTrue(self.check(POBLACION).ok)
        self.assertEqual(self.check(POBLACION, branch=self.other).status, cov.OUTSIDE)


class FeeAndMinimumTests(CoverageFixture):
    def test_a_specific_entry_overrides_the_broader_fee_and_minimum(self):
        area(self.branch, BULACAN, Type.PROVINCE, fee=Decimal('100'), minimum=Decimal('500'))
        area(self.branch, BALIUAG, Type.MUNICIPALITY, fee=Decimal('50'), minimum=Decimal('200'))
        area(self.branch, TARCAN, Type.BARANGAY, fee=Decimal('0'))

        self.assertEqual(self.check(MALOLOS).delivery_fee, Decimal('100'))
        self.assertEqual(self.check(POBLACION).delivery_fee, Decimal('50'))
        self.assertEqual(self.check(POBLACION).minimum_order, Decimal('200'))
        # A fee of zero is a value, not "blank": free delivery in that barangay.
        self.assertEqual(self.check(TARCAN).delivery_fee, Decimal('0'))

    def test_a_blank_value_inherits_from_the_broader_entry(self):
        area(self.branch, BULACAN, Type.PROVINCE, fee=Decimal('100'), minimum=Decimal('300'))
        area(self.branch, BALIUAG, Type.MUNICIPALITY, fee=Decimal('50'))

        result = self.check(POBLACION)
        self.assertEqual(result.delivery_fee, Decimal('50'))
        self.assertEqual(result.minimum_order, Decimal('300'))

    def test_an_inactive_entry_passes_nothing_on(self):
        area(self.branch, BULACAN, Type.PROVINCE, fee=Decimal('100'))
        area(self.branch, BALIUAG, Type.MUNICIPALITY, active=False, fee=Decimal('1'))
        area(self.branch, TARCAN, Type.BARANGAY)

        # Re-included barangay: inherits from the active province, not from the
        # excluded municipality in between.
        self.assertEqual(self.check(TARCAN).delivery_fee, Decimal('100'))

    def test_an_order_under_the_minimum_is_not_ok_but_the_place_is_covered(self):
        area(self.branch, BULACAN, Type.PROVINCE, minimum=Decimal('500'))

        short = self.check(POBLACION, total=Decimal('499.99'))
        self.assertEqual(short.status, cov.BELOW_MINIMUM)
        self.assertTrue(short.covered)
        self.assertFalse(short.ok)
        self.assertEqual(short.shortfall, Decimal('0.01'))
        self.assertTrue(self.check(POBLACION, total=Decimal('500')).ok)
        self.assertTrue(self.check(POBLACION).ok)  # no total, no minimum check


class IncompleteAndUnrecognisedPlaceTests(CoverageFixture):
    def setUp(self):
        super().setUp()
        area(self.branch, BULACAN, Type.PROVINCE)

    def test_nothing_chosen_asks_for_a_place(self):
        result = self.check(geo.resolve_place())
        self.assertEqual(result.status, cov.INCOMPLETE)
        self.assertFalse(result.covered)

    def test_a_province_alone_is_not_enough(self):
        self.assertEqual(self.check(BULACAN).status, cov.INCOMPLETE)

    def test_a_municipality_without_a_barangay_is_not_enough(self):
        self.assertEqual(self.check(BALIUAG).status, cov.INCOMPLETE)

    def test_words_that_match_no_province_are_rejected(self):
        result = self.check(geo.resolve_place('Narnia', 'Cair Paravel', 'Beaversdam'))
        self.assertEqual(result.status, cov.UNRECOGNISED)

    def test_an_unknown_municipality_in_a_real_province_is_rejected(self):
        result = self.check(geo.resolve_place('Bulacan', 'Nowhereville', 'Brgy. X'))
        self.assertEqual(result.status, cov.UNRECOGNISED)
        self.assertIn('Nowhereville', result.message)

    def test_a_barangay_typed_as_other_is_covered_by_the_municipality(self):
        result = self.check(geo.resolve_place('Bulacan', 'Baliuag', 'Sitio Imaginary'))
        self.assertEqual(result.status, cov.COVERED)

    def test_a_barangay_typed_as_other_cannot_dodge_a_barangay_exclusion(self):
        area(self.branch, TARCAN, Type.BARANGAY, active=False)

        result = self.check(geo.resolve_place('Bulacan', 'Baliuag', 'Sitio Imaginary'))
        self.assertEqual(result.status, cov.NEEDS_BARANGAY)
        self.assertFalse(result.covered)


class DisplayTests(CoverageFixture):
    def test_the_banner_names_the_broadest_entries_only(self):
        area(self.branch, BULACAN, Type.PROVINCE)
        area(self.branch, BALIUAG, Type.MUNICIPALITY)

        configured, text = cov.banner_summary(Branch.objects.filter(pk=self.branch.pk))
        self.assertTrue(configured)
        self.assertEqual(text, 'all of Bulacan')

    def test_the_banner_caps_the_list_and_counts_the_rest(self):
        for name in ('Baliuag', 'Bocaue', 'Bustos', 'Pandi', 'Plaridel'):
            area(self.branch, geo.resolve_place('Bulacan', name), Type.MUNICIPALITY)

        _, text = cov.banner_summary(Branch.objects.filter(pk=self.branch.pk))
        self.assertTrue(text.endswith('and 2 more'), text)

    def test_nothing_is_configured_when_no_branch_has_areas(self):
        configured, text = cov.banner_summary(Branch.objects.all())
        self.assertFalse(configured)
        self.assertEqual(text, '')

    def test_the_public_listing_separates_what_is_excluded(self):
        area(self.branch, BULACAN, Type.PROVINCE, fee=Decimal('100'))
        area(self.branch, TARCAN, Type.BARANGAY, active=False)

        listing = cov.public_listing()
        self.assertEqual([g['branch'] for g in listing], [self.branch])
        self.assertEqual([r['name'] for r in listing[0]['areas']], ['Bulacan'])
        self.assertEqual([r['name'] for r in listing[0]['excluded']], ['Tarcan'])

    def test_covering_branches_names_the_other_branches_that_deliver_there(self):
        area(self.branch, BALIUAG, Type.MUNICIPALITY)
        area(self.other, MALOLOS, Type.MUNICIPALITY)

        self.assertEqual(cov.covering_branches(MALOLOS, exclude_id=self.branch.pk), [self.other])
        self.assertEqual(cov.covering_branches(MALOLOS, exclude_id=self.other.pk), [])


class SeedTests(CoverageFixture):
    def test_seeding_gives_an_active_bulacan_province_entry(self):
        created = cov.seed_default_areas([self.branch])

        self.assertEqual(created, 2)
        bulacan = ServiceArea.objects.get(branch=self.branch, area_type=Type.PROVINCE)
        self.assertEqual(bulacan.area_name, 'Bulacan')
        self.assertTrue(bulacan.is_active)
        self.assertTrue(self.check(POBLACION).ok)
        self.assertFalse(self.check(PAMPANGA).covered)

    def test_seeding_never_sets_a_minimum_order(self):
        cov.seed_default_areas([self.branch])

        self.assertFalse(ServiceArea.objects.filter(minimum_order__isnull=False).exists())

    def test_seeding_twice_does_not_undo_the_owners_edits(self):
        cov.seed_default_areas([self.branch])
        bulacan = ServiceArea.objects.get(branch=self.branch, area_type=Type.PROVINCE)
        bulacan.is_active = False
        bulacan.delivery_fee = Decimal('999.00')
        bulacan.save()

        self.assertEqual(cov.seed_default_areas([self.branch]), 0)

        bulacan.refresh_from_db()
        self.assertFalse(bulacan.is_active)
        self.assertEqual(bulacan.delivery_fee, Decimal('999.00'))
        self.assertEqual(ServiceArea.objects.filter(branch=self.branch).count(), 2)


class AdministrationPageTests(CoverageFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def add(self, **overrides):
        data = {
            'branch': self.branch.pk, 'area_type': Type.PROVINCE,
            'province': BULACAN.province_code, 'city': '', 'barangay': '',
            'delivery_fee': '', 'minimum_order': '', 'notes': '', 'is_active': '1',
        }
        data.update(overrides)
        return self.client.post(reverse('service_area_add'), data)

    # --- who may use it -------------------------------------------------------
    def test_only_the_owner_reaches_the_page_and_its_actions(self):
        area_row = area(self.branch, BULACAN, Type.PROVINCE)
        manager = User.objects.create_user(username='sa-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        BranchManagerProfile.objects.update_or_create(
            user=manager, defaults={'manager_name': 'M', 'branch': self.branch})
        staff = User.objects.create_user(username='sa-staff', password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=staff, defaults={'staff_name': 'S', 'branch': self.branch})
        customer = User.objects.create_user(username='sa-cust', password='pw', role=User.Role.CUSTOMER)

        for user in (manager, staff, customer):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                self.assertEqual(self.client.get(reverse('service_area_list')).status_code, 302)
                self.client.post(reverse('service_area_toggle', args=[area_row.pk]))
                self.client.post(reverse('service_area_edit', args=[area_row.pk]), {'notes': 'x'})
                self.add(province=MALOLOS.province_code, area_type=Type.PROVINCE)
        area_row.refresh_from_db()
        self.assertTrue(area_row.is_active)
        self.assertEqual(area_row.notes, '')
        self.assertEqual(ServiceArea.objects.count(), 1)

    def test_the_page_lists_every_branch_and_warns_about_one_with_no_areas(self):
        area(self.branch, BULACAN, Type.PROVINCE)

        html = self.client.get(reverse('service_area_list')).content.decode()

        self.assertIn('Tarcan', html)
        self.assertIn('Makinabang', html)
        self.assertEqual(html.count('data-unconfigured'), 1)
        self.assertIn('not restricted', html)

    def test_the_sidebar_links_to_the_page_for_the_owner(self):
        html = self.client.get(reverse('service_area_list')).content.decode()
        self.assertIn(reverse('service_area_list'), html)
        self.assertIn('Delivery Areas', html)

    # --- add ------------------------------------------------------------------
    def test_the_owner_adds_a_province_a_municipality_and_a_barangay(self):
        self.add()
        self.add(area_type=Type.MUNICIPALITY, city=BALIUAG.city_code, delivery_fee='50.00')
        self.add(area_type=Type.BARANGAY, city=TARCAN.city_code, barangay=TARCAN.barangay_code,
                 minimum_order='250', notes='Home barangay', is_active='')

        rows = {a.area_type: a for a in ServiceArea.objects.filter(branch=self.branch)}
        self.assertEqual(rows[Type.PROVINCE].area_name, 'Bulacan')
        self.assertEqual(rows[Type.MUNICIPALITY].area_name, 'Baliuag')
        self.assertEqual(rows[Type.MUNICIPALITY].delivery_fee, Decimal('50.00'))
        self.assertEqual(rows[Type.BARANGAY].area_name, 'Tarcan')
        self.assertEqual(rows[Type.BARANGAY].minimum_order, Decimal('250.00'))
        self.assertFalse(rows[Type.BARANGAY].is_active)
        self.assertEqual(rows[Type.BARANGAY].province_code, BULACAN.province_code)

    def test_the_name_comes_from_the_codes_not_the_form(self):
        self.add(area_name='Totally Different')
        self.assertEqual(ServiceArea.objects.get().area_name, 'Bulacan')

    def test_a_municipality_must_belong_to_the_chosen_province(self):
        response = self.add(area_type=Type.MUNICIPALITY, province=PAMPANGA.province_code,
                            city=BALIUAG.city_code)

        self.assertEqual(response.status_code, 200)
        self.assertIn('Choose a municipality in that province.', response.content.decode())
        self.assertFalse(ServiceArea.objects.exists())

    def test_a_barangay_must_belong_to_the_chosen_municipality(self):
        response = self.add(area_type=Type.BARANGAY, city=BALIUAG.city_code,
                            barangay=MALOLOS.barangay_code)

        self.assertIn('Choose a barangay in that municipality.', response.content.decode())
        self.assertFalse(ServiceArea.objects.exists())

    def test_bad_amounts_are_refused(self):
        for field, value in (('delivery_fee', '-1'), ('delivery_fee', '5000.01'),
                             ('delivery_fee', 'abc'), ('minimum_order', '100000.01'),
                             ('delivery_fee', '1.234')):
            with self.subTest(field=field, value=value):
                response = self.add(**{field: value})
                self.assertEqual(response.status_code, 200)
        self.assertFalse(ServiceArea.objects.exists())

    def test_the_same_place_cannot_be_listed_twice_for_a_branch(self):
        self.add()
        response = self.add()

        self.assertIn('already listed', response.content.decode())
        self.assertEqual(ServiceArea.objects.count(), 1)
        self.add(branch=self.other.pk)
        self.assertEqual(ServiceArea.objects.count(), 2)

    def test_an_unknown_branch_or_type_is_refused(self):
        self.add(branch=99999)
        self.add(area_type='planet')
        self.assertFalse(ServiceArea.objects.exists())

    # --- edit / activate / deactivate -----------------------------------------
    def test_editing_changes_the_amounts_notes_and_status_but_not_the_place(self):
        row = area(self.branch, BULACAN, Type.PROVINCE)

        self.client.post(reverse('service_area_edit', args=[row.pk]), {
            'delivery_fee': '75', 'minimum_order': '300', 'notes': 'Within 2 days',
            'is_active': '1', 'province': PAMPANGA.province_code, 'area_type': Type.BARANGAY,
        })

        row.refresh_from_db()
        self.assertEqual(row.delivery_fee, Decimal('75.00'))
        self.assertEqual(row.minimum_order, Decimal('300.00'))
        self.assertEqual(row.notes, 'Within 2 days')
        self.assertEqual((row.area_type, row.province_code), (Type.PROVINCE, BULACAN.province_code))

    def test_deactivating_and_reactivating(self):
        row = area(self.branch, BULACAN, Type.PROVINCE)

        self.client.post(reverse('service_area_toggle', args=[row.pk]))
        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.assertFalse(self.check(POBLACION).covered)

        self.client.post(reverse('service_area_toggle', args=[row.pk]))
        row.refresh_from_db()
        self.assertTrue(row.is_active)

    def test_toggle_needs_a_post(self):
        row = area(self.branch, BULACAN, Type.PROVINCE)
        self.assertEqual(self.client.get(reverse('service_area_toggle', args=[row.pk])).status_code, 405)
        row.refresh_from_db()
        self.assertTrue(row.is_active)


class AuditTrailTests(CoverageFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def logs(self, row):
        return list(AuditLog.objects.filter(
            entity='branches.ServiceArea', record_id=str(row.pk)).order_by('id'))

    def test_adding_editing_and_toggling_are_each_logged_with_the_actor(self):
        self.client.post(reverse('service_area_add'), {
            'branch': self.branch.pk, 'area_type': Type.PROVINCE,
            'province': BULACAN.province_code, 'is_active': '1',
        })
        row = ServiceArea.objects.get()
        self.client.post(reverse('service_area_edit', args=[row.pk]), {
            'delivery_fee': '60', 'is_active': '1'})
        self.client.post(reverse('service_area_toggle', args=[row.pk]))
        self.client.post(reverse('service_area_toggle', args=[row.pk]))

        logs = self.logs(row)
        self.assertEqual([entry.action for entry in logs], ['create', 'update', 'update', 'update'])
        self.assertEqual(
            [entry.action_label for entry in logs],
            ['Added a delivery area', 'Edited a delivery area',
             'Deactivated a delivery area', 'Activated a delivery area'],
        )
        self.assertTrue(all(entry.actor_id == self.owner.pk for entry in logs))
        self.assertTrue(all(entry.branch_id == self.branch.pk for entry in logs))
        self.assertEqual(logs[0].record_repr, 'Bulacan')
        self.assertEqual(logs[2].before, {'is_active': True})
        self.assertEqual(logs[2].after, {'is_active': False})
