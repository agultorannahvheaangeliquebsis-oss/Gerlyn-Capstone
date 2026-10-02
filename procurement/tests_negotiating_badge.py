"""The Negotiating tab's "for you" badge.

Negotiating folds three statuses together, and only one of them — Pending
Owner Review — is ever the buyer's turn. The count alone made the buyer open
every row in the tab to find out whether any of them was waiting on them.
"""
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from procurement.models import Procurement

P = Procurement.Status


class NegotiatingBadgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='nb-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='NB Branch', location='Baliuag', owner=cls.owner)
        cls.supplier = User.objects.create_user(
            username='nb-supplier', password='pw', role=User.Role.SUPPLIER)

    def po(self, status):
        return Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner,
            # Recent, not a fixed historical date — procurement_list/
            # delivery_list's shared range control (DESIGN.md §2e) defaults
            # to the last 30 days, and this test's own date isn't the thing
            # under test.
            order_date=timezone.localdate(), status=status, total_cost=0,
        )

    def _get(self, url_name):
        self.client.force_login(self.owner)
        return self.client.get(reverse(url_name)).content.decode()

    def test_badge_counts_only_the_buyers_own_turn(self):
        self.po(P.PENDING_OWNER_REVIEW)
        self.po(P.PENDING_OWNER_REVIEW)
        self.po(P.PENDING_SUPPLIER_REVIEW)
        self.po(P.TERMS_AGREED)

        for url_name in ('procurement_list', 'delivery_list'):
            with self.subTest(url_name):
                html = self._get(url_name)
                # Four negotiating orders, two of them the buyer's turn.
                self.assertIn('>4</span>', html)
                self.assertIn('2 for you', html)

    def test_no_badge_when_nothing_is_waiting_on_the_buyer(self):
        self.po(P.PENDING_SUPPLIER_REVIEW)
        self.po(P.TERMS_AGREED)

        for url_name in ('procurement_list', 'delivery_list'):
            with self.subTest(url_name):
                html = self._get(url_name)
                self.assertNotIn('for you', html)

    def test_badge_respects_branch_scoping(self):
        """A manager sees only their own branch's waiting orders, not every
        branch's — the badge is built from the same scoped queryset."""
        from accounts.models import BranchManagerProfile

        other = Branch.objects.create(
            branch_name='NB Other', location='Makinabang', owner=self.owner)
        manager = User.objects.create_user(
            username='nb-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        BranchManagerProfile.objects.update_or_create(
            user=manager,
            defaults={'branch': self.branch, 'manager_name': 'M', 'contact_number': '0'})

        self.po(P.PENDING_OWNER_REVIEW)
        Procurement.objects.create(
            branch=other, supplier=self.supplier, owner=self.owner,
            order_date=timezone.localdate(), status=P.PENDING_OWNER_REVIEW, total_cost=0,
        )

        self.client.force_login(manager)
        html = self.client.get(reverse('procurement_list')).content.decode()
        self.assertIn('1 for you', html)
        self.assertNotIn('2 for you', html)
