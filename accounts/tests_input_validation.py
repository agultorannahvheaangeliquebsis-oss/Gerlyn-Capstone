from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .input_validation import account_inputs
from .forms import AccountAdminCreationForm
from .models import User, VerificationToken
from .serializers import UserCreateSerializer, UserUpdateSerializer


class AccountInputValidationTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_customer_name_boundaries_and_legitimate_characters(self):
        for name in ("Anne Marie", "O'Connor", 'Dela-Cruz', 'D’Arcy', 'José', '李', 'A' * 50):
            with self.subTest(name=name):
                values, errors = account_inputs({'first_name': ' ' + name + ' '}, customer=True)
                self.assertEqual(errors, {})
                self.assertEqual(values['first_name'], name)
        for name in ('', '   ', 'A' * 51, 'Jane123', '<script>', '---', 'Jane\nDoe'):
            with self.subTest(name=name):
                self.assertIn('first_name', account_inputs({'first_name': name}, customer=True)[1])

    def test_username_limits_format_normalization_and_duplicates(self):
        User.objects.create_user(username='Existing', email='taken@example.com')
        for value in ('', ' ', 'a' * 151, 'has space', '<script>', 'existing', 'Ｅｘｉｓｔｉｎｇ'):
            with self.subTest(value=value):
                self.assertIn('username', account_inputs({'username': value})[1])
        self.assertEqual(account_inputs({'username': 'a' * 150})[1], {})

    def test_email_limits_duplicates_and_pending_addresses(self):
        User.objects.create_user(username='existing', email='taken@example.com', pending_email='pending@example.com')
        for value in ('invalid', 'TAKEN@example.com', 'PENDING@example.com', 'a' * 250 + '@example.com'):
            with self.subTest(value=value):
                self.assertIn('email', account_inputs({'email': value})[1])
        value = 'a' * 64 + '@' + 'b' * 63 + '.' + 'c' * 63 + '.' + 'd' * 61
        self.assertEqual(len(value), 254)
        self.assertEqual(account_inputs({'email': value})[1], {})

    def test_contact_formats_and_cross_role_duplicate_prevention(self):
        supplier = User.objects.create_user(username='supplier', role=User.Role.SUPPLIER)
        supplier.supplier_profile.contact_number = '0917-123-4567'
        supplier.supplier_profile.save()
        for value in ('09171234567', '+639171234567', '+63 917 123 4567'):
            self.assertIn('contact_number', account_inputs({'contact_number': value})[1])
        self.assertEqual(account_inputs({'contact_number': '+63 917 123 4567'}, user=supplier)[1], {})
        for value in ('1' * 21, '09' + ' ' * 30 + '171234567', '09١٢٣٤٥٦٧٨٩'):
            self.assertIn('contact_number', account_inputs({'contact_number': value})[1])

    def test_address_and_internal_name_bounds(self):
        self.assertEqual(account_inputs({'address': 'a' * 500, 'street_detail': 'a' * 255})[1], {})
        errors = account_inputs({'address': 'a' * 501, 'street_detail': 'a' * 256, 'region': 'a' * 11})[1]
        self.assertEqual(set(errors), {'address', 'street_detail', 'region'})
        self.assertEqual(account_inputs({'first_name': 'a' * 49, 'last_name': 'b' * 50})[1], {})
        self.assertIn('last_name', account_inputs({'first_name': 'a' * 50, 'last_name': 'b' * 50})[1])

    def test_password_maximum_and_whitespace_preservation(self):
        validate_password('Aa1!' + 'z' * 124)
        with self.assertRaises(ValidationError):
            validate_password('Aa1!' + 'z' * 125)
        password = ' Secure7! random '
        serializer = UserCreateSerializer(data={'username': 'valid', 'role': 'OWNER', 'password': password, 'fullName': 'Valid Name'})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        user = serializer.save()
        self.assertTrue(user.check_password(password))

    def test_every_role_admin_serializer_rejects_invalid_fields(self):
        for role in ('OWNER', 'BRANCH_MANAGER', 'STAFF', 'SUPPLIER'):
            for key, value in (('username', 'bad space'), ('email', 'bad'), ('fullName', 'x' * 101), ('contactNumber', '123'), ('address', 'x' * 501), ('branchId', 99999)):
                with self.subTest(role=role, key=key):
                    serializer = UserCreateSerializer(data={'username': 'valid', 'password': 'Secure7! random', 'role': role, key: value})
                    self.assertFalse(serializer.is_valid())
        self.assertEqual(User.objects.count(), 0)

    def test_api_registration_rejects_missing_overlong_and_malformed_inputs(self):
        payload = {'username': 'customer', 'email': 'customer@example.com', 'password': 'Secure7! random', 'fullName': 'Anne Customer', 'address': '12 Real Street', 'acceptTerms': True}
        for key, value in (('fullName', ''), ('fullName', 'A' * 51 + ' Smith'), ('contactNumber', 'invalid'), ('address', 'a' * 501), ('username', ['bad']), ('email', 'bad'), ('password', 'x' * 129)):
            with self.subTest(key=key, value=value):
                response = self.client.post('/api/auth/register/', {**payload, key: value}, content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(User.objects.count(), 0)

    def test_admin_model_clean_applies_profile_and_identity_validation(self):
        user = User.objects.create_user(username='valid', role='SUPPLIER')
        user.email = 'invalid'
        with self.assertRaises(ValidationError):
            user.full_clean()
        profile = user.supplier_profile
        profile.contact_person = 'A' * 101
        profile.address = 'x' * 501
        with self.assertRaises(ValidationError) as error:
            profile.full_clean()
        self.assertIn('contact_person', error.exception.message_dict)
        self.assertIn('address', error.exception.message_dict)

    def test_profile_error_does_not_save_other_fields(self):
        user = User.objects.create_user(username='customer', first_name='Anne', last_name='Customer', role='CUSTOMER')
        self.client.force_login(user)
        response = self.client.post(reverse('accounts:update_profile'), {'username': 'changed', 'first_name': 'x' * 51, 'last_name': 'Customer', 'contact_number': '09171234567'})
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.username, 'customer')
        self.assertEqual(user.customer_profile.contact_number, '')

    def test_invite_completion_rejects_overlong_name(self):
        user = User.objects.create_user(username='invited', role='STAFF', is_active=False)
        token = VerificationToken.objects.create(user=user, purpose=VerificationToken.PURPOSE_STAFF_INVITE)
        response = self.client.post(reverse('accounts:staff_invite_complete', args=[token.token]), {'username': 'valid', 'first_name': 'A' * 100, 'last_name': 'Smith', 'password1': 'Secure7! random', 'password2': 'Secure7! random'})
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        token.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(token.is_used)

    def test_update_serializer_cannot_partially_save_invalid_contact(self):
        user = User.objects.create_user(username='owner', role='OWNER')
        serializer = UserUpdateSerializer(user, data={'isActive': False, 'contactNumber': 'invalid'}, partial=True)
        self.assertFalse(serializer.is_valid())
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_registration_accepts_max_username_and_normalizes_names(self):
        response = self.client.post(reverse('accounts:register'), {
            'username': 'a' * 150, 'email': 'max@example.com',
            'first_name': "  Anne-Marie  ", 'last_name': "  O'Connor  ",
            'password1': 'Secure7! random', 'password2': 'Secure7! random',
            'address': 'a' * 500, 'accept_terms': '1',
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='a' * 150)
        self.assertEqual(user.customer_profile.first_name, 'Anne-Marie')
        self.assertEqual(user.customer_profile.last_name, "O'Connor")
        self.assertEqual(len(user.addresses.get().text), 500)

    def test_registration_rejects_empty_names_and_overlong_values(self):
        payload = {'username': 'customer', 'email': 'customer@example.com',
                   'first_name': 'Anne', 'last_name': 'Customer', 'address': '12 Real Street',
                   'password1': 'Secure7! random', 'password2': 'Secure7! random', 'accept_terms': '1'}
        for key, value in (('first_name', ''), ('last_name', ' '), ('first_name', 'a' * 51), ('address', 'a' * 501), ('street_detail', 'a' * 256)):
            cache.clear()
            with self.subTest(key=key):
                response = self.client.post(reverse('accounts:register'), {**payload, key: value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(User.objects.count(), 0)

    def test_supplier_updates_validate_and_display_contact_person_and_address_errors(self):
        user = User.objects.create_user(username='supplier', role='SUPPLIER')
        self.client.force_login(user)
        response = self.client.post(reverse('supplier_account_update'), {
            'username': 'changed', 'contact_person': '<script>', 'address': 'x' * 501,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Use letters, spaces')
        self.assertContains(response, '500')
        user.refresh_from_db()
        self.assertEqual(user.username, 'supplier')

    def test_django_admin_creation_validates_all_roles(self):
        for role in User.Role.values:
            data = {'username': role.lower(), 'role': role, 'email': role.lower() + '@example.com',
                    'first_name': 'Anne', 'last_name': "O'Connor",
                    'password1': 'Secure7! random', 'password2': 'Secure7! random',
                    'usable_password': 'true'}
            with self.subTest(role=role):
                form = AccountAdminCreationForm(data=data)
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                form = AccountAdminCreationForm(data={**data, 'username': 'different', 'first_name': 'A' * 101})
                self.assertFalse(form.is_valid())
                self.assertIn('first_name', form.errors)

    def test_phone_duplicate_with_legacy_whitespace(self):
        user = User.objects.create_user(username='supplier', role='SUPPLIER')
        user.supplier_profile.contact_number = '0917\t123\u00a04567'
        user.supplier_profile.save()
        self.assertIn('contact_number', account_inputs({'contact_number': '+639171234567'})[1])

    def test_business_identifier_limits_format_and_case_insensitive_uniqueness(self):
        user = User.objects.create_user(username='supplier', role='SUPPLIER')
        user.supplier_profile.business_registration_id = 'SEC-123'
        user.supplier_profile.save()
        for value in ('sec-123', 'a' * 101, '<script>', 'ABC\n123'):
            with self.subTest(value=value):
                self.assertIn('business_registration_id', account_inputs({'business_registration_id': value})[1])
        self.assertEqual(account_inputs({'business_registration_id': 'a' * 100})[1], {})
