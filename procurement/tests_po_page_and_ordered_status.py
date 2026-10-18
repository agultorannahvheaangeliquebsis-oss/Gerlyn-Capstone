"""Purchase Orders open on their own page, and "Communicated/Ordered" is "Ordered".

The Purchase Orders list used to raise a PO over itself in the shared detail
modal; like Billing, it now navigates to the PO's own page. And the status
that records an order agreed outside the portal is just "Ordered" — no
"Communicated" wording is shown to the user anywhere.
"""
import importlib

from django.apps import apps
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from procurement.models import Procurement

P = Procurement.Status


class PurchaseOrderPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='pp-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='PP Branch', location='Baliuag', owner=cls.owner)
        cls.supplier = User.objects.create_user(
            username='pp-supplier', password='pw', role=User.Role.SUPPLIER)

    def po(self, status):
        return Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner,
            order_date=timezone.localdate(), status=status, total_cost=0,
        )

    def test_list_view_link_is_plain_navigation_to_the_po_page(self):
        po = self.po(P.APPROVED)
        self.client.force_login(self.owner)

        html = self.client.get(reverse('procurement_list')).content.decode()

        self.assertIn(f'href="{reverse("procurement_detail", args=[po.pk])}"', html)
        self.assertNotIn('data-detail-modal="Purchase Order"', html)

    def test_po_page_renders_in_full(self):
        po = self.po(P.APPROVED)
        self.client.force_login(self.owner)

        response = self.client.get(reverse('procurement_detail', args=[po.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="back-link"')
        self.assertContains(response, po.po_number)

    def test_ordered_is_the_status_value_and_label(self):
        self.assertEqual(P.ORDERED.value, 'Ordered')
        self.assertEqual(P.ORDERED.label, 'Ordered')
        self.assertNotIn('Communicated/Ordered', P.values)

    def test_ordered_po_shows_ordered_not_communicated(self):
        po = self.po(P.ORDERED)
        self.client.force_login(self.owner)

        for url in (reverse('procurement_list'), reverse('delivery_list'),
                    reverse('procurement_detail', args=[po.pk])):
            with self.subTest(url):
                html = self.client.get(url).content.decode()
                self.assertIn('Ordered', html)
                self.assertNotIn('Communicated', html)

    def test_ordered_filter_tab_finds_the_po(self):
        po = self.po(P.ORDERED)
        self.po(P.APPROVED)
        self.client.force_login(self.owner)

        response = self.client.get(reverse('procurement_list'), {'status': 'Ordered'})

        self.assertContains(response, po.po_number)
        self.assertEqual(
            [p.pk for p in response.context['procurements']], [po.pk])

    def test_migration_converts_existing_rows_and_reverses(self):
        po = self.po(P.APPROVED)
        Procurement.objects.filter(pk=po.pk).update(status='Communicated/Ordered')
        migration = importlib.import_module(
            'procurement.migrations.0019_rename_communicated_status_to_ordered')

        migration.communicated_to_ordered(apps, None)
        po.refresh_from_db()
        self.assertEqual(po.status, 'Ordered')

        migration.ordered_to_communicated(apps, None)
        po.refresh_from_db()
        self.assertEqual(po.status, 'Communicated/Ordered')
