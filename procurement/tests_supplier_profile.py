from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import User, VerificationToken
from branches.models import Branch
from inventory.models import Product, ProductCategory
from procurement.models import SupplierProduct


class SupplierProfilePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='profile-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Main Branch', location='Baliwag', owner=cls.owner
        )
        cls.supplier = User.objects.create_user(
            username='acme-login',
            email='orders@acme.test',
            password='pw',
            role=User.Role.SUPPLIER,
            is_email_verified=True,
        )
        cls.supplier.supplier_profile.supplier_name = 'Acme Supply Co.'
        cls.supplier.supplier_profile.contact_person = 'Alicia Cruz'
        cls.supplier.supplier_profile.contact_number = '09170000001'
        cls.supplier.supplier_profile.address = '1 Market Road, Baliwag'
        cls.supplier.supplier_profile.supplier_category = 'Dry Goods'
        cls.supplier.supplier_profile.branch = cls.branch
        cls.supplier.supplier_profile.save()

        cls.other_supplier = User.objects.create_user(
            username='other-supplier',
            email='other@supplier.test',
            password='pw',
            role=User.Role.SUPPLIER,
        )
        cls.other_supplier.supplier_profile.supplier_name = 'Other Supplier'
        cls.other_supplier.supplier_profile.contact_person = 'Other Person'
        cls.other_supplier.supplier_profile.contact_number = '09170000002'
        cls.other_supplier.supplier_profile.address = 'Other Address'
        cls.other_supplier.supplier_profile.supplier_category = 'Frozen'
        cls.other_supplier.supplier_profile.save()

        cls.category = ProductCategory.objects.create(category_name='Beverages')
        cls.product = Product.objects.create(
            category=cls.category,
            sku_code='SKU-PROFILE-001',
            product_name='Orange Juice',
            unit_price='75.00',
        )
        cls.catalog_link = SupplierProduct.objects.create(
            supplier=cls.supplier,
            product=cls.product,
            supplier_sku='ACME-OJ-1L',
            lead_time_days=4,
            is_preferred=True,
        )

        cls.customer = User.objects.create_user(
            username='profile-customer', password='pw', role=User.Role.CUSTOMER
        )
        cls.staff = User.objects.create_user(
            username='profile-staff', password='pw', role=User.Role.STAFF
        )
        cls.manager = User.objects.create_user(
            username='profile-manager', password='pw', role=User.Role.BRANCH_MANAGER
        )

    def setUp(self):
        self.client.force_login(self.supplier)

    @staticmethod
    def valid_payload(**overrides):
        payload = {
            'supplier_name': 'Acme Supply Co.',
            'contact_person': 'Alicia Cruz',
            'email': 'orders@acme.test',
            'contact_number': '09170000001',
            'address': '1 Market Road, Baliwag',
        }
        payload.update(overrides)
        return payload

    def test_supplier_can_view_own_profile_and_read_only_catalog_summary(self):
        response = self.client.get(reverse('supplier_profile'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Acme Supply Co.')
        self.assertContains(response, 'Alicia Cruz')
        self.assertContains(response, 'orders@acme.test')
        self.assertContains(response, 'Dry Goods')
        self.assertContains(response, 'Beverages')
        self.assertContains(response, 'ACME-OJ-1L')
        self.assertContains(response, '4 days')
        self.assertContains(response, 'Active account')

    def test_supplier_can_edit_only_own_permitted_contact_fields(self):
        response = self.client.post(
            reverse('supplier_profile_edit'),
            self.valid_payload(
                supplier_name='Acme Wholesale',
                contact_person='Ana Reyes',
                contact_number='09171112222',
                address='25 New Business Avenue',
            ),
        )

        self.assertRedirects(response, reverse('supplier_profile'))
        self.supplier.supplier_profile.refresh_from_db()
        self.assertEqual(self.supplier.supplier_profile.supplier_name, 'Acme Wholesale')
        self.assertEqual(self.supplier.supplier_profile.contact_person, 'Ana Reyes')
        self.assertEqual(self.supplier.supplier_profile.contact_number, '09171112222')
        self.assertEqual(self.supplier.supplier_profile.address, '25 New Business Avenue')

    def test_supplier_cannot_select_or_open_another_suppliers_profile(self):
        response = self.client.get(
            reverse('supplier_profile'),
            {'supplier_id': self.other_supplier.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Acme Supply Co.')
        self.assertNotContains(response, 'Other Supplier')

        management_response = self.client.get(
            reverse('supplier_detail', kwargs={'pk': self.other_supplier.pk})
        )
        self.assertRedirects(management_response, reverse('accounts:home'))

    def test_forged_post_cannot_change_owner_controlled_fields_or_another_supplier(self):
        original_modules = self.supplier.allowed_modules
        response = self.client.post(
            reverse('supplier_profile_edit'),
            self.valid_payload(
                supplier_name='Allowed New Name',
                role=User.Role.OWNER,
                is_active='',
                is_email_verified='',
                branch='',
                supplier_category='Forged Category',
                allowed_modules=['reports'],
                supplier_id=self.other_supplier.pk,
                lead_time_days='99',
                supplier_sku='FORGED-SKU',
            ),
        )

        self.assertRedirects(response, reverse('supplier_profile'))
        self.supplier.refresh_from_db()
        self.supplier.supplier_profile.refresh_from_db()
        self.other_supplier.supplier_profile.refresh_from_db()
        self.catalog_link.refresh_from_db()
        self.assertEqual(self.supplier.role, User.Role.SUPPLIER)
        self.assertTrue(self.supplier.is_active)
        self.assertTrue(self.supplier.is_email_verified)
        self.assertEqual(self.supplier.allowed_modules, original_modules)
        self.assertEqual(self.supplier.supplier_profile.branch, self.branch)
        self.assertEqual(self.supplier.supplier_profile.supplier_category, 'Dry Goods')
        self.assertEqual(self.catalog_link.lead_time_days, 4)
        self.assertEqual(self.catalog_link.supplier_sku, 'ACME-OJ-1L')
        self.assertEqual(
            self.other_supplier.supplier_profile.supplier_name,
            'Other Supplier',
        )

    def test_supplier_cannot_use_owner_account_api_to_change_protected_fields(self):
        response = self.client.patch(
            f'/api/auth/users/{self.supplier.pk}/',
            data={
                'isActive': False,
                'branchId': None,
                'fullName': 'API Forged Name',
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 403)
        self.supplier.refresh_from_db()
        self.supplier.supplier_profile.refresh_from_db()
        self.assertTrue(self.supplier.is_active)
        self.assertEqual(self.supplier.supplier_profile.branch, self.branch)
        self.assertEqual(self.supplier.supplier_profile.supplier_name, 'Acme Supply Co.')

    def test_generic_account_profile_cannot_bypass_supplier_field_allowlist(self):
        view_response = self.client.get(reverse('accounts:profile'))
        self.assertRedirects(view_response, reverse('supplier_profile'))

        update_response = self.client.post(
            reverse('accounts:update_profile'),
            {
                'username': 'forged-login-name',
                'first_name': 'Forged',
                'last_name': 'Person',
                'email': self.supplier.email,
                'contact_number': '09999999999',
            },
        )

        self.assertRedirects(update_response, reverse('supplier_profile_edit'))
        self.supplier.refresh_from_db()
        self.supplier.supplier_profile.refresh_from_db()
        self.assertEqual(self.supplier.username, 'acme-login')
        self.assertEqual(self.supplier.first_name, '')
        self.assertEqual(self.supplier.last_name, '')
        self.assertEqual(self.supplier.supplier_profile.contact_number, '09170000001')

    def test_email_change_uses_pending_verification_workflow(self):
        response = self.client.post(
            reverse('supplier_profile_edit'),
            self.valid_payload(email='new-orders@acme.test'),
        )

        self.assertRedirects(response, reverse('supplier_profile'))
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.email, 'orders@acme.test')
        self.assertEqual(self.supplier.pending_email, 'new-orders@acme.test')
        self.assertEqual(len(mail.outbox), 1)
        token = VerificationToken.objects.get(
            user=self.supplier,
            purpose=VerificationToken.PURPOSE_EMAIL_CHANGE,
            is_used=False,
        )

        verify_response = self.client.get(
            reverse('accounts:verify_email_change', kwargs={'token': token.token})
        )
        self.assertRedirects(verify_response, reverse('supplier_profile'))
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.email, 'new-orders@acme.test')
        self.assertIsNone(self.supplier.pending_email)

    def test_validation_errors_keep_submitted_data_and_do_not_partially_save(self):
        response = self.client.post(
            reverse('supplier_profile_edit'),
            self.valid_payload(
                supplier_name='Typed But Unsaved Company',
                email='not-an-email',
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Typed But Unsaved Company')
        self.assertContains(response, 'Enter a valid email address.')
        self.supplier.supplier_profile.refresh_from_db()
        self.assertEqual(self.supplier.supplier_profile.supplier_name, 'Acme Supply Co.')

    def test_csrf_is_required_for_profile_update(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.supplier)

        response = csrf_client.post(
            reverse('supplier_profile_edit'), self.valid_payload()
        )

        self.assertEqual(response.status_code, 403)

    def test_customer_and_internal_roles_cannot_use_supplier_profile_routes(self):
        for user in (self.customer, self.staff, self.manager, self.owner):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                self.assertEqual(
                    self.client.get(reverse('supplier_profile')).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(
                        reverse('supplier_profile_edit'), self.valid_payload()
                    ).status_code,
                    403,
                )

    def test_anonymous_user_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(reverse('supplier_profile'))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('accounts:login')))

    def test_owner_can_view_supplier_information_in_management(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('supplier_detail', kwargs={'pk': self.supplier.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Acme Supply Co.')
        self.assertContains(response, 'Alicia Cruz')
        self.assertContains(response, 'orders@acme.test')
        self.assertContains(response, 'ACME-OJ-1L')
        self.assertContains(response, '4 days')
        self.assertContains(response, 'Active')
