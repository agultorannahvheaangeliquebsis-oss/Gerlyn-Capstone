"""Storefront pagination: the catalog grid (including its live-search XHR) and
the customer's My Orders list."""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product
from orders.customer_views import CATALOG_PAGE_SIZE
from sales.models import SalesTransaction


class CatalogPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='cat-page-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Catalog Branch', location='Baliuag', owner=cls.owner
        )
        for i in range(30):
            product = Product.objects.create(
                sku_code=f'CP-{i:02d}', product_name=f'Catalog Item {i:02d}',
                unit_of_measure='Piece', unit_price=Decimal('10.00'),
            )
            Inventory.objects.create(
                product=product, branch=cls.branch, quantity_on_hand=10, reorder_level=1,
            )

    def _names(self, response):
        return [row['product'].product_name for row in response.context['rows']]

    def test_the_grid_is_one_page_of_cards(self):
        response = self.client.get(reverse('orders:catalog'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['rows']), CATALOG_PAGE_SIZE)
        self.assertEqual(response.context['page_obj'].paginator.count, 30)
        self.assertContains(response, 'Showing 1–12 of 30 products')

    def test_every_page_together_is_the_whole_catalog_once(self):
        seen = []
        for number in (1, 2, 3):
            seen += self._names(self.client.get(reverse('orders:catalog'), {'page': number}))
        self.assertEqual(len(seen), 30)
        self.assertEqual(len(set(seen)), 30)

    def test_the_summary_counts_every_match_not_just_the_cards_shown(self):
        response = self.client.get(reverse('orders:catalog'), {'page': 3})
        self.assertEqual(len(response.context['rows']), 6)
        self.assertContains(response, '<span id="result-count">30</span>')

    def test_the_sort_order_holds_across_pages(self):
        first = self._names(self.client.get(reverse('orders:catalog'), {'sort': 'name', 'page': 1}))
        second = self._names(self.client.get(reverse('orders:catalog'), {'sort': 'name', 'page': 2}))
        self.assertEqual(first + second, sorted(first + second))

    def test_search_filters_before_paging(self):
        response = self.client.get(reverse('orders:catalog'), {'q': 'Item 07'})
        self.assertEqual(self._names(response), ['Catalog Item 07'])
        self.assertNotContains(response, 'gv-pager-pages')   # one page, no buttons

    def test_a_page_past_the_end_opens_the_last_page(self):
        response = self.client.get(reverse('orders:catalog'), {'page': 500})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 3)

    def test_the_live_search_response_carries_a_pager_for_its_own_results(self):
        response = self.client.get(
            reverse('orders:catalog'), {'q': 'Catalog'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        data = response.json()
        self.assertIn('pager_html', data)
        self.assertIn('Showing 1–12 of 30 products', data['pager_html'])
        self.assertIn('q=Catalog', data['pager_html'])          # links keep the search
        self.assertIn('>30</span>', data['summary_html'])


class MyOrdersPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='mo-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='My Orders Branch', location='Baliuag', owner=cls.owner
        )
        cls.customer = User.objects.create_user(
            username='mo-customer', password='pw', role=User.Role.CUSTOMER
        )
        for i in range(23):
            SalesTransaction.objects.create(
                branch=cls.branch, customer=cls.customer,
                transaction_type=SalesTransaction.TransactionType.ONLINE,
                order_status=(
                    SalesTransaction.OrderStatus.COMPLETED if i % 2
                    else SalesTransaction.OrderStatus.PENDING
                ),
                total_amount=Decimal('50.00'),
            )

    def setUp(self):
        self.client.force_login(self.customer)

    def test_ten_orders_a_page(self):
        response = self.client.get(reverse('orders:my_orders'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['orders']), 10)
        self.assertEqual(response.context['page_obj'].paginator.count, 23)

    def test_the_last_page_has_the_remainder(self):
        response = self.client.get(reverse('orders:my_orders'), {'page': 3})
        self.assertEqual(len(response.context['orders']), 3)

    def test_a_tab_filter_survives_paging(self):
        response = self.client.get(reverse('orders:my_orders'), {'status': 'completed'})
        self.assertEqual(response.context['page_obj'].paginator.count, 11)
        self.assertContains(response, 'status=completed&amp;page=2')

    def test_another_customers_orders_never_appear(self):
        other = User.objects.create_user(username='mo-other', password='pw', role=User.Role.CUSTOMER)
        self.client.force_login(other)
        response = self.client.get(reverse('orders:my_orders'))
        self.assertEqual(response.context['page_obj'].paginator.count, 0)

    def test_the_pager_labels_follow_the_customers_language(self):
        self.customer.language = User.Language.FILIPINO
        self.customer.save()
        response = self.client.get(reverse('orders:my_orders'))
        self.assertContains(response, 'Susunod')
        self.assertContains(response, 'Ipinapakita')
