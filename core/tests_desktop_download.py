"""The Windows desktop app's footprint on the server: the staff-only download
card and the Sessions page label. The app itself lives in desktop/."""
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.device_info import describe_user_agent
from accounts.models import User

INSTALLER = 'https://example.com/releases/GerlynPLUS-Setup-1.0.0.exe'
CHROME_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
)


class DesktopDownloadCardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='staff1', password='Passw0rd-', role='STAFF')
        self.customer = User.objects.create_user(username='cust1', password='Passw0rd-', role='CUSTOMER')
        self.url = reverse('download')

    @override_settings(DESKTOP_APP_DOWNLOAD_URL=INSTALLER)
    def test_staff_see_the_download_link(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="desktop-app-card"')
        self.assertContains(response, f'href="{INSTALLER}"')

    @override_settings(DESKTOP_APP_DOWNLOAD_URL=INSTALLER)
    def test_customers_and_anonymous_visitors_do_not(self):
        self.assertNotContains(self.client.get(self.url), 'desktop-app-card')
        self.client.force_login(self.customer)
        self.assertNotContains(self.client.get(self.url), 'desktop-app-card')

    @override_settings(DESKTOP_APP_DOWNLOAD_URL='')
    def test_hidden_when_no_url_is_configured(self):
        self.client.force_login(self.staff)
        self.assertNotContains(self.client.get(self.url), 'desktop-app-card')


PRIVATE_B2 = dict(
    DESKTOP_APP_B2_ENDPOINT='https://s3.us-west-004.backblazeb2.com',
    DESKTOP_APP_B2_BUCKET='gerlyn-installers',
    DESKTOP_APP_B2_KEY_ID='004keyid',
    DESKTOP_APP_B2_APPLICATION_KEY='secret-application-key',
    DESKTOP_APP_B2_FILE='desktop/GerlynPLUS-Setup-1.0.0.exe',
)


@override_settings(**PRIVATE_B2)
class PrivateInstallerTests(TestCase):
    """The installer in a private bucket, reachable only through the site."""

    def setUp(self):
        self.url = reverse('desktop_download')
        self.staff = User.objects.create_user(username='staff2', password='Passw0rd-', role='STAFF')
        self.manager = User.objects.create_user(username='mgr2', password='Passw0rd-', role='BRANCH_MANAGER')
        self.customer = User.objects.create_user(username='cust2', password='Passw0rd-', role='CUSTOMER')

    def test_card_points_at_the_protected_view_not_the_bucket(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('download'))
        self.assertContains(response, 'id="desktop-app-card"')
        self.assertContains(response, f'href="{self.url}"')
        self.assertNotContains(response, 'backblazeb2.com')

    @override_settings(DESKTOP_APP_DOWNLOAD_URL=INSTALLER)
    def test_private_bucket_wins_over_the_public_link(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('download'))
        self.assertContains(response, f'href="{self.url}"')
        self.assertNotContains(response, INSTALLER)

    def test_staff_and_managers_get_a_short_lived_signed_link(self):
        for user in (self.staff, self.manager):
            self.client.force_login(user)
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 302)
            location = response['Location']
            self.assertTrue(location.startswith(
                'https://s3.us-west-004.backblazeb2.com/gerlyn-installers/desktop/GerlynPLUS-Setup-1.0.0.exe'
            ) or location.startswith(
                'https://gerlyn-installers.s3.us-west-004.backblazeb2.com/desktop/GerlynPLUS-Setup-1.0.0.exe'
            ), location)
            self.assertIn('X-Amz-Signature=', location)
            self.assertIn('X-Amz-Expires=300', location)
            self.assertIn('GerlynPLUS-Setup-1.0.0.exe', location)
            self.assertIn('attachment', location)
            self.assertNotIn('secret-application-key', location)
            # A cached redirect would replay an expired link.
            self.assertIn('no-store', response['Cache-Control'])

    def test_anonymous_visitors_are_sent_to_sign_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('backblazeb2.com', response['Location'])

    def test_customers_are_refused(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_a_signing_failure_returns_to_the_download_page(self):
        self.client.force_login(self.staff)
        with patch('core.views.signed_installer_url', side_effect=RuntimeError('boom')):
            response = self.client.get(self.url, follow=True)
        self.assertRedirects(response, reverse('download'))
        self.assertContains(response, 'could not be fetched')


class InstallerNotConfiguredTests(TestCase):
    def setUp(self):
        self.url = reverse('desktop_download')
        self.staff = User.objects.create_user(username='staff3', password='Passw0rd-', role='STAFF')
        self.client.force_login(self.staff)

    @override_settings(DESKTOP_APP_DOWNLOAD_URL='')
    def test_nothing_configured_is_a_404(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)

    @override_settings(DESKTOP_APP_DOWNLOAD_URL=INSTALLER)
    def test_falls_back_to_the_public_link(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, INSTALLER, fetch_redirect_response=False)

    @override_settings(DESKTOP_APP_DOWNLOAD_URL='', DESKTOP_APP_B2_BUCKET='b', DESKTOP_APP_B2_FILE='x.exe')
    def test_half_configured_private_bucket_stays_off(self):
        response = self.client.get(reverse('download'))
        self.assertNotContains(response, 'desktop-app-card')
        self.assertEqual(self.client.get(self.url).status_code, 404)


class DesktopUserAgentLabelTests(TestCase):
    def test_desktop_app_is_named_in_sessions(self):
        ua = f'{CHROME_UA} GerlynPLUS-Desktop/1.0.0'
        self.assertEqual(describe_user_agent(ua), 'Windows · GerlynPLUS Desktop')

    def test_ordinary_chrome_is_unchanged(self):
        self.assertEqual(describe_user_agent(CHROME_UA), 'Windows · Chrome')
