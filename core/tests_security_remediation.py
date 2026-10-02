from datetime import date

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client, TestCase

from accounts.models import User
from billing.models import BillingRecord
from branches.models import Branch
from core.module_permissions import SALES_POS
from inventory.models import Product, Warehouse
from procurement.models import Procurement


class SecurityAuditRemediationTests(TestCase):
    """Regression coverage for SEC-01 through SEC-07."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    def login(self, username):
        client = Client()
        client.force_login(User.objects.get(username=username))
        return client

    def test_customer_cannot_enumerate_procurement_orders(self):
        self.assertTrue(Procurement.objects.exists())
        response = self.login('juan').get('/api/procurement/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_manager_cannot_create_procurement_for_another_branch(self):
        manager = User.objects.get(username='carlo')
        foreign = Branch.objects.exclude(
            pk=manager.manager_profile.branch_id
        ).first()
        supplier = User.objects.filter(role=User.Role.SUPPLIER).first()
        product = Product.objects.first()

        response = self.login('carlo').post(
            '/api/procurement/',
            data={
                'branchId': foreign.pk,
                'supplierId': supplier.pk,
                'orderDate': str(date.today()),
                'items': [{
                    'productId': product.pk,
                    'quantityOrdered': 1,
                    'costPrice': '10.00',
                }],
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('branchId', response.json())

    def test_revoked_module_blocks_its_api(self):
        staff = User.objects.get(username='ana')
        staff.allowed_modules = [
            code for code in staff.allowed_modules if code != SALES_POS
        ]
        staff.save(update_fields=['allowed_modules'])

        response = self.login('ana').get('/api/sales/')
        self.assertEqual(response.status_code, 403)

    def test_public_product_api_remains_public(self):
        self.assertEqual(Client().get('/api/products/').status_code, 200)

    def test_payment_proof_requires_record_authorization(self):
        record = BillingRecord.objects.filter(
            transaction__customer__username='juan'
        ).first()
        self.assertIsNotNone(record)
        record.proof_of_payment.save(
            'security-test-proof.jpg', ContentFile(b'proof'), save=True
        )
        try:
            protected_url = f'/billing/{record.pk}/payment-proof/'
            anonymous = Client().get(protected_url)
            self.assertEqual(anonymous.status_code, 302)
            self.assertEqual(self.login('century').get(protected_url).status_code, 404)

            authorized = self.login('juan').get(protected_url)
            self.assertEqual(authorized.status_code, 200)
            self.assertEqual(b''.join(authorized.streaming_content), b'proof')

            self.assertEqual(
                Client().get(f'/media/{record.proof_of_payment.name}').status_code,
                404,
            )
        finally:
            record.proof_of_payment.delete(save=False)

    def test_warehouse_api_is_role_and_branch_scoped(self):
        manager = User.objects.get(username='carlo')
        own_branch = manager.manager_profile.branch
        foreign_warehouse = Warehouse.objects.exclude(branch=own_branch).first()

        manager_response = self.login('carlo').get('/api/warehouses/')
        self.assertEqual(manager_response.status_code, 200)
        self.assertEqual(
            {row['branchId'] for row in manager_response.json()},
            {own_branch.pk},
        )
        self.assertEqual(
            self.login('carlo').get(
                f'/api/warehouses/{foreign_warehouse.pk}/'
            ).status_code,
            404,
        )
        self.assertEqual(self.login('juan').get('/api/warehouses/').json(), [])
        self.assertEqual(self.login('century').get('/api/warehouses/').json(), [])

    def test_manager_cannot_create_or_move_warehouse_to_foreign_branch(self):
        manager = User.objects.get(username='carlo')
        own = Warehouse.objects.get(branch=manager.manager_profile.branch)
        foreign_branch = Branch.objects.exclude(
            pk=manager.manager_profile.branch_id
        ).first()
        Warehouse.objects.filter(branch=foreign_branch).delete()

        create = self.login('carlo').post(
            '/api/warehouses/',
            {'branchId': foreign_branch.pk, 'name': 'Foreign warehouse'},
        )
        self.assertEqual(create.status_code, 403)

        move = self.login('carlo').patch(
            f'/api/warehouses/{own.pk}/',
            data={'branchId': foreign_branch.pk},
            content_type='application/json',
        )
        self.assertEqual(move.status_code, 403)
        own.refresh_from_db()
        self.assertEqual(own.branch_id, manager.manager_profile.branch_id)

    def test_api_registration_enforces_password_validators(self):
        client = Client()
        response = client.post(
            '/api/auth/register/',
            data={
                'username': 'weak-password-user',
                'email': 'weak@example.com',
                'password': 'password',
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('password', response.json())
        self.assertFalse(User.objects.filter(username='weak-password-user').exists())

        accepted = client.post(
            '/api/auth/register/',
            data={
                'username': 'secure-customer',
                'email': 'secure@example.com',
                'password': 'Secure!Pass2026',
                'fullName': 'Secure Customer',
                'contactNumber': '09170000000',
                'address': 'Baliuag, Bulacan',
                'acceptTerms': True,
            },
            content_type='application/json',
        )
        self.assertEqual(accepted.status_code, 201)
        customer = User.objects.get(username='secure-customer')
        self.assertEqual(customer.customer_profile.first_name, 'Secure')
        self.assertEqual(customer.customer_profile.last_name, 'Customer')

    def test_supplier_picker_is_limited_to_purchasing_roles(self):
        for username in ('juan', 'century', 'ana'):
            with self.subTest(username=username):
                response = self.login(username).get(
                    '/api/procurement/suppliers/'
                )
                self.assertEqual(response.status_code, 403)

        self.assertEqual(
            self.login('gerlyn').get('/api/procurement/suppliers/').status_code,
            200,
        )
        self.assertEqual(
            self.login('carlo').get('/api/procurement/suppliers/').status_code,
            200,
        )
