"""Add/Edit User, Supplier and Branch moved from their own pages into modals
on their list pages. The forms' save logic was left alone — these cover the
two things the move did change (the GET route and the error path), plus the
writes themselves, so a regression in either shows up here rather than in a
browser.
"""
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, User, VerificationToken
from accounts.rate_limit import (
    LOGIN_MAX_ATTEMPTS,
    clear_failed_logins,
    is_login_locked_out,
    register_failed_login,
)
from branches.models import Branch


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

    def test_creating_a_manager_sets_their_branch(self):
        response = self.client.post(reverse('accounts:user_add'), {
            # StrongPasswordValidator (uppercase + digit + symbol) is applied
            # on create — the old 'pw123456' no longer passes.
            'username': 'carlo', 'password': 'Vault-7788x',
            'role': User.Role.BRANCH_MANAGER, 'branch': self.branch.pk,
            'first_name': 'Carlo', 'last_name': 'Reyes', 'email': 'c@x.ph',
        })
        self.assertRedirects(response, reverse('accounts:user_list'))
        carlo = User.objects.get(username='carlo')
        self.assertEqual(carlo.role, User.Role.BRANCH_MANAGER)
        profile = BranchManagerProfile.objects.get(user=carlo)
        self.assertEqual(profile.branch_id, self.branch.pk)
        self.assertEqual(profile.manager_name, 'Carlo Reyes')

    def test_missing_password_reopens_the_modal_and_keeps_the_fields(self):
        response = self.client.post(reverse('accounts:user_add'), {
            'username': 'carlo', 'password': '',
            'role': User.Role.STAFF, 'first_name': 'Carlo',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertIn(
            'A password is required for a new user.', response.context['add_errors']
        )
        self.assertContains(response, 'value="carlo"')
        self.assertFalse(User.objects.filter(username='carlo').exists())

    def test_a_rejected_submit_never_writes_the_password_back(self):
        response = self.client.post(reverse('accounts:user_add'), {
            'username': '', 'password': 'sup3rsecret',
            'role': User.Role.STAFF,
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'sup3rsecret')

    def test_duplicate_username_is_rejected(self):
        response = self.client.post(reverse('accounts:user_add'), {
            'username': 'owner', 'password': 'pw123456', 'role': User.Role.STAFF,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertEqual(User.objects.filter(username='owner').count(), 1)

    def test_a_non_internal_role_is_still_refused(self):
        response = self.client.post(reverse('accounts:user_add'), {
            'username': 'shopper', 'password': 'pw123456',
            'role': User.Role.CUSTOMER,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='shopper').exists())

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
