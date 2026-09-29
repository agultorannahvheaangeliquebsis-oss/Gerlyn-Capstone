"""Dates on the supplier portal read the same way everywhere.

The portal words every other date as "Sep 30, 2026" (and "Sep 30" on the
compact arriving pill), but the two order dates - on Needs Response and on
Recent Orders - were still bare ISO. A supplier reading their own orders
shouldn't have to switch date formats halfway down one page.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import SupplierProfile, User
from branches.models import Branch
from procurement.models import Procurement

P = Procurement.Status


class SupplierPortalDateFormatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='spd-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='SPD Branch', location='Baliuag', owner=cls.owner)
        cls.supplier = User.objects.create_user(
            username='spd-supplier', password='pw', role=User.Role.SUPPLIER)
        SupplierProfile.objects.update_or_create(
            user=cls.supplier, defaults={'supplier_name': 'SPD Supply'})

    def _po(self, status):
        return Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner,
            order_date=dt.date(2026, 3, 4), status=status,
            total_cost=Decimal('100.00'),
        )

    def test_needs_response_and_recent_orders_both_read_in_words(self):
        self._po(P.PENDING_SUPPLIER_REVIEW)  # needs the supplier's response
        self._po(P.RECEIVED)                 # recent orders only

        self.client.force_login(self.supplier)
        html = self.client.get(reverse('supplier_portal')).content.decode()

        self.assertIn('Placed Mar 4, 2026', html)
        self.assertIn('>Mar 4, 2026 ·', html)
        self.assertNotIn('2026-03-04', html)
