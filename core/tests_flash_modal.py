from django.contrib import messages
from django.contrib.messages.storage.base import Message
from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from core.templatetags.flash import flash_summary


class FlashSummaryTests(TestCase):
    def _msg(self, level, text='x'):
        return Message(level, text)

    def test_worst_level_heads_the_modal(self):
        items = [self._msg(messages.SUCCESS), self._msg(messages.ERROR), self._msg(messages.WARNING)]
        self.assertEqual(flash_summary(items)['level'], 'error')
        self.assertEqual(flash_summary(items[:1] + items[2:])['level'], 'warning')

    def test_success_and_info_labels(self):
        success = flash_summary([self._msg(messages.SUCCESS)])
        self.assertEqual((success['title'], success['button']), ('Success', 'Done'))
        self.assertEqual(flash_summary([self._msg(messages.INFO)])['level'], 'info')

    def test_unknown_level_reads_as_info(self):
        self.assertEqual(flash_summary([self._msg(messages.DEBUG)])['level'], 'info')
        self.assertEqual(flash_summary([self._msg(45)])['level'], 'info')

    def test_plain_strings_take_the_given_level(self):
        summary = flash_summary(['Not enough stock.'], 'error')
        self.assertEqual(summary['level'], 'error')
        self.assertEqual(summary['title'], 'Something went wrong')


class FlashModalRenderTests(TestCase):
    def _render(self, **context):
        return Template('{% include "partials/_flash_modal.html" %}').render(Context(context))

    def test_nothing_renders_without_messages(self):
        self.assertNotIn('bottom-sheet', self._render(messages=[]))

    def test_messages_render_as_one_modal_not_banners(self):
        html = self._render(messages=[
            self._msg(messages.SUCCESS, 'Product added successfully.'),
            self._msg(messages.WARNING, 'Low stock on Rice.'),
        ])
        self.assertEqual(html.count('class="bottom-sheet '), 1)
        self.assertIn('bottom-sheet--warning', html)
        self.assertIn('Product added successfully.', html)
        self.assertIn('Low stock on Rice.', html)
        self.assertIn('aria-modal="true"', html)
        self.assertNotIn('class="banner', html)

    def test_error_is_an_alertdialog(self):
        html = self._render(messages=[self._msg(messages.ERROR, 'Nope.')])
        self.assertIn('role="alertdialog"', html)

    def test_message_text_is_escaped(self):
        html = self._render(messages=[self._msg(messages.ERROR, '<script>alert(1)</script>')])
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_explicit_items_override_messages(self):
        html = Template(
            '{% include "partials/_flash_modal.html" with flash_source=errs level="error" %}'
        ).render(Context({'errs': ['Quantity must be positive.'], 'messages': []}))
        self.assertIn('Quantity must be positive.', html)
        self.assertIn('bottom-sheet--error', html)

    def test_page_context_items_do_not_raise_a_modal(self):
        # The order detail page has its own `items` (the order lines); they
        # must never be mistaken for messages to show.
        html = self._render(items=['Canned Sardines x3'], messages=[])
        self.assertNotIn('bottom-sheet', html)
        self.assertNotIn('Canned Sardines', html)

    def _msg(self, level, text):
        return Message(level, text)


class FlashModalPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='flash-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Flash Branch', location='Baliuag', owner=cls.owner
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def test_redirected_success_message_is_a_modal_not_a_banner(self):
        response = self.client.post(
            reverse('branch_add'),
            {'branch_name': 'Modal Branch', 'location': 'Baliuag'},
            follow=True,
        )
        self.assertContains(response, 'Branch &quot;Modal Branch&quot; created with its warehouse.')
        self.assertContains(response, 'class="bottom-sheet bottom-sheet--success"', count=1)
        self.assertNotContains(response, 'class="banner banner-success')

    def test_refused_action_message_is_an_error_modal(self):
        manager = User.objects.create_user(
            username='flash-manager', password='pw', role=User.Role.BRANCH_MANAGER
        )
        self.client.force_login(manager)
        response = self.client.get(reverse('branch_list'), follow=True)
        self.assertContains(response, 'Only the Owner can manage branches.')
        self.assertContains(response, 'class="bottom-sheet bottom-sheet--error"', count=1)
        self.assertContains(response, 'role="alertdialog"')
        self.assertNotContains(response, 'class="banner banner-error"')

    def test_supplier_portal_messages_are_a_modal(self):
        supplier = User.objects.create_user(
            username='flash-supplier', password='pw', role=User.Role.SUPPLIER
        )
        self.client.force_login(supplier)
        response = self.client.get(reverse('supplier_portal'), follow=True)
        # Hashed by the manifest storage, hence the prefix; without the script the
        # modal would render but never open.
        self.assertContains(response, 'js/bottom-sheet.')
