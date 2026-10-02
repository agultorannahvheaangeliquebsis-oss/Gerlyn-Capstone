from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from core.module_permissions import (
    ADD,
    DELETE,
    EDIT,
    BILLING,
    DELIVERIES,
    PROCUREMENT,
    PRODUCTS,
    REPORTS,
    VIEW,
    default_module_actions,
    required_action_for_request,
    role_permission_defaults,
)
from inventory.models import Product


class ModuleActionRouteTests(SimpleTestCase):
    def test_rest_routes_resolve_action_from_http_method(self):
        self.assertEqual(required_action_for_request('api-product-list', 'GET'), VIEW)
        self.assertEqual(required_action_for_request('api-product-list', 'POST'), ADD)
        self.assertEqual(required_action_for_request('api-product-detail', 'PATCH'), EDIT)
        self.assertEqual(required_action_for_request('api-product-detail', 'DELETE'), DELETE)

    def test_previously_ungated_writes_are_registered(self):
        self.assertEqual(required_action_for_request('product_add', 'POST'), ADD)
        self.assertEqual(required_action_for_request('product_edit', 'POST'), EDIT)
        self.assertEqual(required_action_for_request('product_delete', 'POST'), DELETE)
        self.assertEqual(required_action_for_request('procurement_add', 'POST'), ADD)
        self.assertEqual(required_action_for_request('delivery_scan', 'POST'), EDIT)

    def test_role_defaults_match_the_permission_matrix(self):
        manager = role_permission_defaults(User.Role.BRANCH_MANAGER)
        staff = role_permission_defaults(User.Role.STAFF)
        supplier = role_permission_defaults(User.Role.SUPPLIER)

        self.assertIn(PRODUCTS, manager['allowed_modules'])
        self.assertNotIn(ADD, manager['module_actions'][PRODUCTS])
        self.assertIn(ADD, manager['module_actions'][PROCUREMENT])
        self.assertIn(BILLING, staff['allowed_modules'])
        self.assertIn(EDIT, staff['module_actions'][BILLING])
        self.assertNotIn(REPORTS, staff['allowed_modules'])
        self.assertEqual(
            supplier['allowed_modules'], [PROCUREMENT, DELIVERIES]
        )


class ConfigurableModulePermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='permission-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Permission Branch', location='Baliuag', owner=self.owner
        )
        self.manager = User.objects.create_user(
            username='permission-manager', password='pw',
            role=User.Role.BRANCH_MANAGER,
        )
        self.manager.manager_profile.branch = self.branch
        self.manager.manager_profile.save(update_fields=['branch'])
        self.manager.allowed_modules = [PRODUCTS]
        self.manager.module_actions = {
            code: [] for code in default_module_actions()
        }
        self.manager.save(update_fields=['allowed_modules', 'module_actions'])

    def _login_manager(self):
        self.client.force_login(self.manager)

    def _product_payload(self, name='Permission Product'):
        return {
            'product_name': name,
            'unit_of_measure': Product.UnitOfMeasure.PIECE,
            'unit_price': '10.00',
        }

    def _grant(self, *actions, view=True):
        self.manager.allowed_modules = [PRODUCTS] if view else []
        module_actions = {code: [] for code in default_module_actions()}
        module_actions[PRODUCTS] = list(actions)
        self.manager.module_actions = module_actions
        self.manager.save(update_fields=['allowed_modules', 'module_actions'])

    def test_view_only_can_open_module_but_cannot_add_directly(self):
        self._login_manager()
        list_response = self.client.get(reverse('product_list'))
        self.assertEqual(list_response.status_code, 200)
        self.assertNotContains(
            list_response, f'href="{reverse("product_add")}"'
        )

        response = self.client.post(
            reverse('product_add'), self._product_payload('Blocked Add')
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Product.objects.filter(product_name='Blocked Add').exists())

    def test_action_grant_controls_the_matching_visible_control(self):
        self._login_manager()
        self._grant(ADD)

        response = self.client.get(reverse('product_list'))

        self.assertContains(response, f'href="{reverse("product_add")}"')
        self.assertNotContains(response, 'title="Edit"')
        self.assertNotContains(response, 'title="Delete"')

    def test_add_edit_and_delete_are_independently_enforced(self):
        self._login_manager()
        self._grant(ADD)
        response = self.client.post(
            reverse('product_add'), self._product_payload('Created Product')
        )
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(product_name='Created Product')

        response = self.client.post(
            reverse('product_edit', args=[product.pk]),
            self._product_payload('Blocked Edit'),
        )
        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.product_name, 'Created Product')

        response = self.client.post(reverse('product_delete', args=[product.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

        self._grant(ADD, EDIT, DELETE)
        self.client.post(
            reverse('product_edit', args=[product.pk]),
            self._product_payload('Allowed Edit'),
        )
        product.refresh_from_db()
        self.assertEqual(product.product_name, 'Allowed Edit')

        self.client.post(reverse('product_delete', args=[product.pk]))
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_disabled_module_blocks_direct_write_even_with_stale_action_grants(self):
        self._login_manager()
        self._grant(ADD, EDIT, DELETE, view=False)

        self.assertEqual(self.client.get(reverse('product_list')).status_code, 302)
        self.client.post(reverse('product_add'), self._product_payload('No View'))
        self.assertFalse(Product.objects.filter(product_name='No View').exists())

    def test_malformed_action_data_fails_closed(self):
        self._login_manager()
        self.manager.module_actions = 'add'
        self.manager.save(update_fields=['module_actions'])

        self.client.post(
            reverse('product_add'), self._product_payload('Malformed Grant')
        )

        self.assertFalse(
            Product.objects.filter(product_name='Malformed Grant').exists()
        )

    def test_api_uses_method_specific_permission(self):
        self._login_manager()
        self.assertEqual(self.client.get('/api/products/').status_code, 200)
        response = self.client.post(
            '/api/products/',
            data={
                'productName': 'API Blocked',
                'unitOfMeasure': Product.UnitOfMeasure.PIECE,
                'unitPrice': '12.00',
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Product.objects.filter(product_name='API Blocked').exists())

    def test_owner_can_save_detailed_permission_matrix(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('accounts:user_edit', args=[self.manager.pk]),
            {
                'username': self.manager.username,
                'role': User.Role.BRANCH_MANAGER,
                'branch': self.branch.pk,
                'allowed_modules': [PRODUCTS],
                'module_actions': [f'{PRODUCTS}:{ADD}', f'{PRODUCTS}:{EDIT}'],
            },
        )

        self.assertRedirects(response, reverse('accounts:user_list'))
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.allowed_modules, [PRODUCTS])
        self.assertEqual(self.manager.module_actions[PRODUCTS], [ADD, EDIT])
        self.assertNotIn(DELETE, self.manager.module_actions[PRODUCTS])

    def test_user_edit_page_renders_the_detailed_matrix(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('accounts:user_edit', args=[self.manager.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Detailed Permissions')
        self.assertContains(response, f'value="{PRODUCTS}"')
        self.assertContains(response, f'value="{PRODUCTS}:{ADD}"')
        self.assertContains(response, f'value="{PRODUCTS}:{EDIT}"')
        self.assertContains(response, f'value="{PRODUCTS}:{DELETE}"')
        # Reports has no create operation, so the matrix shows N/A rather
        # than a checkbox that could never authorize anything.
        self.assertNotContains(response, 'value="reports:add"')
        self.assertContains(response, 'id="role-permission-defaults"')
        self.assertNotContains(response, 'permission.disabled')

    def test_actions_for_an_unviewable_module_are_discarded(self):
        self.client.force_login(self.owner)
        self.client.post(
            reverse('accounts:user_edit', args=[self.manager.pk]),
            {
                'username': self.manager.username,
                'role': User.Role.BRANCH_MANAGER,
                'branch': self.branch.pk,
                'module_actions': [f'{PRODUCTS}:{ADD}'],
            },
        )

        self.manager.refresh_from_db()
        self.assertEqual(self.manager.allowed_modules, [])
        self.assertEqual(self.manager.module_actions[PRODUCTS], [])

    def test_role_change_without_browser_script_resets_to_role_defaults(self):
        staff_defaults = role_permission_defaults(User.Role.STAFF)
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('accounts:user_edit', args=[self.manager.pk]),
            {
                'username': self.manager.username,
                'role': User.Role.STAFF,
                'branch': self.branch.pk,
            },
        )

        self.assertRedirects(response, reverse('accounts:user_list'))
        self.manager.refresh_from_db()
        self.assertEqual(
            self.manager.allowed_modules, staff_defaults['allowed_modules']
        )
        self.assertEqual(
            self.manager.module_actions, staff_defaults['module_actions']
        )
