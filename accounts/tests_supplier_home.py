"""A supplier who opens "/" (reopening the site, the PWA start_url) must land
on their portal, not the customer storefront — from the storefront none of a
supplier's links work, so they could not even reach Logout."""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User


class SupplierHomeRedirectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.supplier = User.objects.create_user(
            username='home-supplier', password='pw', role=User.Role.SUPPLIER)
        cls.customer = User.objects.create_user(
            username='home-customer', password='pw', role=User.Role.CUSTOMER)

    def test_supplier_is_sent_to_their_dashboard_and_on_to_the_portal(self):
        self.client.force_login(self.supplier)
        response = self.client.get(reverse('accounts:home'))
        self.assertRedirects(
            response, reverse('accounts:supplier_dashboard'),
            fetch_redirect_response=False)
        followed = self.client.get(reverse('accounts:home'), follow=True)
        self.assertEqual(followed.redirect_chain[-1][0], reverse('supplier_portal'))
        self.assertEqual(followed.status_code, 200)
        self.assertContains(followed, reverse('accounts:logout'))

    def test_customers_and_visitors_still_see_the_storefront(self):
        self.assertEqual(self.client.get(reverse('accounts:home')).status_code, 200)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(reverse('accounts:home')).status_code, 200)
