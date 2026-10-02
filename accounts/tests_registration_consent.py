"""Terms & Privacy consent at sign-up.

The checkbox's `required` attribute is only a browser nicety, so every test
here goes straight at the server: register_view and POST /api/auth/register/
must both refuse an account without an explicit "yes", and record when it was
given (time + policy version, never the policy text) when it is.
"""
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomerProfile, User
from audit.models import AuditLog


class RegisterConsentTests(TestCase):
    def setUp(self):
        # register_view is rate-limited per IP; start every test clean.
        cache.clear()

    def _register(self, **overrides):
        payload = {
            'username': 'consentcustomer',
            'email': 'consent@example.com',
            'password1': 'Str0ngPass!2026',
            'password2': 'Str0ngPass!2026',
            'first_name': 'Consent',
            'last_name': 'Customer',
            'contact_number': '',
            'address': '12 Real St, Poblacion, Baliuag, Bulacan',
            'accept_terms': '1',
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(reverse('accounts:register'), payload)

    def test_form_shows_a_required_consent_box_linking_to_the_public_policy(self):
        response = self.client.get(reverse('accounts:register'))
        self.assertRegex(
            response.content.decode(),
            r'<input id="accept_terms" type="checkbox" name="accept_terms" value="1"\s+required>',
        )
        policy_url = reverse('accounts:terms_privacy')
        self.assertContains(response, f'href="{policy_url}#terms"')
        self.assertContains(response, f'href="{policy_url}#privacy"')

    def test_missing_consent_is_refused_server_side(self):
        response = self._register(accept_terms=None)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='consentcustomer').exists())
        self.assertContains(response, 'Please tick the box to agree')
        # The rest of what they typed survives, so only the box needs fixing.
        self.assertContains(response, 'value="consentcustomer"')

    def test_only_an_explicit_yes_counts(self):
        for value in ('', '0', 'false', 'off'):
            with self.subTest(value=value):
                response = self._register(accept_terms=value)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(User.objects.filter(username='consentcustomer').exists())

    def test_consent_does_not_bypass_existing_validation(self):
        response = self._register(password2='Different!2026')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='consentcustomer').exists())

    def test_accepted_consent_is_recorded_with_time_and_version(self):
        response = self._register()
        self.assertRedirects(response, reverse('accounts:customer_dashboard'))
        profile = User.objects.get(username='consentcustomer').customer_profile
        self.assertIsNotNone(profile.terms_accepted_at)
        self.assertEqual(profile.terms_version, CustomerProfile.TERMS_VERSION)

    def test_audit_log_keeps_personal_fields_masked_and_no_policy_text(self):
        self._register()
        profile = User.objects.get(username='consentcustomer').customer_profile
        entries = AuditLog.objects.filter(
            entity__icontains='customerprofile', record_id=str(profile.pk),
        )
        self.assertTrue(entries.exists())
        for entry in entries:
            dumped = f'{entry.before} {entry.after} {entry.summary}'
            self.assertNotIn('Consent', dumped)
            self.assertNotIn('Poblacion', dumped)
            self.assertNotIn('Terms & Conditions', dumped)


class RegisterApiConsentTests(TestCase):
    def _post(self, **extra):
        body = {
            'username': 'apiconsent',
            'email': 'apiconsent@example.com',
            'password': 'Str0ngPass!2026',
            'fullName': 'Api Consent',
            'address': '12 Real St, Baliuag, Bulacan',
        }
        body.update(extra)
        return self.client.post('/api/auth/register/', body, content_type='application/json')

    def test_api_refuses_without_explicit_consent(self):
        for extra in ({}, {'acceptTerms': False}, {'acceptTerms': 'false'}):
            with self.subTest(extra=extra):
                response = self._post(**extra)
                self.assertEqual(response.status_code, 400)
                self.assertIn('acceptTerms', response.json())
                self.assertFalse(User.objects.filter(username='apiconsent').exists())

    def test_api_records_consent(self):
        response = self._post(acceptTerms=True)
        self.assertEqual(response.status_code, 201)
        profile = User.objects.get(username='apiconsent').customer_profile
        self.assertIsNotNone(profile.terms_accepted_at)
        self.assertEqual(profile.terms_version, CustomerProfile.TERMS_VERSION)


class ExistingAccountsTests(TestCase):
    def test_accounts_created_outside_sign_up_have_no_consent_on_record(self):
        """What the migration leaves on every pre-existing profile: blank,
        not back-filled with a consent nobody actually gave."""
        user = User.objects.create_user(
            username='legacy', password='Str0ngPass!2026', role=User.Role.CUSTOMER,
        )
        profile = user.customer_profile
        self.assertIsNone(profile.terms_accepted_at)
        self.assertEqual(profile.terms_version, '')

    def test_existing_customer_can_still_log_in(self):
        User.objects.create_user(
            username='legacy', password='Str0ngPass!2026', role=User.Role.CUSTOMER,
        )
        self.assertTrue(self.client.login(username='legacy', password='Str0ngPass!2026'))


class TermsPrivacyPageTests(TestCase):
    def test_policy_page_is_public_and_has_both_anchors(self):
        response = self.client.get(reverse('accounts:terms_privacy'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="terms"')
        self.assertContains(response, 'id="privacy"')
        self.assertContains(response, 'Republic Act No. 10173')
        self.assertContains(response, CustomerProfile.TERMS_VERSION)
