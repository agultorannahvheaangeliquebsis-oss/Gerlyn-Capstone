"""Every paginated page: it still renders at every ?page= an address bar can
produce, and the ones that are easy to seed really do cut their rows into
pages."""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import Notification, SupplierProfile, User
from billing.models import BillingRecord
from branches.models import Branch
from inventory.models import ProductCategory
from sales.models import SalesTransaction

# (url name, key of the Page in the template context)
PAGED_LISTS = [
    ('product_list', 'page_obj'),
    ('category_list', 'page_obj'),
    ('inventory_list', 'page_obj'),
    ('warehouse_stock_list', 'page_obj'),
    ('transfer_list', 'page_obj'),
    ('opening_stock_import_history', 'page_obj'),
    ('procurement_list', 'page_obj'),
    ('delivery_list', 'page_obj'),
    ('supplier_list', 'page_obj'),
    ('supplier_return_list', 'page_obj'),
    ('transaction_list', 'page_obj'),
    ('pos_sales_import_history', 'page_obj'),
    ('billing_list', 'page_obj'),
    ('accounts:user_list', 'page_obj'),
    ('accounts:customer_list', 'page_obj'),
    ('sales_report', 'page_obj'),
    ('billing_report', 'page_obj'),
    ('inventory_report', 'page_obj'),
    ('procurement_report', 'page_obj'),
]


class EveryPagedListRendersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='pages-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            owner=cls.owner, branch_name='Pages Branch', location='Main'
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def test_each_list_renders_at_any_page_parameter(self):
        for name, key in PAGED_LISTS:
            for page in ('1', '2', '0', '-3', 'abc', '99999', '', '1.5'):
                with self.subTest(url=name, page=page):
                    response = self.client.get(reverse(name), {'page': page})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(key, response.context)
                    self.assertGreaterEqual(response.context[key].number, 1)

    def test_a_junk_page_on_an_empty_list_shows_the_empty_state_not_a_pager(self):
        response = self.client.get(reverse('category_list'), {'page': '7'})
        self.assertNotContains(response, 'gv-pager')

    def test_every_withdrawal_tab_pages_its_own_list(self):
        for view in ('transfers', 'writeoffs', 'history'):
            for page in ('1', '2', 'abc', '0'):
                with self.subTest(view=view, page=page):
                    response = self.client.get(
                        reverse('transfer_list'), {'view': view, 'page': page},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context['active_view'], view)
                    self.assertEqual(response.context['page_obj'].number, 1)

    def test_a_write_off_validation_error_redisplays_the_write_offs_tab_with_its_own_pager(self):
        # The re-render used to flip active_view AFTER the lists were built,
        # leaving the pager describing the wrong tab.
        response = self.client.post(reverse('transfer_create'), {
            'branch_id': self.branch.pk, 'product_id': [''], 'quantity_requested': [''],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_view'], 'writeoffs')
        self.assertTrue(response.context['show_create_modal'])

    def test_the_notification_center_and_alerts_center_render(self):
        for name in ('notification_center', 'alerts_center'):
            with self.subTest(url=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class SeededListsArePagedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='seeded-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            owner=cls.owner, branch_name='Seeded Branch', location='Main'
        )
        for i in range(32):
            ProductCategory.objects.create(category_name=f'Category {i:02d}')
            User.objects.create_user(
                username=f'staff-{i:02d}', password='pw', role=User.Role.STAFF
            )
            User.objects.create_user(
                username=f'cust-{i:02d}', password='pw', role=User.Role.CUSTOMER
            )

    def setUp(self):
        self.client.force_login(self.owner)

    def test_categories(self):
        response = self.client.get(reverse('category_list'))
        self.assertEqual(response.context['page_obj'].paginator.count, 32)
        self.assertEqual(len(response.context['categories']), 15)
        self.assertContains(response, 'Showing 1–15 of 32 categories')

    def test_users_are_paged_and_the_owner_row_is_counted_once(self):
        response = self.client.get(reverse('accounts:user_list'), {'page': 3})
        page = response.context['page_obj']
        self.assertEqual(page.number, 3)
        # 32 staff + the owner, 15 a page
        self.assertEqual(page.paginator.count, 33)
        self.assertEqual(len(response.context['rows']), 3)

    def test_user_search_runs_on_the_server_across_every_page(self):
        response = self.client.get(reverse('accounts:user_list'), {'q': 'staff-31'})
        self.assertEqual([r['user'].username for r in response.context['rows']], ['staff-31'])

    def test_customers(self):
        response = self.client.get(reverse('accounts:customer_list'))
        self.assertEqual(response.context['page_obj'].paginator.count, 32)
        self.assertEqual(len(response.context['rows']), 15)

    def test_the_pager_markup_has_previous_next_and_numbered_pages(self):
        response = self.client.get(reverse('accounts:customer_list'), {'page': 2})
        html = response.content.decode()
        self.assertIn('rel="prev"', html)
        self.assertIn('rel="next"', html)
        self.assertIn('aria-current="page"', html)
        self.assertIn('aria-label="Page 3"', html)


class OrderAndBillingListsArePagedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='orders-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            owner=cls.owner, branch_name='Orders Branch', location='Main'
        )
        for i in range(34):
            SalesTransaction.objects.create(
                branch=cls.branch, recorded_by=cls.owner,
                transaction_type=SalesTransaction.TransactionType.PHYSICAL,
                mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
                order_status=SalesTransaction.OrderStatus.COMPLETED,
                total_amount=Decimal('100.00'),
            )

    def setUp(self):
        self.client.force_login(self.owner)

    def test_order_console_pages_but_counts_stay_for_the_whole_list(self):
        response = self.client.get(reverse('transaction_list'), {'page': 2})
        self.assertEqual(response.context['page_obj'].paginator.count, 34)
        self.assertEqual(len(response.context['transactions']), 15)
        # The tab count describes every order, not just this page's rows.
        self.assertEqual(response.context['counts']['completed'], 34)
        self.assertEqual(response.context['counts']['all'], 34)

    def test_a_status_tab_link_starts_again_on_page_one(self):
        response = self.client.get(reverse('transaction_list'), {'page': 3})
        # Tab hrefs are built from the filters only — never carry `page`.
        self.assertNotContains(response, 'status=Completed&amp;page=')

    def test_billing_records_page_and_keep_the_status_filter_on_every_link(self):
        for txn in SalesTransaction.objects.all():
            BillingRecord.objects.update_or_create(
                transaction=txn, defaults={'amount_due': txn.total_amount},
            )
        response = self.client.get(reverse('billing_list'), {'status': 'Pending'})
        page = response.context['page_obj']
        self.assertEqual(page.paginator.count, 34)
        self.assertContains(response, 'status=Pending&amp;page=2')

    def test_sales_report_pages_its_table_but_totals_cover_everything(self):
        response = self.client.get(reverse('sales_report'), {'range': 'all'})
        self.assertEqual(response.context['page_obj'].paginator.count, 34)
        self.assertEqual(len(response.context['transactions']), 15)


class NotificationPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(
            username='notify-cust', password='pw', role=User.Role.CUSTOMER
        )
        for i in range(23):
            Notification.objects.create(user=cls.customer, title=f'Note {i}', message='m')

    def test_the_customer_notifications_page_is_paged(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse('accounts:notifications'))
        self.assertEqual(response.status_code, 200)
        page = response.context['page_obj']
        self.assertEqual((page.paginator.count, len(response.context['notifications'])), (23, 10))
        # The unread badge counts all 23, not the 10 shown.
        self.assertEqual(response.context['unread_count'], 23)
