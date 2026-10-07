"""Pagination: the shared helper, the pager tag, and the pages that use them."""
from decimal import Decimal

from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import User
from core.pagination import DEFAULT_PAGE_SIZE, page_window, paginate
from inventory.models import Product, ProductCategory


class _FakePage:
    """Just enough of a Page for page_window()."""

    class _Paginator:
        def __init__(self, num_pages):
            self.num_pages = num_pages

    def __init__(self, number, num_pages):
        self.number = number
        self.paginator = self._Paginator(num_pages)


class PageWindowTests(SimpleTestCase):
    def test_short_lists_show_every_page(self):
        self.assertEqual(page_window(_FakePage(2, 4)), [1, 2, 3, 4])

    def test_long_lists_collapse_the_far_pages_into_gaps(self):
        self.assertEqual(page_window(_FakePage(1, 20)), [1, 2, 3, None, 20])
        self.assertEqual(page_window(_FakePage(10, 20)), [1, None, 8, 9, 10, 11, 12, None, 20])
        self.assertEqual(page_window(_FakePage(20, 20)), [1, None, 18, 19, 20])

    def test_a_gap_is_never_drawn_for_a_single_skipped_page(self):
        self.assertEqual(page_window(_FakePage(5, 8)), [1, 2, 3, 4, 5, 6, 7, 8])


class PaginateHelperTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.items = list(range(40))

    def _page(self, query):
        return paginate(self.factory.get('/x/', query), self.items)

    def test_default_page_size(self):
        page = self._page({})
        self.assertEqual(len(page.object_list), DEFAULT_PAGE_SIZE)
        self.assertEqual(page.number, 1)

    def test_second_page(self):
        page = self._page({'page': '2'})
        self.assertEqual(list(page.object_list)[0], DEFAULT_PAGE_SIZE)

    def test_junk_and_out_of_range_pages_are_clamped_not_errors(self):
        self.assertEqual(self._page({'page': 'abc'}).number, 1)
        self.assertEqual(self._page({'page': '0'}).number, 1)
        self.assertEqual(self._page({'page': '-4'}).number, 1)
        self.assertEqual(self._page({'page': '999'}).number, 3)   # the last page
        self.assertEqual(self._page({'page': ''}).number, 1)


class PagerTagTests(TestCase):
    def _render(self, query, items=40, noun='widget'):
        request = RequestFactory().get('/list/', query)
        page = paginate(request, list(range(items)))
        template = Template('{% load pager %}{% pager page_obj noun="' + noun + '" %}')
        return template.render(Context({'request': request, 'page_obj': page}))

    def test_count_line_and_buttons(self):
        html = self._render({})
        self.assertIn('Showing 1–15 of 40 widgets', html)
        self.assertIn('Previous', html)
        self.assertIn('Next', html)
        self.assertIn('aria-current="page"', html)

    def test_previous_is_disabled_on_the_first_page_and_next_on_the_last(self):
        first = self._render({})
        self.assertNotIn('rel="prev"', first)
        self.assertIn('rel="next"', first)
        last = self._render({'page': '3'})
        self.assertIn('rel="prev"', last)
        self.assertNotIn('rel="next"', last)

    def test_links_keep_the_other_filters_and_replace_only_page(self):
        html = self._render({'q': 'rice & beans', 'status': 'Paid', 'page': '2'})
        self.assertIn('q=rice+%26+beans', html)
        self.assertIn('status=Paid', html)
        self.assertIn('page=3', html)
        self.assertIn('page=1', html)
        # Page 2 is where we are (drawn as a span, not a link), and the old
        # page=2 must not ride along on the others — no duplicate parameters.
        self.assertNotIn('page=2', html)

    def test_modal_opening_parameters_are_not_carried(self):
        html = self._render({'receive': '12', 'edit': '3', 'q': 'x'})
        self.assertNotIn('receive=', html)
        self.assertNotIn('edit=', html)
        self.assertIn('q=x', html)

    def test_a_single_page_shows_the_count_but_no_buttons(self):
        html = self._render({}, items=5)
        self.assertIn('Showing 1–5 of 5 widgets', html)
        self.assertNotIn('Next', html)

    def test_an_empty_list_draws_nothing(self):
        self.assertEqual(self._render({}, items=0).strip(), '')

    def test_singular_noun_for_one_record(self):
        self.assertIn('of 1 widget<', self._render({}, items=1).replace(' ', ' ').replace('\n', ''))


class ProductListPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='pager-owner', password='pw', role=User.Role.OWNER
        )
        category = ProductCategory.objects.create(category_name='Pager Goods')
        for i in range(40):
            Product.objects.create(
                category=category, sku_code=f'PG-{i:03d}', product_name=f'Pager Item {i:03d}',
                unit_price=Decimal('10.00'),
            )

    def setUp(self):
        self.client.force_login(self.owner)

    def _names(self, response):
        return [row['product'].product_name for row in response.context['rows']]

    def test_first_page_is_one_page_of_alphabetical_rows(self):
        response = self.client.get(reverse('product_list'))
        names = self._names(response)
        self.assertEqual(len(names), DEFAULT_PAGE_SIZE)
        self.assertEqual(names, sorted(names))
        self.assertEqual(names[0], 'Pager Item 000')
        self.assertContains(response, 'Showing 1–15 of 40 products')

    def test_pages_do_not_overlap_or_skip(self):
        seen = []
        for number in (1, 2, 3):
            seen += self._names(self.client.get(reverse('product_list'), {'page': number}))
        self.assertEqual(len(seen), 40)
        self.assertEqual(len(set(seen)), 40)

    def test_last_page_has_the_remainder(self):
        response = self.client.get(reverse('product_list'), {'page': 3})
        self.assertEqual(len(self._names(response)), 10)
        self.assertContains(response, 'Showing 31–40 of 40 products')

    def test_a_page_past_the_end_opens_the_last_page(self):
        response = self.client.get(reverse('product_list'), {'page': 999})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 3)

    def test_search_covers_every_page_not_just_the_visible_one(self):
        # "Item 039" is on page 3; a search must find it from a fresh visit.
        response = self.client.get(reverse('product_list'), {'q': 'Item 039'})
        self.assertEqual(self._names(response), ['Pager Item 039'])

    def test_pager_links_keep_the_search(self):
        response = self.client.get(reverse('product_list'), {'q': 'Pager'})
        self.assertContains(response, 'q=Pager&amp;page=2')

    def test_total_still_reflects_the_whole_filtered_result(self):
        response = self.client.get(reverse('product_list'), {'q': 'Pager', 'page': 2})
        self.assertEqual(response.context['shown_count'], 40)
