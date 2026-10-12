"""The Windows desktop app's footprint on the server: the staff-only download
card and the Sessions page label. The app itself lives in desktop/."""
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


class DesktopUserAgentLabelTests(TestCase):
    def test_desktop_app_is_named_in_sessions(self):
        ua = f'{CHROME_UA} GerlynPLUS-Desktop/1.0.0'
        self.assertEqual(describe_user_agent(ua), 'Windows · GerlynPLUS Desktop')

    def test_ordinary_chrome_is_unchanged(self):
        self.assertEqual(describe_user_agent(CHROME_UA), 'Windows · Chrome')
