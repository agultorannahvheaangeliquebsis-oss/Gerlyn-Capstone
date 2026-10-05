"""Production-hardening regressions: report access, Django admin exposure,
styled error pages, and production settings parsing.

Run with:
    python manage.py test core.tests_production_hardening
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.views.defaults import bad_request, page_not_found, permission_denied, server_error

from accounts.models import User
from core.module_permissions import REPORTS, module_access_map

BASE_DIR = Path(settings.BASE_DIR)

REPORT_URL_NAMES = (
    'reports_dashboard', 'sales_report', 'inventory_report',
    'procurement_report', 'billing_report',
)


def _login(username):
    client = Client()
    client.force_login(User.objects.get(username=username))
    return client


class ReportAccessTests(TestCase):
    """/reports/ stays protected: Owner + Branch Manager only."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    def test_owner_can_open_every_report_page(self):
        client = _login('gerlyn')
        for name in REPORT_URL_NAMES:
            with self.subTest(name=name):
                self.assertEqual(client.get(reverse(name)).status_code, 200)
        self.assertEqual(client.get(reverse('export_csv', args=['sales'])).status_code, 200)

    def test_branch_manager_can_open_every_report_page(self):
        client = _login('carlo')
        for name in REPORT_URL_NAMES:
            with self.subTest(name=name):
                self.assertEqual(client.get(reverse(name)).status_code, 200)

    def test_branch_manager_only_sees_their_own_branch(self):
        carlo = User.objects.get(username='carlo')
        branch_id = carlo.manager_profile.branch_id
        rows = list(_login('carlo').get(reverse('sales_report')).context['transactions'])
        self.assertTrue(rows, 'seed data should give the manager at least one sale')
        self.assertEqual({t.branch_id for t in rows}, {branch_id})

        owner_rows = list(_login('gerlyn').get(reverse('sales_report')).context['transactions'])
        self.assertGreater(len(owner_rows), len(rows))

    def test_other_roles_get_the_styled_403_not_a_raw_one(self):
        # ana = Staff, century = Supplier, juan = Customer
        for username in ('ana', 'century', 'juan'):
            client = _login(username)
            for name in REPORT_URL_NAMES:
                with self.subTest(user=username, page=name):
                    response = client.get(reverse(name))
                    self.assertEqual(response.status_code, 403)
                    self.assertContains(response, 'GerlynPLUS', status_code=403)
                    self.assertContains(
                        response, "You don't have access to this page", status_code=403,
                    )
            with self.subTest(user=username, page='export_csv'):
                response = client.get(reverse('export_csv', args=['sales']))
                self.assertEqual(response.status_code, 403)
                self.assertNotIn('text/csv', response['Content-Type'])

    def test_staff_stays_denied_even_with_an_owner_granted_reports_module(self):
        ana = User.objects.get(username='ana')
        ana.allowed_modules = list(ana.allowed_modules or []) + [REPORTS]
        ana.save()
        response = _login('ana').get(reverse('reports_dashboard'))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(module_access_map(ana)[REPORTS])

    def test_anonymous_is_sent_to_login_and_returned_afterwards(self):
        for name in REPORT_URL_NAMES:
            with self.subTest(name=name):
                url = reverse(name)
                response = self.client.get(url)
                self.assertRedirects(
                    response, f"{settings.LOGIN_URL}?next={url}", fetch_redirect_response=False,
                )

    def test_reports_nav_link_is_only_shown_to_roles_that_can_open_it(self):
        reports_href = f'href="{reverse("reports_dashboard")}"'
        self.assertContains(_login('gerlyn').get(reverse('reports_dashboard')), reports_href)
        self.assertContains(_login('carlo').get(reverse('reports_dashboard')), reports_href)
        for username in ('ana', 'century', 'juan'):
            user = User.objects.get(username=username)
            with self.subTest(user=username):
                self.assertFalse(module_access_map(user)[REPORTS])

    def test_reports_api_stays_denied_for_non_management_roles(self):
        for username in ('ana', 'century', 'juan'):
            with self.subTest(user=username):
                response = _login(username).get('/api/reports/dashboard-summary/')
                self.assertEqual(response.status_code, 403)


def _urlconf_copy(name, **setting_overrides):
    """Execute gerlyn_is/urls.py afresh under overridden settings.

    The admin route is decided when the URLconf module runs, so each case
    needs its own copy rather than the one Django already imported.
    """
    path = BASE_DIR / 'gerlyn_is' / 'urls.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with override_settings(**setting_overrides):
        spec.loader.exec_module(module)
    sys.modules[name] = module
    return module


class AdminExposureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    def tearDown(self):
        for name in ('urls_admin_off', 'urls_admin_on', 'urls_admin_moved'):
            sys.modules.pop(name, None)

    def test_admin_route_does_not_exist_when_disabled(self):
        _urlconf_copy('urls_admin_off', ADMIN_ENABLED=False, ADMIN_URL='admin/')
        with override_settings(ROOT_URLCONF='urls_admin_off'):
            for user in (None, 'gerlyn', 'carlo', 'juan'):
                client = self.client if user is None else _login(user)
                with self.subTest(user=user):
                    self.assertEqual(client.get('/admin/').status_code, 404)
                    self.assertEqual(client.get('/admin/login/').status_code, 404)

    def test_admin_url_can_be_moved_and_the_old_path_is_gone(self):
        _urlconf_copy('urls_admin_moved', ADMIN_ENABLED=True, ADMIN_URL='ops-console-x9/')
        with override_settings(ROOT_URLCONF='urls_admin_moved'):
            self.assertEqual(self.client.get('/admin/').status_code, 404)
            self.assertEqual(self.client.get('/ops-console-x9/').status_code, 302)

    def test_enabled_admin_admits_only_real_superusers(self):
        _urlconf_copy('urls_admin_on', ADMIN_ENABLED=True, ADMIN_URL='admin/')
        with override_settings(ROOT_URLCONF='urls_admin_on'):
            # Business roles, even with is_staff flipped on, are refused.
            for username in ('carlo', 'ana', 'juan', 'century'):
                user = User.objects.get(username=username)
                user.is_staff = True
                user.is_superuser = False
                user.save()
                with self.subTest(user=username):
                    response = _login(username).get('/admin/')
                    self.assertEqual(response.status_code, 302)
                    self.assertIn('/admin/login/', response['Location'])
            # An Owner is only let in because the seed makes them a superuser.
            owner = User.objects.get(username='gerlyn')
            self.assertTrue(owner.is_superuser)
            self.assertEqual(_login('gerlyn').get('/admin/').status_code, 200)

    def test_admin_site_class_requires_is_superuser(self):
        request = RequestFactory().get('/admin/')
        request.user = User.objects.get(username='carlo')
        request.user.is_staff = True
        request.user.is_superuser = False
        self.assertFalse(admin.site.has_permission(request))
        request.user.is_superuser = True
        self.assertTrue(admin.site.has_permission(request))


class ErrorPageTests(TestCase):
    """404/403/400/500 render the GerlynPLUS page and leak nothing."""

    LEAKS = ('URLconf', 'urlpatterns', 'Request Method', 'Traceback', 'settings.py',
             'DEBUG = True', 'You\'re seeing this error')

    def assertClean(self, response, status):
        self.assertEqual(response.status_code, status)
        body = response.content.decode()
        self.assertIn('GerlynPLUS', body)
        for leak in self.LEAKS:
            self.assertNotIn(leak, body)

    def test_unknown_url_is_a_styled_404_without_url_patterns(self):
        for path in ('/hello', '/hello/', '/reports/nope/', '/admin-panel/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertClean(response, 404)
                self.assertContains(response, 'Page not found', status_code=404)
                self.assertNotContains(response, 'healthz', status_code=404)

    def test_handlers_render_the_styled_templates(self):
        request = RequestFactory().get('/anything')
        self.assertClean(bad_request(request, Exception('boom')), 400)
        self.assertClean(permission_denied(request, PermissionDenied()), 403)
        self.assertClean(page_not_found(request, Exception('nope')), 404)
        self.assertClean(server_error(request), 500)

    def test_server_error_does_not_leak_the_exception(self):
        client = Client(raise_request_exception=False)
        with override_settings(ROOT_URLCONF='core.tests_production_hardening'):
            response = client.get('/boom/')
        self.assertClean(response, 500)
        self.assertNotContains(response, 'SECRET-INTERNAL-DETAIL', status_code=500)

    def test_csrf_failure_uses_the_styled_page(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse('accounts:login'), {'username': 'x', 'password': 'y'})
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'GerlynPLUS', status_code=403)
        self.assertNotContains(response, 'CSRF token', status_code=403)


def _boom(request):
    raise RuntimeError('SECRET-INTERNAL-DETAIL')


from django.urls import path as _path  # noqa: E402

# Tiny URLconf used by test_server_error_does_not_leak_the_exception.
urlpatterns = [_path('boom/', _boom)]
handler500 = 'django.views.defaults.server_error'


