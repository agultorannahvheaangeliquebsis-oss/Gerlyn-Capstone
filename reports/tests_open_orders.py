"""Dashboard "Open Orders" — server-side sort + Status/Channel filters.

Covers the whitelists (tampered values fall back), branch scoping staying
intact, filter + sort combined, the 8-row slice being applied AFTER them, and
the params surviving the date-range form and the branch switcher.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from core.querystrings import sort_header
from reports.views import OPEN_ORDERS_LIMIT
from sales.models import SalesTransaction

OS = SalesTransaction.OrderStatus
TT = SalesTransaction.TransactionType


class SortHeaderTests(SimpleTestCase):
    def test_toggles_between_asc_and_desc(self):
        col = sort_header('Total', 'total', 'total', '-total', branch='3')
        self.assertEqual(col['direction'], 'asc')
        self.assertIn('sort=-total', col['href'])
        self.assertIn('branch=3', col['href'])

        col = sort_header('Total', '-total', 'total', '-total')
        self.assertEqual(col['direction'], 'desc')
        self.assertIn('sort=total', col['href'])

    def test_inactive_and_single_key_columns(self):
        col = sort_header('Branch', 'oldest', 'branch')
        self.assertFalse(col['active'])
        self.assertIsNone(col['direction'])
        self.assertIn('sort=branch', col['href'])
        # A single-key column keeps linking to itself when already active.
        again = sort_header('Branch', 'branch', 'branch')
        self.assertTrue(again['active'])
        self.assertIn('sort=branch', again['href'])

    def test_empty_params_are_dropped_and_values_encoded(self):
        col = sort_header('X', 'a', 'b', status='', q='a&b')
        self.assertNotIn('status', col['href'])
        self.assertIn('q=a%26b', col['href'])


class OpenOrdersFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='oo-owner', password='pw', role=User.Role.OWNER,
        )
        cls.branch_a = Branch.objects.create(
            branch_name='Alpha', location='Baliuag', owner=cls.owner,
        )
        cls.branch_b = Branch.objects.create(
            branch_name='Bravo', location='Plaridel', owner=cls.owner,
        )
        cls.manager = User.objects.create_user(
            username='oo-manager', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        profile = cls.manager.manager_profile
        profile.branch = cls.branch_a
        profile.save(update_fields=['branch'])
        cls.base = timezone.now() - timedelta(hours=10)

    @classmethod
    def sale(cls, amount, status=OS.PENDING, channel=TT.PHYSICAL, branch=None,
             age_hours=0):
        txn = SalesTransaction.objects.create(
            branch=branch or cls.branch_a, total_amount=Decimal(amount),
            transaction_type=channel,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        # transaction_date is auto_now_add; .update() is the only way to place
        # it. age_hours is "how much later than the oldest" this order is.
        SalesTransaction.objects.filter(pk=txn.pk).update(
            transaction_date=cls.base + timedelta(hours=age_hours),
            order_status=status,
        )
        txn.refresh_from_db()
        return txn

    def get(self, params=None, user=None):
        client = Client()
        client.force_login(user or self.owner)
        # Range 'all' so fixtures dated "now" never fall out of any window.
        return client.get(reverse('accounts:owner_dashboard')
                          if (user or self.owner).role == User.Role.OWNER
                          else reverse('accounts:manager_dashboard'),
                          {'range': 'all', **(params or {})})

    @staticmethod
    def ids(response):
        return [t.pk for t in response.context['open_orders']]


class OpenOrdersSortTests(OpenOrdersFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.old_cheap = cls.sale('10.00', OS.PREPARING, branch=cls.branch_b, age_hours=0)
        cls.mid_big = cls.sale('500.00', OS.PENDING, branch=cls.branch_a, age_hours=1)
        cls.new_mid = cls.sale('90.00', OS.APPROVED, branch=cls.branch_a, age_hours=2)
        cls.done = cls.sale('999.00', OS.COMPLETED, age_hours=3)
        cls.cancelled = cls.sale('888.00', OS.CANCELLED, age_hours=4)

    def test_default_is_oldest_first_and_excludes_terminal(self):
        r = self.get()
        self.assertEqual(self.ids(r), [self.old_cheap.pk, self.mid_big.pk, self.new_mid.pk])
        self.assertEqual(r.context['open_orders_sort'], 'oldest')

    def test_each_whitelisted_sort(self):
        o, m, n = self.old_cheap.pk, self.mid_big.pk, self.new_mid.pk
        expected = {
            'oldest': [o, m, n],
            'newest': [n, m, o],
            'total': [o, n, m],
            '-total': [m, n, o],
            'branch': [m, n, o],      # Alpha, Alpha (oldest first), Bravo
            '-branch': [o, m, n],     # Bravo, then Alpha (oldest first)
            '-status': [o, n, m],     # Preparing, Approved, Pending
            'status': [m, n, o],      # Pending, Approved, Preparing
        }
        for key, ids in expected.items():
            with self.subTest(sort=key):
                r = self.get({'sort': key})
                self.assertEqual(self.ids(r), ids)
                self.assertEqual(r.context['open_orders_sort'], key)

    def test_unknown_sort_falls_back_to_default(self):
        for bad in ('bogus', 'customer__password', '-transaction_date', '', 'TOTAL'):
            with self.subTest(sort=bad):
                r = self.get({'sort': bad})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.context['open_orders_sort'], 'oldest')
                self.assertEqual(
                    self.ids(r), [self.old_cheap.pk, self.mid_big.pk, self.new_mid.pk],
                )

    def test_active_header_renders_arrow_and_toggle_link(self):
        r = self.get({'sort': 'total'})
        html = r.content.decode()
        self.assertIn('aria-sort="ascending"', html)
        self.assertIn('sort=-total', html)
        r = self.get({'sort': '-total'})
        self.assertIn('aria-sort="descending"', r.content.decode())


class OpenOrdersFilterTests(OpenOrdersFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.p_phys = cls.sale('50.00', OS.PENDING, TT.PHYSICAL, age_hours=0)
        cls.p_online = cls.sale('60.00', OS.PENDING, TT.ONLINE, age_hours=1)
        cls.a_online = cls.sale('70.00', OS.APPROVED, TT.ONLINE, age_hours=2)
        cls.prep_phys = cls.sale('80.00', OS.PREPARING, TT.PHYSICAL, age_hours=3)

    def test_status_filter(self):
        r = self.get({'status': 'Approved'})
        self.assertEqual(self.ids(r), [self.a_online.pk])

    def test_channel_filter(self):
        r = self.get({'channel': 'Online'})
        self.assertEqual(self.ids(r), [self.p_online.pk, self.a_online.pk])

    def test_filter_and_sort_combined(self):
        r = self.get({'channel': 'Online', 'sort': '-total'})
        self.assertEqual(self.ids(r), [self.a_online.pk, self.p_online.pk])
        r = self.get({'status': 'Pending', 'channel': 'Physical', 'sort': 'newest'})
        self.assertEqual(self.ids(r), [self.p_phys.pk])

    def test_terminal_status_is_not_a_valid_filter(self):
        self.sale('1.00', OS.COMPLETED)
        for bad in ('Completed', 'Cancelled', 'nope'):
            with self.subTest(status=bad):
                r = self.get({'status': bad})
                self.assertEqual(r.context['open_orders_status'], '')
                self.assertEqual(len(self.ids(r)), 4)

    def test_unknown_channel_is_ignored(self):
        r = self.get({'channel': 'Mail'})
        self.assertEqual(r.context['open_orders_channel'], '')
        self.assertEqual(len(self.ids(r)), 4)

    def test_filter_with_no_match_shows_filtered_empty_state(self):
        r = self.get({'status': 'Approved', 'channel': 'Physical'})
        self.assertEqual(self.ids(r), [])
        self.assertContains(r, 'No matching open orders')


class OpenOrdersSliceTests(OpenOrdersFixture):
    def test_slice_applies_after_filter_and_sort(self):
        # 10 Pending physical (oldest) then 10 Approved online (newer). With a
        # slice-first implementation the Approved filter would see none of
        # them, because the 8 oldest are all Pending.
        for i in range(10):
            self.sale('10.00', OS.PENDING, TT.PHYSICAL, age_hours=i)
        approved = [
            self.sale(str(100 + i), OS.APPROVED, TT.ONLINE, age_hours=20 + i)
            for i in range(10)
        ]
        r = self.get({'status': 'Approved'})
        self.assertEqual(len(self.ids(r)), OPEN_ORDERS_LIMIT)
        self.assertEqual(self.ids(r), [t.pk for t in approved[:OPEN_ORDERS_LIMIT]])

        # Sort picks the top 8 of ALL open orders, not a re-sort of the
        # 8 oldest: the largest totals live in the newer (Approved) group.
        r = self.get({'sort': '-total'})
        self.assertEqual(
            self.ids(r), [t.pk for t in reversed(approved)][:OPEN_ORDERS_LIMIT],
        )


class OpenOrdersScopingTests(OpenOrdersFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.a = cls.sale('10.00', OS.PENDING, branch=cls.branch_a, age_hours=0)
        cls.b = cls.sale('20.00', OS.PENDING, branch=cls.branch_b, age_hours=1)

    def test_manager_stays_in_branch_under_any_params(self):
        for params in (
            {}, {'sort': 'branch'}, {'sort': '-total'},
            {'branch': self.branch_b.pk}, {'branch': self.branch_b.pk, 'channel': 'Physical'},
        ):
            with self.subTest(params=params):
                r = self.get(params, user=self.manager)
                self.assertEqual(self.ids(r), [self.a.pk])

    def test_owner_branch_selector_still_scopes(self):
        r = self.get({'branch': self.branch_b.pk, 'sort': 'newest'})
        self.assertEqual(self.ids(r), [self.b.pk])
        r = self.get()
        self.assertEqual(self.ids(r), [self.a.pk, self.b.pk])

    def test_unassigned_manager_sees_nothing(self):
        lonely = User.objects.create_user(
            username='oo-lonely', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        profile = lonely.manager_profile
        profile.branch = None
        profile.save(update_fields=['branch'])
        r = self.get({'sort': 'newest', 'branch': self.branch_a.pk}, user=lonely)
        self.assertEqual(self.ids(r), [])


class OpenOrdersCarryTests(OpenOrdersFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sale('10.00', OS.PENDING, TT.ONLINE)

    def test_range_and_branch_forms_carry_sort_and_filters(self):
        r = self.get({'sort': '-total', 'status': 'Pending', 'channel': 'Online',
                      'branch': self.branch_a.pk})
        want = {('sort', '-total'), ('status', 'Pending'), ('channel', 'Online')}
        self.assertTrue(want <= set(r.context['daterange_carry']))
        self.assertTrue(want <= set(r.context['branch_switch_carry']))
        # ...and the date-range form really emits them as hidden inputs.
        self.assertContains(r, '<input type="hidden" name="sort" value="-total">')

    def test_sort_links_keep_filters_branch_and_range(self):
        r = self.get({'status': 'Pending', 'channel': 'Online',
                      'branch': self.branch_a.pk})
        for col in r.context['open_orders_cols'].values():
            with self.subTest(col=col['label']):
                for needle in ('status=Pending', 'channel=Online',
                               f'branch={self.branch_a.pk}', 'range=all'):
                    self.assertIn(needle, col['href'])

    def test_filter_form_carries_sort_branch_and_range(self):
        r = self.get({'sort': 'branch', 'branch': self.branch_a.pk})
        carry = dict(r.context['open_orders_form_carry'])
        self.assertEqual(carry['sort'], 'branch')
        self.assertEqual(carry['branch'], str(self.branch_a.pk))
        self.assertEqual(carry['range'], 'all')
        self.assertNotIn('status', carry)
        self.assertNotIn('channel', carry)


class OpenOrdersChipTests(OpenOrdersFixture):
    """Active-filter chips: each x is a plain link that drops exactly its own
    param and keeps everything else (works without JavaScript)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sale('10.00', OS.PENDING, TT.ONLINE)

    @staticmethod
    def chip_hrefs(response):
        import html
        import re
        from urllib.parse import parse_qs, urlsplit
        found = re.findall(
            r'<a class="oo-chip-remove" aria-label="Remove (\w+) filter" href="([^"]*)"',
            response.content.decode(),
        )
        return {
            name: parse_qs(urlsplit(html.unescape(href)).query)
            for name, href in found
        }

    def test_no_chips_without_active_filters(self):
        r = self.get({'sort': 'total'})
        self.assertNotContains(r, 'oo-chip-remove')
        self.assertNotContains(r, 'oo-badge')

    def test_status_chip_removes_only_status(self):
        r = self.get({'status': 'Pending', 'channel': 'Online', 'sort': '-total',
                      'branch': self.branch_a.pk})
        self.assertContains(r, 'Status: Pending')
        qs = self.chip_hrefs(r)['status']
        self.assertNotIn('status', qs)
        self.assertEqual(qs['channel'], ['Online'])
        self.assertEqual(qs['sort'], ['-total'])
        self.assertEqual(qs['branch'], [str(self.branch_a.pk)])
        self.assertEqual(qs['range'], ['all'])

    def test_channel_chip_removes_only_channel(self):
        r = self.get({'status': 'Pending', 'channel': 'Online', 'sort': 'branch'})
        qs = self.chip_hrefs(r)['channel']
        self.assertNotIn('channel', qs)
        self.assertEqual(qs['status'], ['Pending'])
        self.assertEqual(qs['sort'], ['branch'])

    def test_single_filter_chip_leaves_no_filters(self):
        r = self.get({'channel': 'Physical'})
        self.assertContains(r, 'Channel: In-Store')
        self.assertEqual(set(self.chip_hrefs(r)), {'channel'})
        qs = self.chip_hrefs(r)['channel']
        self.assertNotIn('status', qs)
        self.assertNotIn('channel', qs)

    def test_badge_counts_active_filters(self):
        self.assertContains(self.get({'status': 'Pending'}), '>1</span>')
        self.assertContains(
            self.get({'status': 'Pending', 'channel': 'Online'}), '>2</span>',
        )


