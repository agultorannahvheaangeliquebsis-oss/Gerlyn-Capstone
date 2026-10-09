from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .forms import PasswordResetSetPasswordForm
from .models import User
from .rate_limit import PASSWORD_RESET_EMAIL_MAX_ATTEMPTS


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='GerlynPLUS <noreply@example.com>',
    PUBLIC_BASE_URL='https://shop.example.com',
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
)
class PasswordResetFlowTests(TestCase):
    old_password = 'OldPassword!2026'
    new_password = 'NewPassword!2026'

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(
            username='reset-customer',
            email='customer@example.com',
            password=self.old_password,
            role=User.Role.CUSTOMER,
            allowed_modules=['sales', 'inventory'],
            module_actions={'sales': ['add']},
        )

    def _raw_reset_url(self, token=None):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = token or default_token_generator.make_token(self.user)
        return reverse('accounts:password_reset_confirm', args=[uid, token])

    def _open_valid_reset_form(self, token=None):
        response = self.client.get(self._raw_reset_url(token))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/set-password/', response.url)
        return response.url

    def _complete_reset(self):
        form_url = self._open_valid_reset_form()
        return self.client.post(form_url, {
            'new_password1': self.new_password,
            'new_password2': self.new_password,
        })

    def test_login_page_links_to_forgot_password(self):
        response = self.client.get(reverse('accounts:login'))
        self.assertContains(response, reverse('accounts:forgot_password'))
        self.assertContains(response, 'Forgot password?')

    def test_registered_email_receives_30_minute_single_use_https_link(self):
        response = self.client.post(
            reverse('accounts:forgot_password'),
            {'email': self.user.email},
        )

        self.assertRedirects(
            response,
            f"{reverse('accounts:forgot_password')}?sent=1",
        )
        self.assertEqual(settings.PASSWORD_RESET_TIMEOUT, 30 * 60)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.user.email])
        self.assertIn('GerlynPLUS', message.subject)
        self.assertIn('https://shop.example.com/reset-password/', message.body)
        self.assertIn('expires in 30 minutes', message.body)
        self.assertIn('did not request', message.body)
        self.assertNotIn('http://', message.body)

    def test_unknown_email_has_same_public_response_and_sends_nothing(self):
        response = self.client.post(
            reverse('accounts:forgot_password'),
            {'email': 'unknown@example.com'},
        )

        self.assertRedirects(
            response,
            f"{reverse('accounts:forgot_password')}?sent=1",
        )
        self.assertEqual(len(mail.outbox), 0)
        confirmation = self.client.get(response.url)
        self.assertContains(
            confirmation,
            'If an account is registered for that email address',
        )
        self.assertNotContains(confirmation, 'unknown@example.com')

    def test_reset_requests_are_rate_limited_without_disclosing_the_limit(self):
        responses = [
            self.client.post(
                reverse('accounts:forgot_password'),
                {'email': self.user.email},
            )
            for _ in range(PASSWORD_RESET_EMAIL_MAX_ATTEMPTS + 2)
        ]

        self.assertEqual(len(mail.outbox), PASSWORD_RESET_EMAIL_MAX_ATTEMPTS)
        self.assertTrue(all(response.status_code == 302 for response in responses))
        self.assertEqual(len({response.url for response in responses}), 1)

    def test_email_delivery_error_is_safe_and_does_not_log_email_or_token(self):
        with patch.object(
            default_token_generator,
            'make_token',
            return_value='secret-reset-token',
        ), patch(
            'django.core.mail.backends.locmem.EmailBackend.send_messages',
            side_effect=RuntimeError('provider included a secret'),
        ), self.assertLogs('accounts.email', level='ERROR') as captured:
            response = self.client.post(
                reverse('accounts:forgot_password'),
                {'email': self.user.email},
            )

        self.assertRedirects(
            response,
            f"{reverse('accounts:forgot_password')}?sent=1",
        )
        logs = '\n'.join(captured.output)
        self.assertIn('Password reset email delivery failed', logs)
        self.assertNotIn(self.user.email, logs)
        self.assertNotIn('secret-reset-token', logs)
        self.assertNotIn('provider included a secret', logs)

    def test_valid_token_resets_password_without_login_or_permission_changes(self):
        role = self.user.role
        modules = deepcopy(self.user.allowed_modules)
        actions = deepcopy(self.user.module_actions)

        response = self._complete_reset()

        self.assertRedirects(response, reverse('accounts:password_reset_complete'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))
        self.assertFalse(self.user.check_password(self.old_password))
        self.assertEqual(self.user.role, role)
        self.assertEqual(self.user.allowed_modules, modules)
        self.assertEqual(self.user.module_actions, actions)
        self.assertNotIn(SESSION_KEY, self.client.session)

        new_login = Client().post(reverse('accounts:login'), {
            'username': self.user.username,
            'password': self.new_password,
        })
        self.assertEqual(new_login.status_code, 302)

        old_login = Client().post(reverse('accounts:login'), {
            'username': self.user.username,
            'password': self.old_password,
        })
        self.assertEqual(old_login.status_code, 200)
        self.assertContains(old_login, 'Invalid username or password.')

    def test_previously_used_token_is_rejected(self):
        token = default_token_generator.make_token(self.user)
        form_url = self._open_valid_reset_form(token)
        response = self.client.post(form_url, {
            'new_password1': self.new_password,
            'new_password2': self.new_password,
        })
        self.assertRedirects(response, reverse('accounts:password_reset_complete'))

        reused = self.client.get(self._raw_reset_url(token))
        self.assertEqual(reused.status_code, 200)
        self.assertContains(reused, 'invalid, expired, or has already been used')

    def test_expired_token_is_rejected(self):
        with patch.object(
            default_token_generator,
            '_now',
            return_value=datetime.now() - timedelta(minutes=31),
        ):
            expired_token = default_token_generator.make_token(self.user)

        response = self.client.get(self._raw_reset_url(expired_token))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'invalid, expired, or has already been used')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))

    def test_password_mismatch_displays_error_without_changing_password(self):
        form_url = self._open_valid_reset_form()
        response = self.client.post(form_url, {
            'new_password1': self.new_password,
            'new_password2': 'DifferentPassword!2026',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'didn’t match')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))

    def test_weak_password_is_rejected_by_configured_validators(self):
        form_url = self._open_valid_reset_form()
        response = self.client.post(form_url, {
            'new_password1': 'alllowercase1!',
            'new_password2': 'alllowercase1!',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'uppercase letter')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))

    def test_reset_preserves_every_supported_role_and_permission_payload(self):
        for index, role in enumerate(User.Role.values):
            with self.subTest(role=role):
                user = User.objects.create_user(
                    username=f'role-reset-{index}',
                    email=f'role-reset-{index}@example.com',
                    password=self.old_password,
                    role=role,
                    allowed_modules=['sales'],
                    module_actions={'sales': ['view', 'add']},
                )
                original_modules = deepcopy(user.allowed_modules)
                original_actions = deepcopy(user.module_actions)
                form = PasswordResetSetPasswordForm(user, {
                    'new_password1': self.new_password,
                    'new_password2': self.new_password,
                })

                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                user.refresh_from_db()
                self.assertEqual(user.role, role)
                self.assertEqual(user.allowed_modules, original_modules)
                self.assertEqual(user.module_actions, original_actions)
