from django.test import TestCase
from django.urls import reverse

from accounts.models import Notification, User
from branches.models import Branch


class NotificationCenterViewModalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='nc-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='NC Branch', location='Baliuag', owner=cls.owner
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def _notification(self, **kwargs):
        defaults = dict(
            user=self.owner, title='PO-1 cancelled', message='PO-1 was cancelled.',
            category=Notification.Category.PROCUREMENT,
            priority=Notification.Priority.HIGH, branch=self.branch,
        )
        defaults.update(kwargs)
        return Notification.objects.create(**defaults)

    def test_every_row_has_a_view_modal_trigger_and_modal_shell(self):
        # Rows without an action_url used to get no View control at all.
        self._notification(title='No link', action_url='')
        response = self.client.get(reverse('notification_center'))
        self.assertContains(response, '<button type="button" class="icon-btn icon-btn-action" data-notif-view', count=1)
        self.assertContains(response, 'id="notifModal"')

    def test_resolve_form_acts_immediately_and_confirms_afterwards(self):
        n = self._notification(requires_action=True)
        response = self.client.get(reverse('notification_center'))
        self.assertContains(response, 'class="tray-resolve-form"')
        # No "Mark as resolved?" question up front; the success modal follows.
        self.assertNotContains(response, 'data-confirm=')
        self.assertContains(response, 'data-resolved-message="PO-1 cancelled was marked as resolved.')
        self.assertContains(response, reverse('notification_resolve', args=[n.pk]))

    def test_mark_read_form_is_not_confirmed(self):
        self._notification(requires_action=False)
        response = self.client.get(reverse('notification_center'))
        self.assertContains(response, 'class="tray-read-form"')
        self.assertNotContains(response, 'data-confirm=')

    def test_no_js_resolve_and_read_return_to_notification_center(self):
        needs_action = self._notification(requires_action=True)
        plain = self._notification(title='Plain', requires_action=False)

        resolved = self.client.post(reverse('notification_resolve', args=[needs_action.pk]))
        self.assertRedirects(resolved, reverse('notification_center'))
        needs_action.refresh_from_db()
        self.assertIsNotNone(needs_action.resolved_at)

        read = self.client.post(reverse('notification_mark_read', args=[plain.pk]))
        self.assertRedirects(read, reverse('notification_center'))
        plain.refresh_from_db()
        self.assertTrue(plain.is_read)