class OpenOrdersAnchorTests(OpenOrdersFixture):
    """A sort/filter click is a full-page reload; the links carry #open-orders
    so the browser lands back on the card instead of the top of the page."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sale('10.00', OS.PENDING, TT.ONLINE)

    def test_card_has_anchor_id(self):
        self.assertContains(self.get(), 'id="open-orders"')

    def test_sort_links_and_filter_form_land_on_the_card(self):
        import re
        r = self.get({'status': 'Pending'})
        html_ = r.content.decode()
        sort_hrefs = re.findall(r'<a class="th-sort[^"]*" href="([^"]*)"', html_)
        self.assertEqual(len(sort_hrefs), 4)
        for href in sort_hrefs:
            self.assertTrue(href.endswith('#open-orders'), href)
        self.assertIn('action="#open-orders"', html_)
        for href in re.findall(r'class="oo-chip-remove"[^>]*href="([^"]*)"', html_):
            self.assertTrue(href.endswith('#open-orders'), href)


class OpenOrdersClearButtonTests(OpenOrdersFixture):
    """The top-level "Clear Filters" button: only while a filter is active, and
    it drops both filters while keeping branch, range and sort."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sale('10.00', OS.PENDING, TT.ONLINE)

    def test_hidden_without_filters(self):
        self.assertNotContains(self.get({'sort': 'total'}), 'Clear Filters')

    def test_shown_with_filter_and_keeps_other_params(self):
        import html
        import re
        from urllib.parse import parse_qs, urlsplit
        r = self.get({'status': 'Pending', 'channel': 'Online', 'sort': '-total',
                      'branch': self.branch_a.pk})
        m = re.search(r'<a class="oo-trigger oo-clear" href="([^"]*)"', r.content.decode())
        self.assertIsNotNone(m)
        parts = urlsplit(html.unescape(m.group(1)))
        qs = parse_qs(parts.query)
        self.assertNotIn('status', qs)
        self.assertNotIn('channel', qs)
        self.assertEqual(qs['sort'], ['-total'])
        self.assertEqual(qs['branch'], [str(self.branch_a.pk)])
        self.assertEqual(qs['range'], ['all'])
        self.assertEqual(parts.fragment, 'open-orders')
