"""P0 regression: branch scoping must fail CLOSED for an unassigned account.

`core.scoping.branch_for_user()` returns None for the Owner (sees every
branch) AND for a Manager/Staff whose profile branch is NULL. That second
state is reachable in normal operation — `BranchManagerProfile.branch` and
`StaffProfile.branch` are both `on_delete=SET_NULL`, so deleting a branch
nulls out its manager/staff.

~17 call sites used `user_branch = branch_for_user(u); if user_branch:
qs.filter(branch=user_branch)`, which cannot tell the two apart and handed
an unassigned account every branch's data. The worst was
`reports.views.export_csv` — a bulk download of every branch's sales,
inventory, procurement and billing in one request.

These assert both directions: an unassigned account sees NOTHING, and an
assigned one still sees its own branch (guarding against over-correcting
into fail-closed-for-everyone).
"""
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from core.scoping import scope_queryset
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


class UnassignedAccountFailsClosedTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    def login(self, username):
        client = Client()
        client.force_login(User.objects.get(username=username))
        return client

    def _unassign(self, username):
        """Put an account into the reachable "role set, no branch" state."""
        user = User.objects.get(username=username)
        profile = user.get_role_profile()
        profile.branch = None
        profile.save(update_fields=['branch'])
        return user

    # -- the helper itself ----------------------------------------------

    def test_scope_queryset_returns_nothing_for_an_unassigned_manager(self):
        self._unassign('carlo')
        carlo = User.objects.get(username='carlo')
        self.assertEqual(
            scope_queryset(SalesTransaction.objects.all(), carlo).count(), 0,
            'an unassigned manager must fail closed, not inherit Owner scope',
        )

    def test_assigned_manager_still_sees_their_own_branch(self):
        carlo = User.objects.get(username='carlo')
        self.assertGreater(
            scope_queryset(SalesTransaction.objects.all(), carlo).count(), 0,
            'the fix must not lock out a correctly-assigned manager',
        )

    # -- reports: the CSV bulk export (the worst site) ----------------

    def test_unassigned_manager_sales_csv_export_is_header_only(self):
        self._unassign('carlo')
        body = self.login('carlo').get(
            reverse('export_csv', args=['sales'])
        ).content
        self.assertEqual(body.count(b'\n'), 1, body[:400])

    def test_unassigned_manager_inventory_csv_export_is_header_only(self):
        self._unassign('carlo')
        body = self.login('carlo').get(
            reverse('export_csv', args=['inventory'])
        ).content
        self.assertEqual(body.count(b'\n'), 1, body[:400])

    def test_assigned_manager_sales_csv_export_still_has_rows(self):
        body = self.login('carlo').get(
            reverse('export_csv', args=['sales'])
        ).content
        self.assertGreater(body.count(b'\n'), 1)

    # -- reports: the HTML sales report ---------------------------

    def test_unassigned_manager_sales_report_page_lists_nothing(self):
        self._unassign('carlo')
        response = self.login('carlo').get(reverse('sales_report'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['transactions']), [])

    # -- reports API --------------------------------------------

    def test_unassigned_manager_sales_report_api_is_empty(self):
        self._unassign('carlo')
        response = self.login('carlo').get('/api/reports/sales-report/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_unassigned_manager_dashboard_summary_api_totals_are_zero(self):
        self._unassign('carlo')
        response = self.login('carlo').get('/api/reports/dashboard-summary/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['totalTransactions'], 0)

    # -- inventory API -----------------------------------------

    def test_unassigned_manager_inventory_api_is_empty(self):
        self._unassign('carlo')
        response = self.login('carlo').get('/api/inventory/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_assigned_manager_inventory_api_still_has_rows(self):
        response = self.login('carlo').get('/api/inventory/')
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.json()), 0)

    # -- inventory views -------------------------------------

    def test_unassigned_manager_floor_stock_list_shows_no_rows(self):
        self._unassign('carlo')
        response = self.login('carlo').get(reverse('inventory_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['rows']), [])

    def test_unassigned_manager_cannot_adjust_stock_at_a_named_branch(self):
        """stock_adjustment_add is a WRITE — an unassigned Branch Manager used
        to be able to name any branch in the POST."""
        self._unassign('carlo')
        other = Branch.objects.exclude(branch_name='Tarcan').first()
        product = Product.objects.first()
        response = self.login('carlo').post(reverse('stock_adjustment_add'), {
            'product_id': str(product.pk),
            'branch_id': str(other.pk),
            'quantity': '5',
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Inventory.objects.filter(
                product=product, branch=other, quantity_on_hand=5
            ).exists()
        )


class UnassignedStaffPosTests(TestCase):
    """F-13 already refuses the POS for a branchless Staff/Manager; kept here
    so the two guards are read together."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    def test_unassigned_staff_cannot_record_a_sale_at_any_branch(self):
        user = User.objects.get(username='ana')
        profile = user.get_role_profile()
        profile.branch = None
        profile.save(update_fields=['branch'])

        other = Branch.objects.exclude(branch_name='Tarcan').first()
        product = Product.objects.first()
        before = SalesTransaction.objects.count()

        client = Client()
        client.force_login(user)
        client.post(reverse('transaction_add'), {
            'branch': str(other.pk),
            'mode_of_payment': 'Cash',
            'product_id': [str(product.pk)],
            'quantity': ['1'],
        })

        self.assertEqual(SalesTransaction.objects.count(), before)