class ProductionSettingsTests(SimpleTestCase):
    """Settings are evaluated in a subprocess so env vars can be varied."""

    # settings.py loads .env with setdefault(), so anything not pinned here would
    # silently come from the developer's own file. Pin every variable that the
    # assertions depend on (a blank value counts as "set", and as False).
    PINNED = {
        'DJANGO_DEBUG': '', 'DJANGO_ALLOWED_HOSTS': 'localhost', 'DJANGO_CSRF_TRUSTED': '',
        'DJANGO_ADMIN_ENABLED': '', 'DJANGO_ADMIN_URL': '', 'DJANGO_SECURE_SSL': '',
        'DJANGO_SECRET_KEY': 'x' * 60, 'DB_PASSWORD': 'unused',
        'RAILWAY_PUBLIC_DOMAIN': '', 'RAILWAY_ENVIRONMENT_NAME': '', 'RAILWAY_ENVIRONMENT': '',
    }

    def _settings(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith(('GEOCODING_', 'MAP_TILE_'))}
        base.update(self.PINNED)
        base.update(env)
        code = (
            'import json, gerlyn_is.settings as s;'
            'print("@@" + json.dumps({k: getattr(s, k) for k in ('
            '"DEBUG","ALLOWED_HOSTS","CSRF_TRUSTED_ORIGINS","ADMIN_ENABLED","ADMIN_URL",'
            '"SECURE_SSL_REDIRECT","SESSION_COOKIE_SECURE","GEOCODING_SEARCH_URL",'
            '"GEOCODING_REVERSE_URL","MAP_TILE_URL_TEMPLATE") if hasattr(s, k)}))'
        )
        out = subprocess.run(
            [sys.executable, '-c', code], cwd=BASE_DIR, env=base,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        line = next(l for l in out.stdout.splitlines() if l.startswith('@@'))
        return json.loads(line[2:])

    def test_the_string_false_is_not_truthy(self):
        for value in ('False', 'false', '0', 'no', ''):
            with self.subTest(value=value):
                cfg = self._settings(DJANGO_DEBUG=value)
                self.assertIs(cfg['DEBUG'], False)

    def test_railway_forces_debug_off_and_configures_hosts_and_csrf(self):
        cfg = self._settings(
            DJANGO_DEBUG='True', RAILWAY_PUBLIC_DOMAIN='demo.up.railway.app',
            RAILWAY_ENVIRONMENT_NAME='production',
        )
        self.assertIs(cfg['DEBUG'], False)
        self.assertIn('demo.up.railway.app', cfg['ALLOWED_HOSTS'])
        self.assertIn('healthcheck.railway.app', cfg['ALLOWED_HOSTS'])
        self.assertNotIn('*', cfg['ALLOWED_HOSTS'])
        self.assertIn('https://demo.up.railway.app', cfg['CSRF_TRUSTED_ORIGINS'])
        self.assertTrue(cfg['SECURE_SSL_REDIRECT'])
        self.assertTrue(cfg['SESSION_COOKIE_SECURE'])
        self.assertFalse(cfg['ADMIN_ENABLED'])

    def test_admin_is_on_for_local_debug_and_off_otherwise(self):
        self.assertTrue(self._settings(DJANGO_DEBUG='True')['ADMIN_ENABLED'])
        self.assertFalse(self._settings()['ADMIN_ENABLED'])
        cfg = self._settings(DJANGO_ADMIN_ENABLED='True', DJANGO_ADMIN_URL='/ops/')
        self.assertTrue(cfg['ADMIN_ENABLED'])
        self.assertEqual(cfg['ADMIN_URL'], 'ops/')

    def test_geocoding_defaults_to_osm_and_blank_env_values_do_not_disable_it(self):
        cfg = self._settings(
            GEOCODING_SEARCH_URL='', GEOCODING_REVERSE_URL='', MAP_TILE_URL_TEMPLATE='',
        )
        self.assertEqual(cfg['GEOCODING_SEARCH_URL'], 'https://nominatim.openstreetmap.org/search')
        self.assertEqual(cfg['GEOCODING_REVERSE_URL'], 'https://nominatim.openstreetmap.org/reverse')
        self.assertTrue(cfg['MAP_TILE_URL_TEMPLATE'].startswith('https://tile.openstreetmap.org/'))

    def test_geocoding_provider_can_be_overridden(self):
        cfg = self._settings(GEOCODING_SEARCH_URL='https://geo.example/search')
        self.assertEqual(cfg['GEOCODING_SEARCH_URL'], 'https://geo.example/search')
