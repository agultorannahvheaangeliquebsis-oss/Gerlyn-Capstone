"""Status-pill colour parity.

These render the canonical partials directly, so they pin the DESIGN.md
status table without depending on any seeded records: the same semantic
status must land on the same .pill-* class on every surface.
"""
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

User = get_user_model()


def _pill(template, **ctx):
    return render_to_string(template, ctx)


class CancelledIsDangerEverywhereTests(SimpleTestCase):
    """DESIGN.md: Cancelled -> .pill-danger. Billing and supplier returns
    used to paint it neutral, so an order page showed a red "Cancelled"
    order pill beside a grey "Cancelled" payment pill."""

    def test_order_po_billing_and_return_cancelled_share_danger(self):
        for template, extra in [
            ('partials/_order_status_pill.html', {}),
            ('partials/_order_status_pill.html', {'customer': True}),
            ('partials/_po_status_pill.html', {}),
            ('partials/_billing_status_pill.html', {}),
            ('partials/_billing_status_pill.html', {'customer': True}),
            ('partials/_return_status_pill.html', {}),
        ]:
            with self.subTest(template=template, **extra):
                html = _pill(template, status='Cancelled', **extra)
                self.assertIn('pill-danger', html)
                self.assertNotIn('pill-neutral', html)

    def test_billing_staff_wording_is_unchanged(self):
        self.assertIn('Voided', _pill('partials/_billing_status_pill.html', status='Cancelled'))


class PillVocabularyUnchangedTests(SimpleTestCase):
    def test_other_states_keep_their_classes(self):
        cases = [
            ('partials/_billing_status_pill.html', 'Paid', 'pill-ok'),
            ('partials/_billing_status_pill.html', 'Rejected', 'pill-danger'),
            ('partials/_billing_status_pill.html', 'Refunded', 'pill-info'),
            ('partials/_return_status_pill.html', 'Settled', 'pill-ok'),
            ('partials/_return_status_pill.html', 'Requested', 'pill-warn'),
            ('partials/_po_status_pill.html', 'Incoming', 'pill-info'),
            ('partials/_po_status_pill.html', 'Draft', 'pill-neutral'),
        ]
        for template, status, cls in cases:
            with self.subTest(template=template, status=status):
                self.assertIn(cls, _pill(template, status=status))


class PoPillWordsWhoseTurnItIsTests(SimpleTestCase):
    """The three back-and-forth statuses are internal names; on the buyer's
    side the badge says whose turn it is instead."""

    def test_negotiation_statuses_read_from_the_buyers_side(self):
        cases = [
            ('Pending Owner Review', 'Your turn — respond'),
            ('Pending Supplier Review', 'Waiting on supplier'),
            ('Terms Agreed', 'Terms agreed — waiting on supplier to confirm'),
        ]
        for status, wording in cases:
            with self.subTest(status=status):
                self.assertIn(wording, _pill('partials/_po_status_pill.html', status=status))


class PoDocumentStatusTests(SimpleTestCase):
    """The printed page and the PDF are the same artefact, so they share one
    status line — worded for a reader who may be the supplier."""

    def test_document_wording_is_neutral_and_carries_no_badge_markup(self):
        cases = [
            ('Pending Owner Review', 'Awaiting buyer response'),
            ('Pending Supplier Review', 'Awaiting supplier response'),
            ('Terms Agreed', 'Terms agreed — awaiting supplier confirmation'),
            ('Received', 'Received'),
            ('Cancelled', 'Cancelled'),
            ('Draft', 'Draft'),
        ]
        for status, wording in cases:
            with self.subTest(status=status):
                html = _pill('procurement/_po_document_status.html', status=status)
                self.assertEqual(html.strip(), wording)
                self.assertNotIn('<span', html)

    def test_document_never_borrows_the_screen_pills_buyer_wording(self):
        html = _pill('procurement/_po_document_status.html', status='Pending Owner Review')
        self.assertNotIn('Your turn', html)


class CustomerProfileMemberSinceTests(TestCase):
    """The profile hero showed "Member since <month year>" — an account-age
    boast on a shop profile that told the customer nothing they could act on
    and crowded the role pill beside it."""

    def test_profile_no_longer_shows_member_since(self):
        user = User.objects.create_user(
            username='shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(user)
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Member since')
        # date_joined itself is untouched — only the display went away.
        self.assertIsNotNone(User.objects.get(pk=user.pk).date_joined)
