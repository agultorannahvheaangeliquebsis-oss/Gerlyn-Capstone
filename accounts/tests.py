"""Add/Edit User, Supplier and Branch moved from their own pages into modals
on their list pages. The forms' save logic was left alone — these cover the
two things the move did change (the GET route and the error path), plus the
writes themselves, so a regression in either shows up here rather than in a
browser.
"""
import re

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, User, VerificationToken
from accounts.role_policy import USER_MANAGEMENT_ROLES
from accounts.rate_limit import (
    LOGIN_MAX_ATTEMPTS,
    clear_failed_logins,
    is_login_locked_out,
    register_failed_login,
)
from accounts.two_factor import OTP_MAX_ATTEMPTS
from branches.models import Branch


def _latest_otp():
    """Pulls the 6-digit code out of the most recently sent email — the code
    is random, generated fresh per test, so tests read it back rather than
    hardcoding a value."""
    body = mail.outbox[-1].body
    return re.search(r'login code is: (\d{6})', body).group(1)


class ContactNumberValidatorTests(TestCase):
    def test_accepts_every_format_already_in_use_across_seed_data_and_tests(self):
        from accounts.validators import validate_ph_mobile_number

        for value in (
            '09171234567', '0917-123-4567', '+639171234567', '+63 917 123 4567',
            '0917-000-0001', '0918-000-0000', '0999-000-0000',
        ):
            validate_ph_mobile_number(value)  # raises on failure

    def test_rejects_obviously_wrong_input(self):
        from django.core.exceptions import ValidationError

        from accounts.validators import validate_ph_mobile_number

        for value in ('not-a-number', '12345', '091712345', '+1234567890'):
            with self.assertRaises(ValidationError):
                validate_ph_mobile_number(value)


class UserModalFlowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.client.force_login(self.owner)

    def test_get_add_and_edit_urls_redirect_to_the_list(self):
        self.assertRedirects(
            self.client.get(reverse('accounts:user_add')),
            reverse('accounts:user_list') + '?add=1',
        )
        self.assertRedirects(
            self.client.get(reverse('accounts:user_edit', args=[self.owner.pk])),
            reverse('accounts:user_list') + f'?edit={self.owner.pk}',
        )

    def test_inviting_a_manager_creates_an_inactive_account_and_sets_their_branch(self):
        response = self.client.post(reverse('accounts:user_add'), {
            'email': 'carlo@example.com',
            'role': User.Role.BRANCH_MANAGER, 'branch': self.branch.pk,
        })
        self.assertRedirects(response, reverse('accounts:user_list'))
        carlo = User.objects.get(email='carlo@example.com')
        self.assertEqual(carlo.role, User.Role.BRANCH_MANAGER)
        self.assertFalse(carlo.is_active)
        self.assertFalse(carlo.has_usable_password())
        profile = BranchManagerProfile.objects.get(user=carlo)
        self.assertEqual(profile.branch_id, self.branch.pk)

    def test_inviting_sends_the_invite_email(self):
        self.client.post(reverse('accounts:user_add'), {
            'email': 'invitee@example.com', 'role': User.Role.STAFF, 'branch': self.branch.pk,
        })
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['invitee@example.com'])
        user = User.objects.get(email='invitee@example.com')
        self.assertTrue(
            VerificationToken.objects.filter(
                user=user, purpose=VerificationToken.PURPOSE_STAFF_INVITE,
            ).exists()
        )

    def test_missing_email_reopens_the_modal(self):
        response = self.client.post(reverse('accounts:user_add'), {
            'role': User.Role.STAFF, 'branch': self.branch.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertIn('An email address is required.', response.context['add_errors'])

    def test_a_staff_or_manager_invite_without_a_branch_is_refused(self):
        response = self.client.post(reverse('accounts:user_add'), {
            'email': 'nobranch@example.com', 'role': User.Role.STAFF,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('Assign a branch for this role.', response.context['add_errors'])
        self.assertFalse(User.objects.filter(email='nobranch@example.com').exists())

    def test_duplicate_email_is_rejected(self):
        User.objects.create_user(
            username='existing', password='pw', role=User.Role.STAFF,
            email='taken@example.com',
        )
        response = self.client.post(reverse('accounts:user_add'), {
            'email': 'taken@example.com', 'role': User.Role.STAFF, 'branch': self.branch.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertEqual(User.objects.filter(email='taken@example.com').count(), 1)

    def test_a_non_internal_role_is_still_refused(self):
        response = self.client.post(reverse('accounts:user_add'), {
            'email': 'shopper@example.com', 'role': User.Role.CUSTOMER,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='shopper@example.com').exists())

    def test_customer_is_absent_from_add_edit_and_filter_role_choices(self):
        response = self.client.get(
            reverse('accounts:user_list'), {'edit': self.owner.pk}
        )

        expected_roles = set(USER_MANAGEMENT_ROLES)
        self.assertEqual(
            {value for value, _label in response.context['role_choices']},
            expected_roles,
        )
        self.assertEqual(
            {value for value, _label in response.context['form_role_choices']},
            expected_roles,
        )
        self.assertNotContains(response, '<option value="CUSTOMER"')

    def test_customer_cannot_be_converted_through_internal_edit_url(self):
        customer = User.objects.create_user(
            username='existing-customer', password='pw',
            role=User.Role.CUSTOMER,
        )

        response = self.client.post(
            reverse('accounts:user_edit', args=[customer.pk]),
            {
                'username': customer.username,
                'role': User.Role.STAFF,
                'branch': self.branch.pk,
            },
        )

        self.assertEqual(response.status_code, 404)
        customer.refresh_from_db()
        self.assertEqual(customer.role, User.Role.CUSTOMER)

    def test_forged_customer_role_edit_is_rejected(self):
        staff = User.objects.create_user(
            username='role-edit-staff', password='pw', role=User.Role.STAFF,
        )

        response = self.client.post(
            reverse('accounts:user_edit', args=[staff.pk]),
            {
                'username': staff.username,
                'role': User.Role.CUSTOMER,
                'branch': self.branch.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'A username and a valid role are required.',
            response.context['edit_errors'],
        )
        staff.refresh_from_db()
        self.assertEqual(staff.role, User.Role.STAFF)

    def test_editing_without_a_password_keeps_the_old_one(self):
        staff = User.objects.create_user(
            username='ana', password='original', role=User.Role.STAFF
        )
        self.client.post(reverse('accounts:user_edit', args=[staff.pk]), {
            'username': 'ana', 'password': '', 'role': User.Role.STAFF,
            'first_name': 'Ana', 'branch': self.branch.pk,
        })
        staff.refresh_from_db()
        self.assertTrue(staff.check_password('original'))
        self.assertEqual(staff.first_name, 'Ana')

    def test_editing_sets_a_valid_contact_number_on_the_role_profile(self):
        staff = User.objects.create_user(
            username='ana', password='pw', role=User.Role.STAFF
        )
        response = self.client.post(reverse('accounts:user_edit', args=[staff.pk]), {
            'username': 'ana', 'password': '', 'role': User.Role.STAFF,
            'first_name': 'Ana', 'branch': self.branch.pk,
            'contact_number': '0917-123-4567',
        })
        self.assertRedirects(response, reverse('accounts:user_list'))
        staff.refresh_from_db()
        self.assertEqual(staff.staff_profile.contact_number, '0917-123-4567')

    def test_editing_with_a_malformed_contact_number_reopens_the_modal(self):
        staff = User.objects.create_user(
            username='ana', password='pw', role=User.Role.STAFF
        )
        response = self.client.post(reverse('accounts:user_edit', args=[staff.pk]), {
            'username': 'ana', 'password': '', 'role': User.Role.STAFF,
            'first_name': 'Ana', 'branch': self.branch.pk,
            'contact_number': 'not-a-number',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_edit_modal'])
        self.assertIn(
            'Enter a valid PH mobile number, e.g. 0917 123 4567 or +63 917 123 4567.',
            response.context['edit_errors'],
        )
        staff.refresh_from_db()
        self.assertEqual(staff.staff_profile.contact_number, '')

    def test_editing_with_a_contact_number_already_used_elsewhere_is_refused(self):
        ben = User.objects.create_user(
            username='ben', password='pw', role=User.Role.STAFF
        )
        ben.staff_profile.contact_number = '0917-123-4567'
        ben.staff_profile.save()
        staff = User.objects.create_user(
            username='ana', password='pw', role=User.Role.STAFF
        )
        response = self.client.post(reverse('accounts:user_edit', args=[staff.pk]), {
            'username': 'ana', 'password': '', 'role': User.Role.STAFF,
            'first_name': 'Ana', 'branch': self.branch.pk,
            'contact_number': '0917-123-4567',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('That contact number is already in use.', response.context['edit_errors'])

    def test_non_owner_cannot_post(self):
        staff = User.objects.create_user(
            username='ana', password='pw', role=User.Role.STAFF
        )
        self.client.force_login(staff)
        response = self.client.post(reverse('accounts:user_add'), {
            'username': 'sneaky', 'password': 'pw123456', 'role': User.Role.OWNER,
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(username='sneaky').exists())


class UserManagementApiRolePolicyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='api-owner', password='pw', role=User.Role.OWNER
        )
        self.customer = User.objects.create_user(
            username='api-customer', password='pw',
            email='api-customer@example.com', role=User.Role.CUSTOMER,
        )
        self.client.force_login(self.owner)

    def test_internal_api_does_not_list_customers(self):
        response = self.client.get('/api/auth/users/')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            User.Role.CUSTOMER,
            {row['roleCode'] for row in response.json()},
        )

    def test_forged_customer_creation_is_rejected(self):
        response = self.client.post(
            '/api/auth/users/',
            {
                'username': 'forged-customer',
                'email': 'forged-customer@example.com',
                'password': 'Str0ngPass!2026',
                'role': User.Role.CUSTOMER,
                'fullName': 'Forged Customer',
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('role', response.json())
        self.assertFalse(User.objects.filter(username='forged-customer').exists())

    def test_internal_api_still_creates_an_allowed_supplier(self):
        response = self.client.post(
            '/api/auth/users/',
            {
                'username': 'api-supplier',
                'email': 'api-supplier@example.com',
                'password': 'Str0ngPass!2026',
                'role': User.Role.SUPPLIER,
                'fullName': 'API Supplier',
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            User.objects.filter(
                username='api-supplier', role=User.Role.SUPPLIER,
            ).exists()
        )

    def test_internal_api_cannot_update_a_customer(self):
        response = self.client.patch(
            f'/api/auth/users/{self.customer.pk}/',
            {'isActive': False},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 404)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_active)

    def test_customer_registration_api_still_creates_a_customer(self):
        self.client.logout()
        response = self.client.post(
            '/api/auth/register/',
            {
                'username': 'registered-customer',
                'email': 'registered-customer@example.com',
                'password': 'Str0ngPass!2026',
                'fullName': 'Registered Customer',
                'contactNumber': '09171234567',
                'address': '12 Real St, Baliuag, Bulacan',
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 201)
        registered = User.objects.get(username='registered-customer')
        self.assertEqual(registered.role, User.Role.CUSTOMER)
        self.assertEqual(registered.customer_profile.first_name, 'Registered')


class CustomerInviteBoundaryTests(TestCase):
    def test_customer_staff_invite_token_cannot_be_completed(self):
        customer = User.objects.create_user(
            username='invite-customer', password='original-password',
            email='invite-customer@example.com', role=User.Role.CUSTOMER,
        )
        token = VerificationToken.objects.create(
            user=customer, purpose=VerificationToken.PURPOSE_STAFF_INVITE,
        )

        response = self.client.get(
            reverse('accounts:staff_invite_complete', args=[token.token])
        )

        self.assertRedirects(
            response, reverse('accounts:login'), fetch_redirect_response=False
        )
        token.refresh_from_db()
        customer.refresh_from_db()
        self.assertTrue(token.is_used)
        self.assertTrue(customer.check_password('original-password'))
        self.assertEqual(customer.role, User.Role.CUSTOMER)


class SupplierModalFlowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)

    def test_get_add_url_redirects_to_the_list(self):
        self.assertRedirects(
            self.client.get(reverse('supplier_add')),
            reverse('supplier_list') + '?add=1',
        )

    def test_creating_a_supplier_fills_the_profile(self):
        response = self.client.post(reverse('supplier_add'), {
            'supplier_name': 'Century Pacific Food Inc.',
            'username': 'centurypacific',
            'contact_person': 'Ricardo Buenaventura',
            'contact_number': '0917-123-4567',
            'supplier_category': 'Canned Goods',
            'email': 'sales@supplier.com.ph',
            'address': 'Laguna Technical Park',
        })
        self.assertRedirects(response, reverse('supplier_list'))
        supplier = User.objects.get(username='centurypacific')
        self.assertEqual(supplier.role, User.Role.SUPPLIER)
        profile = supplier.supplier_profile
        self.assertEqual(profile.supplier_name, 'Century Pacific Food Inc.')
        self.assertEqual(profile.contact_person, 'Ricardo Buenaventura')
        self.assertEqual(profile.address, 'Laguna Technical Park')

    def test_missing_name_reopens_the_modal_and_keeps_the_rest(self):
        response = self.client.post(reverse('supplier_add'), {
            'supplier_name': '', 'username': 'centurypacific',
            'contact_person': 'Ricardo Buenaventura',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertContains(response, 'Ricardo Buenaventura')
        self.assertFalse(User.objects.filter(username='centurypacific').exists())

    def test_duplicate_username_is_rejected_without_creating_a_user(self):
        response = self.client.post(reverse('supplier_add'), {
            'supplier_name': 'Anything', 'username': 'owner',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertEqual(User.objects.filter(username='owner').count(), 1)
        self.assertEqual(User.objects.filter(role=User.Role.SUPPLIER).count(), 0)

    def test_editing_updates_the_profile_not_a_new_user(self):
        self.client.post(reverse('supplier_add'), {
            'supplier_name': 'Old Name', 'username': 'century',
        })
        supplier = User.objects.get(username='century')
        self.client.post(reverse('supplier_edit', args=[supplier.pk]), {
            'supplier_name': 'New Name', 'username': 'century',
            'supplier_category': 'Canned Goods',
        })
        supplier.refresh_from_db()
        self.assertEqual(supplier.supplier_profile.supplier_name, 'New Name')
        self.assertEqual(User.objects.filter(role=User.Role.SUPPLIER).count(), 1)

    def test_manager_can_see_the_list_but_not_post(self):
        manager = User.objects.create_user(
            username='carlo', password='pw', role=User.Role.BRANCH_MANAGER
        )
        self.client.force_login(manager)
        self.assertEqual(self.client.get(reverse('supplier_list')).status_code, 200)
        self.client.post(reverse('supplier_add'), {
            'supplier_name': 'Nope', 'username': 'nope',
        })
        self.assertFalse(User.objects.filter(username='nope').exists())


class BranchModalFlowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)

    def test_get_add_url_redirects_to_the_list(self):
        self.assertRedirects(
            self.client.get(reverse('branch_add')),
            reverse('branch_list') + '?add=1',
        )

    def test_creating_a_branch_also_creates_its_warehouse(self):
        response = self.client.post(reverse('branch_add'), {
            'branch_name': 'Tarcan', 'location': 'Baliuag, Bulacan',
        })
        self.assertRedirects(response, reverse('branch_list'))
        branch = Branch.objects.get(branch_name='Tarcan')
        self.assertEqual(branch.location, 'Baliuag, Bulacan')
        self.assertTrue(
            branch.warehouse_set.exists() if hasattr(branch, 'warehouse_set')
            else Branch.objects.filter(pk=branch.pk).exists()
        )
        from inventory.models import Warehouse
        self.assertTrue(Warehouse.objects.filter(branch=branch).exists())

    def test_blank_name_reopens_the_modal_and_keeps_the_location(self):
        response = self.client.post(reverse('branch_add'), {
            'branch_name': '', 'location': 'Baliuag, Bulacan',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertIn('Branch name is required.', response.context['add_errors'])
        self.assertContains(response, 'Baliuag, Bulacan')
        self.assertFalse(Branch.objects.exists())

    def test_editing_renames_without_making_a_second_warehouse(self):
        from inventory.models import Warehouse
        self.client.post(reverse('branch_add'), {'branch_name': 'Tarcan'})
        branch = Branch.objects.get(branch_name='Tarcan')
        self.client.post(reverse('branch_edit', args=[branch.pk]), {
            'branch_name': 'Tarcan Main', 'location': 'Baliuag',
        })
        branch.refresh_from_db()
        self.assertEqual(branch.branch_name, 'Tarcan Main')
        self.assertEqual(Warehouse.objects.filter(branch=branch).count(), 1)

    def test_list_opens_the_edit_modal_and_ignores_a_bad_pk(self):
        branch = Branch.objects.create(branch_name='Tarcan', owner=self.owner)
        good = self.client.get(reverse('branch_list'), {'edit': branch.pk})
        self.assertTrue(good.context['show_edit_modal'])
        self.assertEqual(good.context['edit_branch'].pk, branch.pk)

        bad = self.client.get(reverse('branch_list'), {'edit': '9999'})
        self.assertEqual(bad.status_code, 200)
        self.assertNotIn('show_edit_modal', bad.context)

    def test_non_owner_cannot_post(self):
        staff = User.objects.create_user(
            username='ana', password='pw', role=User.Role.STAFF
        )
        self.client.force_login(staff)
        self.client.post(reverse('branch_add'), {'branch_name': 'Sneaky'})
        self.assertFalse(Branch.objects.filter(branch_name='Sneaky').exists())


class RegistrationVerificationEmailTests(TestCase):
    """register_view creates the account and logs the customer straight in
    (verification is optional, not a login gate), but it must still send
    them a verification email — it used to silently not call
    send_verification_email at all, so nobody who signed up ever got one
    unless they found the "Resend" banner on their own Profile page."""

    def _register(self, **overrides):
        payload = {
            'username': 'newcustomer',
            'email': 'newcustomer@example.com',
            'password1': 'Str0ngPass!2026',
            'password2': 'Str0ngPass!2026',
            'first_name': 'New',
            'last_name': 'Customer',
            'contact_number': '09171234567',
            # The delivery address is required (register_view refuses a blank
            # one). It is a plain typed line; the barangay/street fields are
            # optional extras off the dropdowns.
            'address': '12 Real St, Poblacion, Baliuag, Bulacan',
            'barangay': '031403001',
            'street_detail': '12 Real St',
        }
        payload.update(overrides)
        return self.client.post(reverse('accounts:register'), payload)

    def test_registering_sends_a_verification_email(self):
        response = self._register()
        self.assertRedirects(response, reverse('accounts:customer_dashboard'))

        user = User.objects.get(username='newcustomer')
        self.assertFalse(user.is_email_verified)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['newcustomer@example.com'])
        self.assertIn('verify', mail.outbox[0].subject.lower())
        self.assertTrue(
            VerificationToken.objects.filter(
                user=user, purpose=VerificationToken.PURPOSE_EMAIL_VERIFY,
            ).exists()
        )

    def test_the_emailed_link_actually_verifies_the_account(self):
        self._register()
        user = User.objects.get(username='newcustomer')
        token = VerificationToken.objects.get(
            user=user, purpose=VerificationToken.PURPOSE_EMAIL_VERIFY,
        )

        response = self.client.get(reverse('accounts:verify_email', args=[token.token]))
        self.assertRedirects(
            response, reverse('accounts:login'), fetch_redirect_response=False
        )

        user.refresh_from_db()
        self.assertTrue(user.is_email_verified)

    def test_a_successful_signup_creates_the_one_default_address(self):
        """checkout ships to the account's default Address, so registration
        has to leave one behind."""
        self._register()
        user = User.objects.get(username='newcustomer')

        address = user.addresses.get()
        self.assertTrue(address.is_default)
        self.assertEqual(address.text, '12 Real St, Poblacion, Baliuag, Bulacan')
        self.assertEqual(address.barangay, '031403001')

    def test_signup_is_refused_without_an_address(self):
        from accounts.models import Address

        response = self._register(address='')

        self.assertEqual(response.status_code, 200)  # re-rendered, not redirected
        self.assertFalse(User.objects.filter(username='newcustomer').exists())
        self.assertEqual(Address.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)


class RegisterInputValidationTests(TestCase):
    """register_view used to pass username/email straight to create_user()
    with no format or length check — an empty username hit a NOT NULL
    constraint and an over-length one hit MySQL's own column-length error,
    both surfacing as a bare 500 instead of a message on the form."""

    def _register(self, **overrides):
        payload = {
            'username': 'newcustomer',
            'email': 'newcustomer@example.com',
            'password1': 'Str0ngPass!2026',
            'password2': 'Str0ngPass!2026',
            'first_name': 'New',
            'last_name': 'Customer',
            'contact_number': '09171234567',
            'address': '12 Real St, Poblacion, Baliuag, Bulacan',
        }
        payload.update(overrides)
        return self.client.post(reverse('accounts:register'), payload)

    def test_an_empty_username_is_rejected_not_crashed(self):
        response = self._register(username='')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='newcustomer@example.com').exists())

    def test_a_username_over_150_characters_is_rejected_not_crashed(self):
        response = self._register(username='x' * 200)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='newcustomer@example.com').exists())

    def test_a_malformed_email_is_rejected(self):
        response = self._register(email='not-an-email')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='newcustomer').exists())

    def test_a_valid_submission_still_succeeds(self):
        """Regression guard — the new checks must not block a normal signup."""
        response = self._register()
        self.assertRedirects(response, reverse('accounts:customer_dashboard'))
        self.assertTrue(User.objects.filter(username='newcustomer').exists())

    def test_an_optional_contact_number_left_blank_still_succeeds(self):
        response = self._register(contact_number='')
        self.assertRedirects(response, reverse('accounts:customer_dashboard'))

    def test_a_malformed_contact_number_is_rejected(self):
        response = self._register(contact_number='12345')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='newcustomer').exists())

    def test_a_contact_number_already_registered_is_rejected(self):
        self._register()
        self.client.logout()
        response = self._register(
            username='anothercustomer', email='another@example.com',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='anothercustomer').exists())

    def test_sixth_attempt_from_the_same_ip_within_the_window_is_refused(self):
        from accounts.rate_limit import REGISTER_MAX_ATTEMPTS

        for i in range(REGISTER_MAX_ATTEMPTS):
            self._register(username=f'flooduser{i}', email=f'flood{i}@example.com')
            # Each successful signup logs the test client in; register_view's
            # own "already authenticated" redirect would otherwise short-circuit
            # every attempt after the first, never reaching the rate limit at
            # all — logging back out between attempts is what actually
            # simulates 6 separate anonymous visitors sharing one IP.
            self.client.logout()

        response = self._register(username='oneMoreUser', email='onemore@example.com')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='oneMoreUser').exists())
        self.assertContains(response, 'Too many sign-up attempts')


class SelfEditContactNumberTests(TestCase):
    """update_profile's contact-number branch — the same one internal
    users hit through internal_settings.html, since both templates post to
    this one view. Only customer login is exercised here; the branch itself
    doesn't care which template rendered the form."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.user)

    def test_a_valid_number_is_saved(self):
        response = self.client.post(reverse('accounts:update_profile'), {
            'contact_number': '0917-123-4567',
        })
        self.assertRedirects(response, reverse('accounts:profile'))
        self.user.customer_profile.refresh_from_db()
        self.assertEqual(self.user.customer_profile.contact_number, '0917-123-4567')

    def test_a_malformed_number_is_rejected_with_a_field_error(self):
        response = self.client.post(reverse('accounts:update_profile'), {
            'contact_number': '12345',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['errors']['contact_number'],
            'Enter a valid PH mobile number, e.g. 0917 123 4567 or +63 917 123 4567.',
        )
        self.user.customer_profile.refresh_from_db()
        self.assertEqual(self.user.customer_profile.contact_number, '')

    def test_leaving_it_blank_does_not_touch_the_existing_number(self):
        self.user.customer_profile.contact_number = '0917-123-4567'
        self.user.customer_profile.save()
        response = self.client.post(reverse('accounts:update_profile'), {
            'contact_number': '',
        })
        self.assertRedirects(response, reverse('accounts:profile'))
        self.user.customer_profile.refresh_from_db()
        self.assertEqual(self.user.customer_profile.contact_number, '0917-123-4567')


class SavedAddressTests(TestCase):
    """Settings → Addresses needs the address line filled in. The server
    composes it from the dropdowns as a fallback, but with neither there is
    nothing to save. Same rule as registration, because checkout ships to
    whatever is saved here."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='addr-user', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.user)

    def _create(self, **overrides):
        payload = {
            'type': 'HOME',
            'text': '9 Gil Carlos St, Baliuag, Bulacan',
            'barangay': '031403001',
            'street_detail': '9 Gil Carlos St',
        }
        payload.update(overrides)
        return self.client.post(reverse('accounts:address_create'), payload, follow=True)

    def test_a_full_address_is_saved_and_becomes_the_default(self):
        from accounts.models import Address

        self._create()

        address = Address.objects.get(user=self.user)
        self.assertTrue(address.is_default)
        self.assertEqual(address.barangay, '031403001')
        self.assertEqual(address.text, '9 Gil Carlos St, Baliuag, Bulacan')

    def test_a_blank_address_with_no_dropdowns_is_refused(self):
        from django.contrib.messages import get_messages

        from accounts.models import Address

        response = self._create(
            text='', barangay='', street_detail='', city='', province='', region='',
        )

        self.assertEqual(Address.objects.filter(user=self.user).count(), 0)
        self.assertTrue(any(
            'address' in str(m).lower()
            for m in get_messages(response.wsgi_request)
        ))

    def test_a_blank_address_line_is_composed_from_the_dropdowns(self):
        """No text typed, but a barangay/city/province chosen — the server
        builds the line from those rather than refusing."""
        from accounts.models import Address

        self._create(text='')

        address = Address.objects.get(user=self.user)
        self.assertTrue(address.text)
        self.assertEqual(address.barangay, '031403001')


class LoginLockoutCacheTests(TestCase):
    """The 5-attempt lockout (accounts/rate_limit.py) stores its counter in
    the Django cache. It used to land in the default LocMemCache, which is
    per-process: under Gunicorn's multiple workers each worker kept its own
    count, so the real cap was 5 x worker count and every redeploy reset it.
    settings.py now points CACHES at DatabaseCache so all workers share one
    durable store.

    A single-process test run can't prove multi-worker sharing, so these
    tests do two things it *can* prove: (1) the backend is actually the
    shared one, not LocMemCache — a misconfiguration guard; (2) the lockout
    still counts, trips and clears end to end against whatever backend is
    configured.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(
            username='cashier', password='Right-Pass-2026', role=User.Role.STAFF
        )

    def test_configured_cache_is_shared_across_processes(self):
        # LocMemCache is per-process and would silently reintroduce the bug.
        from django.conf import settings

        backend = settings.CACHES['default']['BACKEND']
        self.assertNotEqual(
            backend, 'django.core.cache.backends.locmem.LocMemCache',
            'login lockout must not run on a per-process cache',
        )
        self.assertEqual(
            backend, 'django.core.cache.backends.db.DatabaseCache', backend
        )

    def test_helpers_count_trip_and_clear(self):
        for _ in range(LOGIN_MAX_ATTEMPTS - 1):
            register_failed_login('cashier')
        self.assertFalse(is_login_locked_out('cashier'))

        register_failed_login('cashier')
        self.assertTrue(is_login_locked_out('cashier'))

        clear_failed_logins('cashier')
        self.assertFalse(is_login_locked_out('cashier'))

    def test_identifier_is_normalised(self):
        for _ in range(LOGIN_MAX_ATTEMPTS):
            register_failed_login('  CASHIER  ')
        # Same account, different casing/whitespace -> same lock.
        self.assertTrue(is_login_locked_out('cashier'))

    def test_login_view_locks_out_after_five_failures(self):
        login_url = reverse('accounts:login')
        for _ in range(LOGIN_MAX_ATTEMPTS):
            self.client.post(login_url, {'username': 'cashier', 'password': 'wrong'})

        # 6th attempt — even with the CORRECT password — is refused.
        response = self.client.post(
            login_url, {'username': 'cashier', 'password': 'Right-Pass-2026'}
        )
        self.assertContains(response, 'Too many failed attempts')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_successful_login_clears_the_counter(self):
        login_url = reverse('accounts:login')
        for _ in range(LOGIN_MAX_ATTEMPTS - 1):
            self.client.post(login_url, {'username': 'cashier', 'password': 'wrong'})

        self.client.post(
            login_url, {'username': 'cashier', 'password': 'Right-Pass-2026'}
        )
        self.assertIn('_auth_user_id', self.client.session)
        self.assertFalse(is_login_locked_out('cashier'))

    def test_api_login_returns_429_after_five_failures(self):
        api_url = reverse('api-login')
        for _ in range(LOGIN_MAX_ATTEMPTS):
            self.client.post(
                api_url, {'username': 'cashier', 'password': 'wrong'}
            )

        response = self.client.post(
            api_url, {'username': 'cashier', 'password': 'Right-Pass-2026'}
        )
        self.assertEqual(response.status_code, 429)


class TwoFactorLoginTests(TestCase):
    """Turning on Two-Factor Authentication used to change nothing about
    login — both the template login and the API's LoginView still let a
    correct password straight in. This is the actual second factor: a
    password success emails a 6-digit code and holds the session unauthenticated
    until that code is entered too."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='secure_owner', password='Right-Pass-2026',
            email='owner@example.com', role=User.Role.OWNER,
            two_factor_enabled=True,
        )

    # ── Template login flow ────────────────────────────────

    def test_correct_password_does_not_log_in_when_2fa_is_on(self):
        response = self.client.post(reverse('accounts:login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        self.assertRedirects(response, reverse('accounts:login_verify'))
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['owner@example.com'])

    def test_correct_code_completes_the_login(self):
        self.client.post(reverse('accounts:login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        code = _latest_otp()
        response = self.client.post(reverse('accounts:login_verify'), {'code': code})
        self.assertRedirects(response, reverse('accounts:owner_dashboard'))
        self.assertIn('_auth_user_id', self.client.session)

    def test_wrong_code_does_not_log_in(self):
        self.client.post(reverse('accounts:login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        real_code = _latest_otp()
        wrong_code = '000000' if real_code != '000000' else '111111'
        response = self.client.post(reverse('accounts:login_verify'), {'code': wrong_code})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Incorrect code')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_code_is_single_use(self):
        self.client.post(reverse('accounts:login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        code = _latest_otp()
        self.client.post(reverse('accounts:login_verify'), {'code': code})
        self.client.get(reverse('accounts:logout'))

        # Logging in again starts a fresh pending state; replaying the old
        # code against it must fail even though it was correct once.
        self.client.post(reverse('accounts:login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        response = self.client.post(reverse('accounts:login_verify'), {'code': code})
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_too_many_wrong_codes_locks_out_and_requires_a_fresh_login(self):
        self.client.post(reverse('accounts:login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        real_code = _latest_otp()
        wrong_code = '000000' if real_code != '000000' else '111111'
        for _ in range(OTP_MAX_ATTEMPTS):
            self.client.post(reverse('accounts:login_verify'), {'code': wrong_code})

        response = self.client.post(reverse('accounts:login_verify'), {'code': real_code})
        self.assertRedirects(response, reverse('accounts:login'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_resend_sends_a_new_code_and_invalidates_the_old_one(self):
        self.client.post(reverse('accounts:login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        first_code = _latest_otp()
        self.client.post(reverse('accounts:login_verify'), {'resend': '1'})
        self.assertEqual(len(mail.outbox), 2)
        second_code = _latest_otp()

        # The old code no longer works once a resend issued a new one.
        stale = self.client.post(reverse('accounts:login_verify'), {'code': first_code})
        self.assertNotIn('_auth_user_id', self.client.session)

        fresh = self.client.post(reverse('accounts:login_verify'), {'code': second_code})
        self.assertIn('_auth_user_id', self.client.session)

    def test_login_without_2fa_is_unaffected(self):
        User.objects.create_user(
            username='plain_staff', password='Right-Pass-2026', role=User.Role.STAFF,
        )
        response = self.client.post(reverse('accounts:login'), {
            'username': 'plain_staff', 'password': 'Right-Pass-2026',
        })
        self.assertIn('_auth_user_id', self.client.session)
        self.assertEqual(len(mail.outbox), 0)

    def test_verify_page_is_unreachable_without_a_pending_login(self):
        response = self.client.get(reverse('accounts:login_verify'))
        self.assertRedirects(response, reverse('accounts:login'))

    # ── API login flow ─────────────────────────────────────

    def test_api_login_requires_verification_when_2fa_is_on(self):
        response = self.client.post(reverse('api-login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['two_factor_required'])
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_api_verify_completes_the_login(self):
        self.client.post(reverse('api-login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        code = _latest_otp()
        response = self.client.post(reverse('api-verify-2fa'), {'code': code})
        self.assertEqual(response.status_code, 200)
        self.assertIn('_auth_user_id', self.client.session)

    def test_api_verify_rejects_a_wrong_code(self):
        self.client.post(reverse('api-login'), {
            'username': 'secure_owner', 'password': 'Right-Pass-2026',
        })
        real_code = _latest_otp()
        wrong_code = '000000' if real_code != '000000' else '111111'
        response = self.client.post(reverse('api-verify-2fa'), {'code': wrong_code})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_api_verify_without_a_pending_login_is_rejected(self):
        response = self.client.post(reverse('api-verify-2fa'), {'code': '123456'})
        self.assertEqual(response.status_code, 400)


class ApiThrottlingTests(TestCase):
    """No /api/ endpoint limited how often it could be called — this proves
    DEFAULT_THROTTLE_CLASSES/RATES in REST_FRAMEWORK actually engage, using
    an artificially tiny rate so the test doesn't need 100+ requests to
    prove it.

    SimpleRateThrottle.THROTTLE_RATES is set from api_settings once at
    import time, not re-read per request — @override_settings on
    REST_FRAMEWORK does not reach it, so the rate is patched directly on the
    class both AnonRateThrottle and UserRateThrottle inherit it from.
    """

    def test_repeated_anonymous_requests_are_throttled(self):
        from unittest.mock import patch

        from rest_framework.throttling import SimpleRateThrottle

        url = reverse('api-login')
        with patch.object(SimpleRateThrottle, 'THROTTLE_RATES', {'anon': '2/min', 'user': '2/min'}):
            for _ in range(2):
                response = self.client.post(url, {'username': 'nobody', 'password': 'wrong'})
                self.assertNotEqual(response.status_code, 429)

            response = self.client.post(url, {'username': 'nobody', 'password': 'wrong'})
            self.assertEqual(response.status_code, 429)


class DeactivateAccountIsReversibleTests(TestCase):
    """Deactivate used to permanently scrub identity fields the moment it
    ran — the same action as a hard delete, just triggered by an ordinary
    "I don't want this anymore" click. It's now a plain, reversible
    is_active toggle; hard_delete_personal_data is the only thing that
    erases anything, and only ever in response to an actual request."""

    def setUp(self):
        self.customer = User.objects.create_user(
            username='deac-cust', password='Right-Pass-2026',
            email='deac@example.com', first_name='Dea', last_name='Customer',
            role=User.Role.CUSTOMER,
        )
        self.customer.customer_profile.contact_number = '09171234567'
        self.customer.customer_profile.address = '12 Real St, Baliuag'
        self.customer.customer_profile.save()
        self.client.force_login(self.customer)

    def test_deactivating_keeps_identity_fields_intact(self):
        response = self.client.post(reverse('accounts:deactivate_account_confirm'), {
            'password': 'Right-Pass-2026',
        })
        self.assertRedirects(response, reverse('accounts:login'))

        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_active)
        self.assertEqual(self.customer.username, 'deac-cust')
        self.assertEqual(self.customer.email, 'deac@example.com')
        self.assertEqual(self.customer.first_name, 'Dea')
        self.assertEqual(self.customer.customer_profile.contact_number, '09171234567')
        self.assertEqual(self.customer.customer_profile.address, '12 Real St, Baliuag')

    def test_owner_can_reactivate_a_deactivated_customer(self):
        from accounts.services import deactivate_account
        deactivate_account(self.customer)
        self.assertFalse(self.customer.is_active)

        owner = User.objects.create_user(
            username='deac-owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(owner)
        response = self.client.post(
            reverse('accounts:user_toggle_active', args=[self.customer.pk])
        )
        self.assertRedirects(response, reverse('accounts:customer_list'))

        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_active)
        self.assertEqual(self.customer.username, 'deac-cust')


class HardDeletePersonalDataTests(TestCase):
    """The Owner-triggered erasure — only ever fired for an actual request
    received outside the system. Scrubs identity, keeps everything the
    account is linked to (a real transaction FK still resolves to the same
    row, just no longer personally identifiable)."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='hd-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.customer = User.objects.create_user(
            username='hd-cust', password='pw', email='hd@example.com',
            first_name='Hard', last_name='Delete', role=User.Role.CUSTOMER,
        )
        self.customer.customer_profile.contact_number = '09171234567'
        self.customer.customer_profile.address = '12 Real St, Baliuag'
        self.customer.customer_profile.save()

    def test_hard_delete_scrubs_identity_and_sets_the_audit_pair(self):
        from accounts.services import hard_delete_personal_data

        original_pk = self.customer.pk
        hard_delete_personal_data(self.customer, performed_by=self.owner)

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.pk, original_pk)
        self.assertFalse(self.customer.is_active)
        self.assertNotEqual(self.customer.username, 'hd-cust')
        self.assertNotEqual(self.customer.email, 'hd@example.com')
        self.assertEqual(self.customer.first_name, '')
        self.assertEqual(self.customer.customer_profile.contact_number, '')
        self.assertEqual(self.customer.customer_profile.address, '')
        self.assertEqual(self.customer.deleted_by, self.owner)
        self.assertIsNotNone(self.customer.deleted_at)

    def test_hard_delete_preserves_records_the_user_created(self):
        from accounts.services import hard_delete_personal_data
        from sales.models import SalesTransaction

        txn = SalesTransaction.objects.create(
            branch=self.branch, customer=self.customer, total_amount=100,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
        )
        hard_delete_personal_data(self.customer, performed_by=self.owner)

        txn.refresh_from_db()
        self.assertEqual(txn.customer_id, self.customer.pk)
        self.assertEqual(SalesTransaction.objects.count(), 1)

    def test_deleted_addresses_are_removed(self):
        from accounts.models import Address
        from accounts.services import hard_delete_personal_data

        Address.objects.create(user=self.customer, is_default=True, text='12 Real St')
        hard_delete_personal_data(self.customer, performed_by=self.owner)

        self.assertFalse(Address.objects.filter(user=self.customer).exists())

    def test_outstanding_verification_tokens_are_invalidated(self):
        """Found live: cancelling a pending invite (hard_delete_personal_data
        on a not-yet-set-up account) scrubbed the email but left the emailed
        link itself working — anyone who still had it could open the setup
        page and complete an account for a row that had just been erased."""
        from accounts.services import hard_delete_personal_data

        VerificationToken.objects.create(
            user=self.customer, purpose=VerificationToken.PURPOSE_STAFF_INVITE,
        )
        self.assertTrue(
            VerificationToken.objects.filter(user=self.customer).exists()
        )

        hard_delete_personal_data(self.customer, performed_by=self.owner)

        self.assertFalse(
            VerificationToken.objects.filter(user=self.customer).exists()
        )

    def test_cancelling_a_pending_invite_kills_the_link(self):
        """The exact scenario reported live: Cancel Invite on a not-yet-
        completed account must leave the emailed link dead, not pointed at
        a row that's just had its email overwritten with a placeholder."""
        owner_client = Client(SERVER_NAME='localhost')
        owner_client.force_login(self.owner)
        branch = self.branch
        invited_email = 'cancel-me@example.com'
        owner_client.post(reverse('accounts:user_add'), {
            'email': invited_email, 'role': User.Role.STAFF, 'branch': branch.pk,
        })
        invited = User.objects.get(email=invited_email)
        token = VerificationToken.objects.get(
            user=invited, purpose=VerificationToken.PURPOSE_STAFF_INVITE,
        )

        owner_client.post(reverse('accounts:user_cancel_invite', args=[invited.pk]))

        response = Client(SERVER_NAME='localhost').get(
            reverse('accounts:staff_invite_complete', args=[token.token]),
            SERVER_NAME='localhost',
        )
        self.assertRedirects(response, reverse('accounts:login'))

    def test_hard_delete_refuses_a_still_pending_invite(self):
        """user_hard_delete is for a completed account (an actual privacy
        request); a not-yet-completed invite has its own dedicated cancel
        path (user_cancel_invite) that deletes the row outright instead of
        leaving a scrubbed husk behind."""
        owner_client = Client(SERVER_NAME='localhost')
        owner_client.force_login(self.owner)
        owner_client.post(reverse('accounts:user_add'), {
            'email': 'never-completed@example.com', 'role': User.Role.STAFF,
            'branch': self.branch.pk,
        })
        invited = User.objects.get(email='never-completed@example.com')

        owner_client.post(reverse('accounts:user_hard_delete', args=[invited.pk]))

        invited.refresh_from_db()
        self.assertIsNone(invited.deleted_at)
        self.assertEqual(invited.email, 'never-completed@example.com')


class CancelPendingInviteTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='cpi-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.client.force_login(self.owner)
        self.client.post(reverse('accounts:user_add'), {
            'email': 'to-cancel@example.com', 'role': User.Role.STAFF, 'branch': self.branch.pk,
        })
        self.invited = User.objects.get(email='to-cancel@example.com')

    def test_cancelling_deletes_the_row_outright(self):
        pk = self.invited.pk
        response = self.client.post(reverse('accounts:user_cancel_invite', args=[pk]))
        self.assertRedirects(response, reverse('accounts:user_list'))
        self.assertFalse(User.objects.filter(pk=pk).exists())

    def test_cancelling_cascades_the_role_profile_and_token(self):
        from accounts.models import StaffProfile
        pk = self.invited.pk
        self.client.post(reverse('accounts:user_cancel_invite', args=[pk]))
        self.assertFalse(StaffProfile.objects.filter(user_id=pk).exists())
        self.assertFalse(VerificationToken.objects.filter(user_id=pk).exists())

    def test_cannot_cancel_a_completed_account(self):
        token = VerificationToken.objects.get(
            user=self.invited, purpose=VerificationToken.PURPOSE_STAFF_INVITE,
        )
        Client(SERVER_NAME='localhost').post(
            reverse('accounts:staff_invite_complete', args=[token.token]),
            {'username': 'realstaffuser', 'first_name': 'Real', 'last_name': 'Staff',
             'password1': 'Str0ngPass!2026', 'password2': 'Str0ngPass!2026'},
            SERVER_NAME='localhost',
        )
        response = self.client.post(
            reverse('accounts:user_cancel_invite', args=[self.invited.pk])
        )
        self.assertRedirects(response, reverse('accounts:user_list'))
        self.assertTrue(User.objects.filter(pk=self.invited.pk).exists())

    def test_non_owner_cannot_cancel_invites(self):
        staff = User.objects.create_user(
            username='cpi-staff', password='pw', role=User.Role.STAFF
        )
        self.client.force_login(staff)
        pk = self.invited.pk
        self.client.post(reverse('accounts:user_cancel_invite', args=[pk]))
        self.assertTrue(User.objects.filter(pk=pk).exists())


class UserHardDeleteViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='hdv-owner', password='pw', role=User.Role.OWNER
        )
        self.customer = User.objects.create_user(
            username='hdv-cust', password='pw', email='hdv@example.com',
            role=User.Role.CUSTOMER,
        )
        self.staff = User.objects.create_user(
            username='hdv-staff', password='pw', role=User.Role.STAFF
        )
        self.client.force_login(self.owner)

    def test_owner_can_hard_delete_a_customer(self):
        response = self.client.post(
            reverse('accounts:user_hard_delete', args=[self.customer.pk])
        )
        self.assertRedirects(response, reverse('accounts:customer_list'))
        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.deleted_at)

    def test_owner_can_hard_delete_a_staff_account(self):
        response = self.client.post(
            reverse('accounts:user_hard_delete', args=[self.staff.pk])
        )
        self.assertRedirects(response, reverse('accounts:user_list'))
        self.staff.refresh_from_db()
        self.assertIsNotNone(self.staff.deleted_at)

    def test_non_owner_cannot_hard_delete_anyone(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('accounts:user_hard_delete', args=[self.customer.pk])
        )
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.deleted_at)

    def test_owner_cannot_hard_delete_their_own_account(self):
        response = self.client.post(
            reverse('accounts:user_hard_delete', args=[self.owner.pk])
        )
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.deleted_at)

    def test_deleting_an_already_deleted_account_is_a_safe_no_op(self):
        from accounts.services import hard_delete_personal_data
        hard_delete_personal_data(self.customer, performed_by=self.owner)
        first_deleted_at = self.customer.deleted_at

        response = self.client.post(
            reverse('accounts:user_hard_delete', args=[self.customer.pk])
        )
        self.assertRedirects(response, reverse('accounts:customer_list'))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.deleted_at, first_deleted_at)


class CustomerAccountsListTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='cl-owner', password='pw', role=User.Role.OWNER
        )
        self.staff = User.objects.create_user(
            username='cl-staff', password='pw', role=User.Role.STAFF
        )
        self.customer = User.objects.create_user(
            username='cl-cust', password='pw', email='clcust@example.com',
            role=User.Role.CUSTOMER,
        )

    def test_owner_sees_customers_but_not_staff(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('accounts:customer_list'))
        self.assertEqual(response.status_code, 200)
        usernames = [r['user'].username for r in response.context['rows']]
        self.assertIn('cl-cust', usernames)
        self.assertNotIn('cl-staff', usernames)

    def test_non_owner_is_refused(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('accounts:customer_list'))
        self.assertRedirects(
            response, reverse('accounts:home'), fetch_redirect_response=False
        )

    def test_search_filters_by_username(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('accounts:customer_list'), {'q': 'cl-cust'})
        usernames = [r['user'].username for r in response.context['rows']]
        self.assertEqual(usernames, ['cl-cust'])


class StaffInviteCompleteTests(TestCase):
    """The link an invited internal user follows — accounts:user_add creates
    the inactive placeholder and sends this link; this is where they
    actually get to sign in for the first time."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='inv-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.client.force_login(self.owner)
        self.client.post(reverse('accounts:user_add'), {
            'email': 'invitee@example.com', 'role': User.Role.STAFF, 'branch': self.branch.pk,
        })
        self.client.logout()
        self.invited_user = User.objects.get(email='invitee@example.com')
        self.token = VerificationToken.objects.get(
            user=self.invited_user, purpose=VerificationToken.PURPOSE_STAFF_INVITE,
        )

    def _complete(self, **overrides):
        payload = {
            'username': 'realstaffuser', 'first_name': 'Real', 'last_name': 'Staffer',
            'password1': 'Str0ngPass!2026', 'password2': 'Str0ngPass!2026',
        }
        payload.update(overrides)
        return self.client.post(
            reverse('accounts:staff_invite_complete', args=[self.token.token]), payload
        )

    def test_completing_the_invite_activates_and_logs_in(self):
        response = self._complete()
        self.assertRedirects(response, reverse('accounts:staff_dashboard'))

        self.invited_user.refresh_from_db()
        self.assertTrue(self.invited_user.is_active)
        self.assertTrue(self.invited_user.has_usable_password())
        self.assertEqual(self.invited_user.username, 'realstaffuser')
        self.assertEqual(self.invited_user.first_name, 'Real')
        self.assertTrue(self.invited_user.check_password('Str0ngPass!2026'))
        self.assertIn('_auth_user_id', self.client.session)

        self.assertEqual(self.invited_user.staff_profile.branch_id, self.branch.pk)
        self.assertEqual(self.invited_user.staff_profile.staff_name, 'Real Staffer')

    def test_email_field_is_not_editable_by_the_submission(self):
        self._complete()
        self.invited_user.refresh_from_db()
        self.assertEqual(self.invited_user.email, 'invitee@example.com')

    def test_mismatched_passwords_are_rejected(self):
        response = self._complete(password2='SomethingElse!9')
        self.assertEqual(response.status_code, 200)
        self.invited_user.refresh_from_db()
        self.assertFalse(self.invited_user.is_active)

    def test_a_taken_username_is_rejected(self):
        response = self._complete(username='inv-owner')
        self.assertEqual(response.status_code, 200)
        self.invited_user.refresh_from_db()
        self.assertFalse(self.invited_user.is_active)

    def test_the_token_is_single_use(self):
        self._complete()
        # The client is now logged in as the just-completed user — a fresh,
        # anonymous client is what actually proves the token itself, not
        # just an authenticated session, is what the second attempt hits.
        anon = Client(SERVER_NAME='localhost')
        second = anon.post(
            reverse('accounts:staff_invite_complete', args=[self.token.token]),
            {'username': 'anotherone', 'first_name': 'A', 'last_name': 'B',
             'password1': 'Str0ngPass!2026', 'password2': 'Str0ngPass!2026'},
        )
        self.assertRedirects(second, reverse('accounts:login'))

    def test_an_invalid_token_redirects_to_login(self):
        response = self.client.get(
            reverse('accounts:staff_invite_complete', args=['00000000-0000-0000-0000-000000000000'])
        )
        self.assertRedirects(response, reverse('accounts:login'))


class UserResendInviteTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='ri-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.client.force_login(self.owner)
        self.client.post(reverse('accounts:user_add'), {
            'email': 'pending@example.com', 'role': User.Role.STAFF, 'branch': self.branch.pk,
        })
        self.invited_user = User.objects.get(email='pending@example.com')

    def test_resending_invalidates_the_old_token_and_sends_a_new_one(self):
        old_token = VerificationToken.objects.get(
            user=self.invited_user, purpose=VerificationToken.PURPOSE_STAFF_INVITE,
        )
        response = self.client.post(
            reverse('accounts:user_resend_invite', args=[self.invited_user.pk])
        )
        self.assertRedirects(response, reverse('accounts:user_list'))
        self.assertEqual(len(mail.outbox), 2)  # one from Add User, one from resend

        self.assertFalse(
            VerificationToken.objects.filter(pk=old_token.pk).exists()
        )
        new_token = VerificationToken.objects.get(
            user=self.invited_user, purpose=VerificationToken.PURPOSE_STAFF_INVITE,
        )
        self.assertTrue(new_token.is_valid)

    def test_cannot_resend_to_an_already_set_up_account(self):
        token = VerificationToken.objects.get(
            user=self.invited_user, purpose=VerificationToken.PURPOSE_STAFF_INVITE,
        )
        Client(SERVER_NAME='localhost').post(
            reverse('accounts:staff_invite_complete', args=[token.token]),
            {'username': 'realuser', 'first_name': 'Real', 'last_name': 'Person',
             'password1': 'Str0ngPass!2026', 'password2': 'Str0ngPass!2026'},
            SERVER_NAME='localhost',
        )
        response = self.client.post(
            reverse('accounts:user_resend_invite', args=[self.invited_user.pk])
        )
        self.assertRedirects(response, reverse('accounts:user_list'))
        self.assertEqual(len(mail.outbox), 1)  # no second email sent

    def test_cannot_resend_a_cancelled_invite(self):
        """The exact gap the live bug came through: cancelling used to only
        scrub the row (has_usable_password() stayed False, identical to a
        still-pending row), so Resend could hand out a fresh, working setup
        link for an identity that had already been erased. Cancelling now
        deletes the row outright, which closes the gap by construction —
        there's nothing left for Resend to find."""
        pk = self.invited_user.pk
        self.client.post(reverse('accounts:user_cancel_invite', args=[pk]))
        self.assertFalse(User.objects.filter(pk=pk).exists())

        response = self.client.post(
            reverse('accounts:user_resend_invite', args=[pk])
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(mail.outbox), 1)  # no invite email sent for a cancelled row
