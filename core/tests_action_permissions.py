import importlib
from datetime import date

from django.apps import apps
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import PermissionChangeLog, User
from branches.models import Branch
from core.module_permissions import (
    ACTIONS, ALERTS, BILLING, DELIVERIES, FLOOR_STOCK,
    ALL_MODULE_CODES,
    PROCUREMENT, PRODUCTS, REPORTS,
    ROLE_ACTION_CEILINGS,
    SALES_POS,
    WAREHOUSE_STOCK, WITHDRAWALS,
    default_action_permissions,
    user_can,
)
from inventory.models import Product
from procurement.models import Procurement
from sales.models import SalesTransaction


class ActionPermissionMatrixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user('perm-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            owner=cls.owner, branch_name='Permission Branch', location='Baliuag'
        )
        cls.other_branch = Branch.objects.create(
            owner=cls.owner, branch_name='Other Branch', location='Baliuag'
        )
        cls.manager = User.objects.create_user(
            'perm-manager', password='pw', role=User.Role.BRANCH_MANAGER
        )
        cls.manager.manager_profile.branch = cls.branch
        cls.manager.manager_profile.save(update_fields=['branch'])
        cls.staff = User.objects.create_user('perm-staff', password='pw', role=User.Role.STAFF)
        cls.staff.staff_profile.branch = cls.branch
        cls.staff.staff_profile.save(update_fields=['branch'])
        cls.supplier = User.objects.create_user(
            'perm-supplier', password='pw', role=User.Role.SUPPLIER
        )
        cls.customer = User.objects.create_user(
            'perm-customer', password='pw', role=User.Role.CUSTOMER
        )

        for user in (cls.manager, cls.staff):
            user.action_permissions = default_action_permissions(user.role, ALL_MODULE_CODES)
            user.allowed_modules = list(ALL_MODULE_CODES)
            user.save(update_fields=['action_permissions', 'allowed_modules'])

    def test_every_module_action_obeys_owner_manager_staff_role_matrix(self):
        for module in ALL_MODULE_CODES:
            for action in ACTIONS:
                with self.subTest(role='owner', module=module, action=action):
                    self.assertTrue(user_can(self.owner, module, action))
                for user in (self.manager, self.staff):
                    expected = action in ROLE_ACTION_CEILINGS[user.role].get(module, set())
                    with self.subTest(role=user.role, module=module, action=action):
                        self.assertEqual(user_can(user, module, action), expected)

    def test_supplier_and_customer_stay_outside_internal_grants(self):
        for user in (self.supplier, self.customer):
            user.action_permissions = {
                module: list(ACTIONS) for module in ALL_MODULE_CODES
            }
            for module in ALL_MODULE_CODES:
                for action in ACTIONS:
                    with self.subTest(role=user.role, module=module, action=action):
                        self.assertFalse(user_can(user, module, action))

    def test_stored_grant_cannot_bypass_role_ceiling(self):
        self.staff.action_permissions = {'products': list(ACTIONS)}
        self.staff.save(update_fields=['action_permissions'])
        self.assertTrue(user_can(self.staff, 'products', 'view'))
        self.assertFalse(user_can(self.staff, 'products', 'add'))
        self.assertFalse(user_can(self.staff, 'products', 'edit'))
        self.assertFalse(user_can(self.staff, 'products', 'delete'))

    def test_unknown_stored_matrix_keys_do_not_expand_access(self):
        self.manager.allowed_modules = ['products']
        self.manager.action_permissions = {
            'products': ['view'],
            'not-a-module': list(ACTIONS),
        }
        self.manager.save(update_fields=['allowed_modules', 'action_permissions'])

        self.assertTrue(user_can(self.manager, 'products', 'view'))
        self.assertFalse(user_can(self.manager, 'products', 'add'))
        self.assertFalse(user_can(self.manager, SALES_POS, 'view'))

    def test_unknown_module_or_action_fails_closed(self):
        self.assertFalse(user_can(self.manager, 'not-a-module', 'view'))
        self.assertFalse(user_can(self.manager, SALES_POS, 'approve'))

    def test_unassigned_restricted_account_fails_closed(self):
        self.staff.staff_profile.branch = None
        self.staff.staff_profile.save(update_fields=['branch'])
        for module in ALL_MODULE_CODES:
            for action in ACTIONS:
                self.assertFalse(user_can(self.staff, module, action))

    def test_cross_branch_access_fails_closed(self):
        self.assertTrue(user_can(self.manager, SALES_POS, 'view', self.branch))
        self.assertFalse(user_can(self.manager, SALES_POS, 'view', self.other_branch))


class PermissionRouteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user('route-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            owner=cls.owner, branch_name='Route Branch', location='Baliuag'
        )
        cls.other_branch = Branch.objects.create(
            owner=cls.owner, branch_name='Foreign Route Branch', location='Baliuag'
        )
        cls.staff = User.objects.create_user('route-staff', password='pw', role=User.Role.STAFF)
        cls.staff.staff_profile.branch = cls.branch
        cls.staff.staff_profile.save(update_fields=['branch'])
        cls.staff.action_permissions = {SALES_POS: ['view']}
        cls.staff.allowed_modules = [SALES_POS]
        cls.staff.save(update_fields=['action_permissions', 'allowed_modules'])

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)

    def test_direct_template_url_checks_action_grant(self):
        response = self.client.get(reverse('transaction_list'))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(reverse('transaction_add'))
        self.assertEqual(response.status_code, 302)

    def test_api_get_and_post_on_same_url_use_different_actions(self):
        self.assertEqual(self.client.get('/api/sales/').status_code, 200)
        self.assertEqual(
            self.client.post('/api/sales/', data={}, content_type='application/json').status_code,
            403,
        )

    def test_unknown_method_on_protected_api_route_fails_closed(self):
        self.assertEqual(self.client.generic('TRACE', '/api/sales/').status_code, 403)

    def test_api_cross_branch_record_is_not_accessible(self):
        transaction = SalesTransaction.objects.create(
            branch=self.other_branch,
            total_amount='10.00',
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        self.assertEqual(self.client.get(f'/api/sales/{transaction.pk}/').status_code, 404)

    def test_action_button_is_hidden_without_add_grant(self):
        response = self.client.get(reverse('transaction_list'))
        self.assertNotContains(response, 'New Walk-in Sale')

    def test_each_module_view_grant_is_enforced_on_direct_urls(self):
        routes = {
            PRODUCTS: 'product_list',
            FLOOR_STOCK: 'inventory_list',
            WAREHOUSE_STOCK: 'warehouse_stock_list',
            WITHDRAWALS: 'transfer_list',
            ALERTS: 'alerts_center',
            SALES_POS: 'transaction_list',
            BILLING: 'billing_list',
            PROCUREMENT: 'procurement_list',
            DELIVERIES: 'delivery_list',
            REPORTS: 'reports_dashboard',
        }
        for module, url_name in routes.items():
            self.staff.action_permissions = {}
            self.staff.allowed_modules = []
            self.staff.save(update_fields=['action_permissions', 'allowed_modules'])
            with self.subTest(module=module, outcome='denied'):
                self.assertEqual(self.client.get(reverse(url_name)).status_code, 302)

            self.staff.action_permissions = {module: ['view']}
            self.staff.allowed_modules = [module]
            self.staff.save(update_fields=['action_permissions', 'allowed_modules'])
            with self.subTest(module=module, outcome='allowed'):
                self.assertEqual(self.client.get(reverse(url_name)).status_code, 200)

    def test_each_internal_api_view_grant_is_enforced(self):
        routes = {
            FLOOR_STOCK: '/api/inventory/',
            WAREHOUSE_STOCK: '/api/warehouse-stock/',
            WITHDRAWALS: '/api/stock-transfers/',
            ALERTS: '/api/batches/',
            SALES_POS: '/api/sales/',
            BILLING: '/api/billing/',
            PROCUREMENT: '/api/procurement/',
            REPORTS: '/api/reports/dashboard-summary/',
        }
        for module, url in routes.items():
            self.staff.action_permissions = {}
            self.staff.allowed_modules = []
            self.staff.save(update_fields=['action_permissions', 'allowed_modules'])
            with self.subTest(module=module, outcome='denied'):
                self.assertEqual(self.client.get(url).status_code, 403)

            self.staff.action_permissions = {module: ['view']}
            self.staff.allowed_modules = [module]
            self.staff.save(update_fields=['action_permissions', 'allowed_modules'])
            with self.subTest(module=module, outcome='allowed'):
                self.assertEqual(self.client.get(url).status_code, 200)


class PermissionAdministrationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('admin-owner', password='pw', role=User.Role.OWNER)
        self.branch = Branch.objects.create(
            owner=self.owner, branch_name='Admin Branch', location='Baliuag'
        )
        self.manager = User.objects.create_user(
            'admin-manager', password='pw', role=User.Role.BRANCH_MANAGER,
            email='manager@example.com',
        )
        self.manager.manager_profile.branch = self.branch
        self.manager.manager_profile.save(update_fields=['branch'])
        self.client.force_login(self.owner)

    def test_owner_change_is_ceiling_filtered_and_audited(self):
        response = self.client.post(reverse('accounts:user_edit', args=[self.manager.pk]), {
            'username': self.manager.username,
            'email': self.manager.email,
            'role': User.Role.BRANCH_MANAGER,
            'branch': self.branch.pk,
            'permission__products__view': '1',
            'permission__floor_stock__add': '1',  # outside Manager ceiling
        })
        self.assertEqual(response.status_code, 302)
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.action_permissions, {'products': ['view']})
        log = PermissionChangeLog.objects.get(target_user=self.manager)
        self.assertEqual(log.changed_by, self.owner)
        self.assertEqual(log.new_permissions, {'products': ['view']})
        self.assertIsNotNone(log.changed_at)

    def test_non_owner_cannot_manage_grants(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('accounts:user_edit', args=[self.manager.pk]), {})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PermissionChangeLog.objects.exists())

    def test_owner_can_set_action_matrix_when_inviting_user(self):
        response = self.client.post(reverse('accounts:user_add'), {
            'email': 'matrix-invite@example.com',
            'role': User.Role.STAFF,
            'branch': self.branch.pk,
            'permissions_matrix': '1',
            'permission__sales_pos__view': '1',
            'permission__sales_pos__add': '1',
        })

        self.assertEqual(response.status_code, 302)
        invited = User.objects.get(email='matrix-invite@example.com')
        self.assertEqual(invited.action_permissions, {'sales_pos': ['view', 'add']})
        self.assertEqual(invited.allowed_modules, ['sales_pos'])

    def test_backfill_preserves_legacy_module_access_within_role_ceiling(self):
        self.manager.allowed_modules = ['products', 'reports']
        self.manager.action_permissions = None
        self.manager.save(update_fields=['allowed_modules', 'action_permissions'])
        migration = importlib.import_module(
            'accounts.migrations.0022_user_action_permissions_permissionchangelog'
        )
        migration.backfill_action_permissions(apps, None)
        self.manager.refresh_from_db()
        self.assertEqual(
            self.manager.action_permissions,
            {
                'products': ['view', 'add', 'edit', 'delete'],
                'reports': ['view'],
            },
        )


class TraceabilityDeletionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            'trace-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            owner=self.owner, branch_name='Traceability Branch', location='Baliuag'
        )
        self.client.force_login(self.owner)

    def test_product_delete_deactivates_without_removing_row(self):
        product = Product.objects.create(
            sku_code='TRACE-PRODUCT', product_name='Historical Product', unit_price='20.00'
        )

        response = self.client.post(reverse('product_delete', args=[product.pk]))

        self.assertRedirects(response, reverse('product_list'))
        product.refresh_from_db()
        self.assertFalse(product.is_active)
        self.assertEqual(
            self.client.get(reverse('orders:product_detail', args=[product.pk])).status_code,
            404,
        )
        self.assertEqual(self.client.get(f'/api/products/{product.pk}/').status_code, 404)

    def test_supplier_delete_deactivates_without_removing_row(self):
        supplier = User.objects.create_user(
            'trace-supplier', password='pw', role=User.Role.SUPPLIER
        )

        response = self.client.post(reverse('supplier_delete', args=[supplier.pk]))

        self.assertRedirects(response, reverse('supplier_list'))
        supplier.refresh_from_db()
        self.assertFalse(supplier.is_active)

    def test_procurement_api_delete_cancels_without_removing_row(self):
        supplier = User.objects.create_user(
            'po-supplier', password='pw', role=User.Role.SUPPLIER
        )
        procurement = Procurement.objects.create(
            branch=self.branch,
            supplier=supplier,
            owner=self.owner,
            order_date=date.today(),
            status=Procurement.Status.DRAFT,
        )

        response = self.client.delete(
            reverse('api-procurement-detail', args=[procurement.pk])
        )

        self.assertEqual(response.status_code, 204)
        procurement.refresh_from_db()
        self.assertEqual(procurement.status, Procurement.Status.CANCELLED)
