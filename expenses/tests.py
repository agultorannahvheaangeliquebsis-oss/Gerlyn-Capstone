"""Expenses: who may record them, what is valid, the audit trail, the receipt."""
import importlib
import io
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import BranchManagerProfile, StaffProfile, User
from audit import context as audit_context
from audit.models import AuditLog
from branches.models import Branch
from branches.services import branch_deletion_blockers
from core.module_permissions import (
    EXPENSES, role_permission_defaults, user_can_access_module, user_can_perform_module_action,
)
from expenses import services
from expenses.models import Expense

D = Decimal
FAST_HASHER = ['django.contrib.auth.hashers.MD5PasswordHasher']


def png(name='bill.png', size=(8, 8)):
    out = io.BytesIO()
    Image.new('RGB', size, (200, 30, 30)).save(out, 'PNG')
    return SimpleUploadedFile(name, out.getvalue(), content_type='image/png')


def client_for(user):
    client = Client()
    client.force_login(user)
    return client


@override_settings(PASSWORD_HASHERS=FAST_HASHER)
class ExpenseCase(TestCase):
    """One owner, two branches, a manager on each, and a Staff member."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.owner = User.objects.create_user(username='ex-owner', password='pw', role=User.Role.OWNER)
        cls.branch_a = Branch.objects.create(branch_name='Expense A', location='Baliuag', owner=cls.owner)
        cls.branch_b = Branch.objects.create(branch_name='Expense B', location='Pulilan', owner=cls.owner)
        cls.manager_a = cls._user('ex-mgr-a', User.Role.BRANCH_MANAGER, BranchManagerProfile, 'manager_name', cls.branch_a)
        cls.manager_b = cls._user('ex-mgr-b', User.Role.BRANCH_MANAGER, BranchManagerProfile, 'manager_name', cls.branch_b)
        cls.staff_a = cls._user('ex-staff-a', User.Role.STAFF, StaffProfile, 'staff_name', cls.branch_a)
        cls.customer = User.objects.create_user(username='ex-cust', password='pw', role=User.Role.CUSTOMER)

    @classmethod
    def _user(cls, username, role, profile_model, name_field, branch):
        user = User.objects.create_user(username=username, password='pw', role=role)
        profile_model.objects.update_or_create(user=user, defaults={name_field: username, 'branch': branch})
        user.refresh_from_db()
        return user

    def setUp(self):
        media = tempfile.mkdtemp(prefix='gerlyn-expense-receipts-')
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        patcher = patch.object(
            Expense._meta.get_field('receipt'), 'storage', FileSystemStorage(location=media, base_url=None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.media = media
        # Services called directly (not through a request) leave their audit
        # label behind for the next test.
        self.addCleanup(audit_context.set_action_label, '')

    def make(self, branch=None, **kw):
        values = dict(
            branch=branch or self.branch_a, category='Rent', amount=D('1500.00'),
            date=self.today, description='October rent', recorded_by=self.owner)
        values.update(kw)
        return Expense.objects.create(**values)

    def post_add(self, user, **kw):
        data = {'branch': self.branch_a.pk, 'category': 'Utilities', 'amount': '820.50',
                'date': self.today.isoformat(), 'description': 'Meralco'}
        data.update(kw)
        return client_for(user).post(reverse('expense_add'), data)


class PermissionTests(ExpenseCase):
    def test_staff_cannot_see_add_edit_delete_or_open_a_receipt(self):
        expense = self.make()
        client = client_for(self.staff_a)
        self.assertEqual(client.get(reverse('expense_list')).status_code, 403)
        self.assertEqual(client.get(reverse('expense_add')).status_code, 403)
        self.assertEqual(client.post(reverse('expense_add'), {'category': 'Rent'}).status_code, 403)
        self.assertEqual(client.get(reverse('expense_edit', args=[expense.pk])).status_code, 403)
        self.assertEqual(client.post(reverse('expense_delete', args=[expense.pk])).status_code, 403)
        self.assertEqual(client.get(reverse('expense_receipt', args=[expense.pk])).status_code, 403)
        self.assertEqual(Expense.objects.count(), 1)

    def test_a_grant_cannot_open_the_module_to_staff(self):
        self.staff_a.allowed_modules = list(self.staff_a.allowed_modules) + [EXPENSES]
        self.staff_a.module_actions = {EXPENSES: ['add', 'edit', 'delete']}
        self.staff_a.save()
        self.assertFalse(user_can_access_module(self.staff_a, EXPENSES))
        self.assertFalse(user_can_perform_module_action(self.staff_a, EXPENSES, 'add'))
        self.assertEqual(client_for(self.staff_a).get(reverse('expense_list')).status_code, 403)

    def test_customers_and_anonymous_visitors_are_refused(self):
        self.assertEqual(client_for(self.customer).get(reverse('expense_list')).status_code, 403)
        response = Client().get(reverse('expense_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_role_defaults_give_managers_the_module_and_staff_nothing(self):
        manager = role_permission_defaults('BRANCH_MANAGER')
        self.assertIn(EXPENSES, manager['allowed_modules'])
        self.assertEqual(manager['module_actions'][EXPENSES], ['add', 'edit', 'delete'])
        self.assertNotIn(EXPENSES, role_permission_defaults('STAFF')['allowed_modules'])
        self.assertEqual(role_permission_defaults('STAFF')['module_actions'][EXPENSES], [])
        # A manager account created today really gets it.
        self.assertTrue(user_can_perform_module_action(self.manager_a, EXPENSES, 'delete'))

    def test_a_manager_without_the_module_or_the_action_is_refused(self):
        expense = self.make()
        self.manager_a.allowed_modules = [m for m in self.manager_a.allowed_modules if m != EXPENSES]
        self.manager_a.save()
        self.assertEqual(client_for(self.manager_a).get(reverse('expense_list')).status_code, 403)

        self.manager_b.module_actions = {**self.manager_b.module_actions, EXPENSES: ['add', 'edit']}
        self.manager_b.save()
        other = self.make(self.branch_b)
        self.assertEqual(client_for(self.manager_b).post(reverse('expense_delete', args=[other.pk])).status_code, 403)
        self.assertTrue(Expense.objects.filter(pk=other.pk).exists())
        self.assertTrue(Expense.objects.filter(pk=expense.pk).exists())


class BranchScopeTests(ExpenseCase):
    def test_a_manager_records_for_their_own_branch_whatever_they_post(self):
        response = self.post_add(self.manager_a, branch=self.branch_b.pk)
        self.assertRedirects(response, reverse('expense_list'))
        expense = Expense.objects.get()
        self.assertEqual(expense.branch, self.branch_a)
        self.assertEqual(expense.recorded_by, self.manager_a)

    def test_a_manager_with_no_branch_cannot_record_anything(self):
        orphan = User.objects.create_user(username='ex-orphan', password='pw', role=User.Role.BRANCH_MANAGER)
        response = self.post_add(orphan)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No branch is assigned to your account')
        self.assertFalse(Expense.objects.exists())

    def test_the_list_shows_a_manager_only_their_branch(self):
        mine, theirs = self.make(self.branch_a, description='mine'), self.make(self.branch_b, description='theirs')
        response = client_for(self.manager_a).get(reverse('expense_list'), {'range': 'all'})
        self.assertEqual([e.pk for e in response.context['expenses']], [mine.pk])
        self.assertEqual(response.context['total_amount'], D('1500.00'))
        self.assertNotContains(response, 'theirs')
        # ...and ?branch= does not widen it.
        response = client_for(self.manager_a).get(
            reverse('expense_list'), {'range': 'all', 'branch': self.branch_b.pk})
        self.assertEqual([e.pk for e in response.context['expenses']], [mine.pk])
        self.assertIsNotNone(theirs.pk)

    def test_an_unassigned_manager_sees_nothing(self):
        self.make()
        orphan = User.objects.create_user(username='ex-orphan2', password='pw', role=User.Role.BRANCH_MANAGER)
        response = client_for(orphan).get(reverse('expense_list'), {'range': 'all'})
        self.assertEqual(list(response.context['expenses']), [])

    def test_a_manager_cannot_open_edit_delete_or_view_the_receipt_of_another_branchs_expense(self):
        other = self.make(self.branch_b, receipt=png())
        client = client_for(self.manager_a)
        self.assertEqual(client.get(reverse('expense_edit', args=[other.pk])).status_code, 404)
        self.assertEqual(client.post(reverse('expense_edit', args=[other.pk]), {
            'category': 'Rent', 'amount': '1', 'date': self.today.isoformat()}).status_code, 404)
        self.assertEqual(client.post(reverse('expense_delete', args=[other.pk])).status_code, 404)
        self.assertEqual(client.get(reverse('expense_receipt', args=[other.pk])).status_code, 404)
        other.refresh_from_db()
        self.assertEqual(other.amount, D('1500.00'))

    def test_an_owner_sees_every_branch_and_can_narrow_to_one(self):
        a, b = self.make(self.branch_a), self.make(self.branch_b)
        client = client_for(self.owner)
        everything = client.get(reverse('expense_list'), {'range': 'all'}).context['expenses']
        self.assertEqual({e.pk for e in everything}, {a.pk, b.pk})
        narrowed = client.get(reverse('expense_list'), {'range': 'all', 'branch': self.branch_b.pk}).context['expenses']
        self.assertEqual([e.pk for e in narrowed], [b.pk])

    def test_an_owner_must_choose_a_branch(self):
        response = self.post_add(self.owner, branch='')
        self.assertContains(response, 'Choose a branch.')
        self.assertFalse(Expense.objects.exists())

    def test_a_branch_with_expenses_cannot_be_deleted(self):
        self.assertNotIn('expense records', branch_deletion_blockers(self.branch_a))
        self.make()
        self.assertIn('expense records', branch_deletion_blockers(self.branch_a))


class CrudTests(ExpenseCase):
    def test_owner_creates_edits_and_deletes(self):
        response = self.post_add(self.owner, branch=self.branch_b.pk, category='Salaries', amount='12000',
                                 description='Cashiers, October')
        self.assertRedirects(response, reverse('expense_list'))
        expense = Expense.objects.get()
        self.assertEqual((expense.branch, expense.category, expense.amount), (self.branch_b, 'Salaries', D('12000.00')))

        response = client_for(self.owner).post(reverse('expense_edit', args=[expense.pk]), {
            'branch': self.branch_a.pk, 'category': 'Salaries', 'amount': '12500.75',
            'date': self.today.isoformat(), 'description': 'Cashiers, October (adjusted)'})
        self.assertRedirects(response, reverse('expense_list'))
        expense.refresh_from_db()
        self.assertEqual((expense.branch, expense.amount), (self.branch_a, D('12500.75')))

        response = client_for(self.owner).post(reverse('expense_delete', args=[expense.pk]))
        self.assertRedirects(response, reverse('expense_list'))
        self.assertFalse(Expense.objects.exists())

    def test_a_manager_edit_cannot_move_the_expense_to_another_branch(self):
        expense = self.make()
        client_for(self.manager_a).post(reverse('expense_edit', args=[expense.pk]), {
            'branch': self.branch_b.pk, 'category': 'Rent', 'amount': '1600',
            'date': self.today.isoformat(), 'description': 'x'})
        expense.refresh_from_db()
        self.assertEqual((expense.branch, expense.amount), (self.branch_a, D('1600.00')))

    def test_delete_needs_a_post(self):
        expense = self.make()
        self.assertEqual(client_for(self.owner).get(reverse('expense_delete', args=[expense.pk])).status_code, 405)
        self.assertTrue(Expense.objects.exists())

    def test_every_category_is_accepted(self):
        for category in ('Rent', 'Utilities', 'Salaries', 'Transportation', 'Supplies', 'Maintenance'):
            self.post_add(self.owner, category=category)
        self.post_add(self.owner, category='Other', description='Notary fee')
        self.assertEqual(Expense.objects.count(), 7)
        self.assertEqual(set(Expense.objects.values_list('category', flat=True)), set(Expense.Category.values))

    def test_the_list_filters_by_category_and_range(self):
        rent = self.make(category='Rent')
        self.make(category='Utilities')
        self.make(category='Rent', date=self.today - timedelta(days=90))
        client = client_for(self.owner)
        response = client.get(reverse('expense_list'), {'range': '30', 'category': 'Rent'})
        self.assertEqual([e.pk for e in response.context['expenses']], [rent.pk])
        self.assertEqual(response.context['total_amount'], D('1500.00'))
        self.assertEqual(len(client.get(reverse('expense_list'), {'range': 'all'}).context['expenses']), 3)


class ValidationTests(ExpenseCase):
    def assert_rejected(self, message, **kw):
        response = self.post_add(self.owner, **kw)
        self.assertEqual(response.status_code, 200, kw)
        self.assertContains(response, message)
        self.assertFalse(Expense.objects.exists(), kw)

    def test_amounts(self):
        self.assert_rejected('Amount must be more than ₱0.', amount='0')
        self.assert_rejected('Amount must be more than ₱0.', amount='-5')
        self.assert_rejected('Amount must be a valid number.', amount='abc')
        self.assert_rejected('Amount must be a valid number.', amount='1e3')
        self.assert_rejected('Amount cannot be more than ₱10,000,000.00.', amount='10000000.01')
        self.assert_rejected('Amount can have at most 2 decimal places.', amount='10.005')
        self.assert_rejected('Amount is required.', amount='')

    def test_the_largest_allowed_amount_is_accepted(self):
        self.post_add(self.owner, amount='10000000.00')
        self.assertEqual(Expense.objects.get().amount, D('10000000.00'))

    def test_dates(self):
        self.assert_rejected('The date cannot be in the future.', date=(self.today + timedelta(days=1)).isoformat())
        self.assert_rejected('The date is too far in the past.', date='1999-12-31')
        self.assert_rejected('Enter the date this expense belongs to.', date='')
        self.assert_rejected('Enter the date this expense belongs to.', date='2026-02-30')

    def test_today_is_allowed(self):
        self.post_add(self.owner, date=self.today.isoformat())
        self.assertEqual(Expense.objects.get().date, self.today)

    def test_category_and_description(self):
        self.assert_rejected('Choose a category from the list.', category='Gifts')
        self.assert_rejected('Describe what this expense was for.', category='Other', description='  ')
        self.assert_rejected('Description cannot be longer than 1000 characters.', description='x' * 1001)

    def test_a_receipt_must_be_a_real_jpg_or_png(self):
        def attempt(upload, message):
            response = client_for(self.owner).post(reverse('expense_add'), {
                'branch': self.branch_a.pk, 'category': 'Rent', 'amount': '10',
                'date': self.today.isoformat(), 'receipt': upload})
            self.assertContains(response, message)
            self.assertFalse(Expense.objects.exists())

        attempt(SimpleUploadedFile('receipt.png', b'<html>not an image</html>', content_type='image/png'),
                'is not a valid image')
        gif = io.BytesIO()
        Image.new('RGB', (4, 4)).save(gif, 'GIF')
        attempt(SimpleUploadedFile('receipt.gif', gif.getvalue(), content_type='image/gif'),
                'must be a JPG or PNG picture')
        attempt(SimpleUploadedFile('big.png', b'0' * (5 * 1024 * 1024 + 1), content_type='image/png'),
                'is larger than 5 MB')

    def test_a_valid_receipt_is_stored_under_a_name_we_choose(self):
        response = client_for(self.owner).post(reverse('expense_add'), {
            'branch': self.branch_a.pk, 'category': 'Rent', 'amount': '10',
            'date': self.today.isoformat(), 'receipt': png('../../evil name.png')})
        self.assertRedirects(response, reverse('expense_list'))
        name = Expense.objects.get().receipt.name
        self.assertRegex(name, r'^expense_receipts/\d{4}/\d{2}/[0-9a-f]{32}\.png$')


class ReceiptTests(ExpenseCase):
    def test_the_receipt_is_streamed_privately_to_those_who_may_see_it(self):
        expense = self.make(receipt=png())
        response = client_for(self.manager_a).get(reverse('expense_receipt', args=[expense.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        b''.join(response.streaming_content)
        self.assertEqual(client_for(self.owner).get(reverse('expense_receipt', args=[expense.pk])).status_code, 200)

    def test_an_expense_without_a_receipt_is_a_404(self):
        expense = self.make()
        self.assertEqual(client_for(self.owner).get(reverse('expense_receipt', args=[expense.pk])).status_code, 404)

    def test_the_receipt_is_not_reachable_from_the_public_media_route(self):
        expense = self.make(receipt=png())
        response = Client().get(f'/media/{expense.receipt.name}')
        self.assertEqual(response.status_code, 404)

    def test_replacing_or_removing_a_receipt_deletes_the_old_file_after_commit(self):
        expense = self.make(receipt=png())
        old_name = expense.receipt.name
        storage = expense.receipt.storage
        self.assertTrue(storage.exists(old_name))
        with self.captureOnCommitCallbacks(execute=True):
            response = client_for(self.owner).post(reverse('expense_edit', args=[expense.pk]), {
                'branch': self.branch_a.pk, 'category': 'Rent', 'amount': '1500',
                'date': self.today.isoformat(), 'receipt': png('new.png')})
        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertNotEqual(expense.receipt.name, old_name)
        self.assertFalse(storage.exists(old_name))

        new_name = expense.receipt.name
        with self.captureOnCommitCallbacks(execute=True):
            client_for(self.owner).post(reverse('expense_edit', args=[expense.pk]), {
                'branch': self.branch_a.pk, 'category': 'Rent', 'amount': '1500',
                'date': self.today.isoformat(), 'remove_receipt': '1'})
        expense.refresh_from_db()
        self.assertFalse(expense.receipt)
        self.assertFalse(storage.exists(new_name))

    def test_deleting_an_expense_deletes_its_receipt_file(self):
        expense = self.make(receipt=png())
        name, storage = expense.receipt.name, expense.receipt.storage
        with self.captureOnCommitCallbacks(execute=True):
            client_for(self.owner).post(reverse('expense_delete', args=[expense.pk]))
        self.assertFalse(storage.exists(name))


class AuditTests(ExpenseCase):
    def rows(self):
        return list(AuditLog.objects.filter(entity='expenses.Expense').order_by('id'))

    def test_create_edit_and_delete_are_logged_with_the_actor(self):
        client = client_for(self.manager_a)
        client.post(reverse('expense_add'), {
            'category': 'Salaries', 'amount': '9000', 'date': self.today.isoformat(),
            'description': 'Maria Santos, October', 'receipt': png()})
        expense = Expense.objects.get()
        client.post(reverse('expense_edit', args=[expense.pk]), {
            'category': 'Salaries', 'amount': '9500', 'date': self.today.isoformat(),
            'description': 'Maria Santos, October'})
        client.post(reverse('expense_delete', args=[expense.pk]))

        created, updated, deleted = self.rows()
        self.assertEqual((created.action, deleted.action, updated.action), ('create', 'delete', 'update'))
        for row in (created, updated, deleted):
            self.assertEqual(row.actor_id, self.manager_a.pk)
            self.assertEqual(row.actor_role, 'BRANCH_MANAGER')
            self.assertEqual(row.branch_id, self.branch_a.pk)
            self.assertEqual(row.record_id, str(expense.pk))
            self.assertEqual(row.record_repr, 'Salaries')
        self.assertEqual(created.action_label, 'Recorded an expense')
        self.assertEqual(updated.action_label, 'Edited an expense')
        self.assertEqual(deleted.action_label, 'Deleted an expense')
        self.assertEqual((updated.before['amount'], updated.after['amount']), ('9000.00', '9500.00'))
        self.assertEqual(deleted.before['amount'], '9500.00')

    def test_free_text_and_the_receipt_file_never_reach_the_log(self):
        client_for(self.owner).post(reverse('expense_add'), {
            'branch': self.branch_a.pk, 'category': 'Salaries', 'amount': '9000',
            'date': self.today.isoformat(), 'description': 'Maria Santos, October', 'receipt': png()})
        created = self.rows()[0]
        self.assertEqual(created.after['description'], '[personal data]')
        self.assertEqual(created.after['receipt'], '[file]')
        self.assertNotIn('Maria', str(created.after) + created.summary)

    def test_a_refused_attempt_leaves_no_trace_in_the_log(self):
        other = self.make(self.branch_b)
        before = AuditLog.objects.filter(entity='expenses.Expense').count()
        client_for(self.manager_a).post(reverse('expense_delete', args=[other.pk]))
        client_for(self.staff_a).post(reverse('expense_delete', args=[other.pk]))
        self.assertEqual(AuditLog.objects.filter(entity='expenses.Expense').count(), before)

    def test_services_called_directly_are_logged_too(self):
        cleaned, errors = services.clean_expense(self.owner, {
            'branch': self.branch_a.pk, 'category': 'Rent', 'amount': '10',
            'date': self.today.isoformat()}, None)
        self.assertEqual(errors, [])
        services.create_expense(self.owner, cleaned)
        self.assertEqual(len(self.rows()), 1)


class MigrationTests(ExpenseCase):
    def test_the_grant_migration_gives_existing_managers_the_module_and_leaves_staff_alone(self):
        migration = importlib.import_module('expenses.migrations.0002_grant_expenses_module')
        User.objects.filter(pk=self.manager_a.pk).update(allowed_modules=['reports'], module_actions={})
        staff_modules = list(self.staff_a.allowed_modules)
        migration.grant(django_apps, None)
        self.manager_a.refresh_from_db()
        self.staff_a.refresh_from_db()
        self.assertIn(EXPENSES, self.manager_a.allowed_modules)
        self.assertEqual(self.manager_a.module_actions[EXPENSES], ['add', 'edit', 'delete'])
        self.assertEqual(self.staff_a.allowed_modules, staff_modules)
        self.assertEqual(self.staff_a.module_actions.get(EXPENSES, []), [])   # no action granted
        # Running it again changes nothing.
        before = (list(self.manager_a.allowed_modules), dict(self.manager_a.module_actions))
        migration.grant(django_apps, None)
        self.manager_a.refresh_from_db()
        self.assertEqual((list(self.manager_a.allowed_modules), dict(self.manager_a.module_actions)), before)
