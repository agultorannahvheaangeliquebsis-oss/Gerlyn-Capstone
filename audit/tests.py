import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from accounts.models import Address, User
from accounts.services import hard_delete_personal_data
from audit import triggers
from audit.context import set_action_label, suppressed
from audit.models import AppendOnlyError, AuditLog
from audit.registry import GLOBAL_EXCLUDE, REGISTRY
from backups.models import BackupRun, RestoreTestRecord
from billing.models import BillingRecord
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


def _logs(entity=None, action=None, source=None):
    qs = AuditLog.objects.order_by('id')
    if source:
        qs = qs.filter(source=source)
    if entity:
        qs = qs.filter(entity=entity)
    if action:
        qs = qs.filter(action=action)
    return list(qs)


def _everything_written():
    """All text the log holds, lowercased — for "this must not appear anywhere"."""
    return json.dumps([
        [l.summary, l.record_repr, l.before, l.after] for l in AuditLog.objects.all()
    ], default=str).lower()


class AppendOnlyTests(TestCase):
    def setUp(self):
        self.log = AuditLog.objects.create(
            action='create', entity='branches.Branch', record_id='1',
        )

    def test_an_existing_row_cannot_be_edited(self):
        self.log.summary = 'tampered'
        with self.assertRaises(AppendOnlyError):
            self.log.save()

    def test_a_row_cannot_be_deleted(self):
        with self.assertRaises(AppendOnlyError):
            self.log.delete()

    def test_a_queryset_cannot_bulk_edit_or_delete(self):
        with self.assertRaises(AppendOnlyError):
            AuditLog.objects.all().update(summary='x')
        with self.assertRaises(AppendOnlyError):
            AuditLog.objects.all().delete()
        with self.assertRaises(AppendOnlyError):
            AuditLog.objects.filter(pk=self.log.pk).delete()
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_appending_still_works(self):
        AuditLog.objects.create(action='update', entity='branches.Branch', record_id='2')
        self.assertEqual(AuditLog.objects.count(), 2)


@skipUnlessDBFeature('has_select_for_update')  # MySQL/Postgres only, not sqlite
class DatabaseTriggerTests(TransactionTestCase):
    """The database itself refuses to change the table — proven with raw SQL,
    which bypasses every Python-level guard. TransactionTestCase because
    CREATE TRIGGER commits implicitly. The triggers are removed again before
    the test's flush, which issues DELETEs of its own."""

    def setUp(self):
        if connection.vendor != 'mysql':
            self.skipTest('MySQL triggers only')
        with connection.cursor() as cursor:
            triggers.install(cursor)
        self.addCleanup(self._remove_triggers)

    def _remove_triggers(self):
        with connection.cursor() as cursor:
            triggers.uninstall(cursor)

    def test_raw_update_and_delete_are_refused(self):
        AuditLog.objects.create(action='create', entity='branches.Branch', record_id='1')
        with connection.cursor() as cursor:
            with self.assertRaises(DatabaseError):
                cursor.execute("UPDATE audit_auditlog SET summary = 'tampered'")
            with self.assertRaises(DatabaseError):
                cursor.execute('DELETE FROM audit_auditlog')
        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(AuditLog.objects.get().summary, '')

    def test_inserts_are_still_allowed(self):
        AuditLog.objects.create(action='create', entity='branches.Branch', record_id='1')
        AuditLog.objects.create(action='update', entity='branches.Branch', record_id='1')
        self.assertEqual(AuditLog.objects.count(), 2)


class CaptureTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='cap-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )

    def test_create_records_the_whole_row(self):
        (entry,) = _logs('branches.Branch', 'create')

        self.assertEqual(entry.record_id, str(self.branch.pk))
        self.assertEqual(entry.record_repr, 'Tarcan')
        self.assertEqual(entry.branch_id, self.branch.pk)
        self.assertIsNone(entry.before)
        self.assertEqual(entry.after['branch_name'], 'Tarcan')
        self.assertEqual(entry.after['location'], 'Baliuag')
        self.assertIn('Created Branch Tarcan', entry.summary)

    def test_update_records_only_what_changed(self):
        self.branch.location = 'Bulacan'
        self.branch.save()

        (entry,) = _logs('branches.Branch', 'update')
        self.assertEqual(entry.before, {'location': 'Baliuag'})
        self.assertEqual(entry.after, {'location': 'Bulacan'})
        self.assertIn('location Baliuag -> Bulacan', entry.summary)

    def test_saving_without_changing_anything_records_nothing(self):
        self.branch.save()

        self.assertEqual(_logs('branches.Branch', 'update'), [])

    def test_update_fields_limits_the_diff_to_what_was_actually_saved(self):
        self.branch.location = 'Bulacan'
        self.branch.branch_name = 'Never saved'
        self.branch.save(update_fields=['location'])

        (entry,) = _logs('branches.Branch', 'update')
        self.assertEqual(set(entry.after), {'location'})

    def test_delete_records_what_was_removed(self):
        pk = self.branch.pk
        self.branch.delete()

        (entry,) = _logs('branches.Branch', 'delete')
        self.assertEqual(entry.record_id, str(pk))
        self.assertEqual(entry.before['branch_name'], 'Tarcan')
        self.assertIsNone(entry.after)

    def test_a_different_spelling_of_the_same_number_is_not_a_change(self):
        product = Product.objects.create(
            sku_code='SKU-1', product_name='Rice', unit_of_measure='Sack', unit_price=100,
        )
        product.unit_price = Decimal('100.00')
        product.save()

        self.assertEqual(_logs('inventory.Product', 'update'), [])

    def test_excluded_fields_are_never_tracked(self):
        product = Product.objects.create(
            sku_code='SKU-2', product_name='Sugar', unit_of_measure='Kg', unit_price=50,
        )
        stock = Inventory.objects.create(
            product=product, branch=self.branch, quantity_on_hand=10, reorder_level=5,
        )
        (created,) = _logs('inventory.Inventory', 'create')
        self.assertNotIn('quantity_on_hand', created.after)

        # Quantities move on every sale and live in the stock ledger.
        stock.quantity_on_hand = 3
        stock.save()
        self.assertEqual(_logs('inventory.Inventory', 'update'), [])

        stock.reorder_level = 8
        stock.save()
        (updated,) = _logs('inventory.Inventory', 'update')
        self.assertEqual(updated.after, {'reorder_level': 8})

    def test_models_outside_the_registry_are_ignored(self):
        from accounts.models import VerificationToken

        VerificationToken.objects.create(
            user=self.owner, purpose=VerificationToken.PURPOSE_EMAIL_VERIFY
        )
        self.assertEqual(_logs('accounts.VerificationToken'), [])

    def test_long_text_is_truncated(self):
        product = Product.objects.create(
            sku_code='SKU-3', product_name='Flour', unit_of_measure='Kg', unit_price=40,
            description='x' * 1000,
        )
        (entry,) = _logs('inventory.Product', 'create')
        # 300 characters kept plus the '...' marker.
        self.assertEqual(len(entry.after['description']), 303)

    def test_direct_orm_writes_are_attributed_to_the_system(self):
        (entry,) = _logs('branches.Branch', 'create')

        self.assertIsNone(entry.actor_id)
        self.assertEqual(entry.source, 'system')

    def test_a_rolled_back_change_leaves_no_trace(self):
        before = AuditLog.objects.count()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                Branch.objects.create(branch_name='Ghost', location='Nowhere', owner=self.owner)
                raise RuntimeError('business action failed')

        self.assertEqual(AuditLog.objects.count(), before)


class PrivacyTests(TestCase):
    """The log must never become a second copy of anything personal — least of
    all after an erasure request."""

    def _customer(self):
        customer = User.objects.create_user(
            username='juan_dela_cruz', password='Sup3rSecret!x',
            email='juan.private@example.com', first_name='Juan', last_name='Delacruz',
            role=User.Role.CUSTOMER,
        )
        profile = customer.customer_profile
        profile.contact_number = '09171234567'
        profile.address = '12 Secret Street'
        profile.save()
        Address.objects.create(user=customer, is_default=True, text='99 Hidden Road')
        return customer

    PERSONAL = [
        'juan.private@example.com', 'juan_dela_cruz', 'juan', 'delacruz',
        '09171234567', '12 secret street', '99 hidden road',
        'sup3rsecret', 'pbkdf2',
    ]

    def test_creating_an_account_records_no_credentials_or_personal_values(self):
        self._customer()

        blob = _everything_written()
        for secret in self.PERSONAL:
            self.assertNotIn(secret, blob)
        (entry,) = _logs('accounts.User', 'create')
        self.assertEqual(entry.after['email'], '[personal data]')
        self.assertEqual(entry.record_repr, f'User account #{entry.record_id}')
        self.assertNotIn('password', entry.after)

    def test_changing_a_personal_field_records_that_it_changed_not_the_values(self):
        customer = self._customer()
        customer.email = 'brand.new@example.com'
        customer.save()

        (entry,) = _logs('accounts.User', 'update')
        self.assertEqual(entry.before, {'email': '[personal data]'})
        self.assertEqual(entry.after, {'email': '[personal data]'})
        self.assertIn('email changed', entry.summary)
        self.assertNotIn('brand.new@example.com', _everything_written())

    def test_an_erased_account_leaves_no_personal_data_in_the_log(self):
        owner = User.objects.create_user(username='hd-owner', password='pw', role=User.Role.OWNER)
        customer = self._customer()

        hard_delete_personal_data(customer, performed_by=owner)

        blob = _everything_written()
        for secret in self.PERSONAL:
            self.assertNotIn(secret, blob)
        # ...but the erasure itself is on record, by id.
        updates = _logs('accounts.User', 'update')
        self.assertTrue(any('deleted_at' in (u.after or {}) for u in updates))

    def test_file_fields_are_recorded_as_a_marker_never_a_name_or_content(self):
        owner = User.objects.create_user(username='f-owner', password='pw', role=User.Role.OWNER)
        branch = Branch.objects.create(branch_name='B', location='L', owner=owner)
        txn = SalesTransaction.objects.create(branch=branch, total_amount=100)
        record = txn.billing_record
        record.proof_of_payment = 'payment_proofs/secret-receipt-of-juan.png'
        record.save()

        entries = [
            e for e in _logs('billing.BillingRecord', 'update')
            if 'proof_of_payment' in (e.after or {})
        ]
        self.assertEqual(entries[0].after['proof_of_payment'], '[file]')
        self.assertNotIn('secret-receipt', _everything_written())

    def test_order_delivery_address_and_discount_id_are_masked(self):
        owner = User.objects.create_user(username='o-owner', password='pw', role=User.Role.OWNER)
        branch = Branch.objects.create(branch_name='B', location='L', owner=owner)
        SalesTransaction.objects.create(
            branch=branch, total_amount=100, delivery_address='7 Private Lane',
            discount_id_number='OSCA-99887766', discount_holder_name='Lola Basyang',
        )
        blob = _everything_written()
        for secret in ('7 private lane', 'osca-99887766', 'lola basyang'):
            self.assertNotIn(secret, blob)

    def test_the_customers_pwd_request_details_are_masked(self):
        """P15 — the ID number and name typed at checkout are the same
        private data as the verified discount_* pair."""
        owner = User.objects.create_user(username='r-owner', password='pw', role=User.Role.OWNER)
        branch = Branch.objects.create(branch_name='B', location='L', owner=owner)
        SalesTransaction.objects.create(
            branch=branch, total_amount=100, pwd_request_status='Pending',
            pwd_request_id_number='PWD-77665544', pwd_request_holder_name='Ka Ruping',
        )
        blob = _everything_written()
        for secret in ('pwd-77665544', 'ka ruping'):
            self.assertNotIn(secret, blob)


class ActorTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='act-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.client.force_login(self.owner)

    def _invite(self):
        return self.client.post(reverse('accounts:user_add'), {
            'email': 'invitee@example.com', 'role': User.Role.STAFF,
            'branch': self.branch.pk,
        }, REMOTE_ADDR='203.0.113.7')

    def test_a_request_names_the_person_who_made_it(self):
        self._invite()

        (entry,) = _logs('accounts.User', 'create', source='request')
        self.assertEqual(entry.actor_id, self.owner.pk)
        self.assertEqual(entry.actor_role, 'OWNER')
        self.assertEqual(entry.source, 'request')
        self.assertEqual(entry.ip_address, '203.0.113.7')

    def test_rows_from_one_request_share_a_request_id(self):
        self._invite()

        ids = {e.request_id for e in _logs() if e.source == 'request'}
        self.assertEqual(len(ids), 1)
        self.assertNotIn('', ids)
        # The user, their auto-created staff profile...
        self.assertGreaterEqual(len([e for e in _logs() if e.source == 'request']), 2)

    def test_cancelling_an_invite_records_who_deleted_it(self):
        self._invite()
        invited = User.objects.get(email='invitee@example.com')

        self.client.post(reverse('accounts:user_cancel_invite', args=[invited.pk]))

        (entry,) = _logs('accounts.User', 'delete', source='request')
        self.assertEqual(entry.record_id, str(invited.pk))
        self.assertEqual(entry.actor_id, self.owner.pk)
        self.assertNotIn('invitee@example.com', _everything_written())

    def test_actor_and_branch_are_plain_integers_not_foreign_keys(self):
        """An FK with SET_NULL would UPDATE audit rows whenever a user or branch
        is removed. The log has to outlive both, untouched."""
        from django.db import models as dj_models

        self.assertFalse(
            any(isinstance(f, dj_models.ForeignKey) for f in AuditLog._meta.get_fields())
        )


class SwitchesAndFailureTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='sw-owner', password='pw', role=User.Role.OWNER
        )
        AuditLog.objects.all()  # touch
        self.baseline = AuditLog.objects.count()

    def _branch(self, name):
        return Branch.objects.create(branch_name=name, location='L', owner=self.owner)

    def test_suppressed_blocks_record_nothing(self):
        with suppressed():
            self._branch('Quiet')
        self.assertEqual(AuditLog.objects.count(), self.baseline)

    def test_suppressed_works_as_a_decorator(self):
        @suppressed()
        def bulk():
            self._branch('Quiet 2')

        bulk()
        self.assertEqual(AuditLog.objects.count(), self.baseline)
        self._branch('Loud')
        # (A new branch also auto-creates its warehouse, so more than one row.)
        self.assertGreater(AuditLog.objects.count(), self.baseline)

    @override_settings(AUDIT_ENABLED=False)
    def test_the_master_switch_turns_capture_off(self):
        self._branch('Off')
        self.assertEqual(AuditLog.objects.count(), self.baseline)

    def test_a_failing_audit_write_never_breaks_the_business_action(self):
        with mock.patch.object(AuditLog, 'save', side_effect=RuntimeError('disk full')):
            with self.assertLogs('audit.capture', level='ERROR'):
                branch = self._branch('Still saved')

        self.assertTrue(Branch.objects.filter(pk=branch.pk).exists())


class BackupAuditCaptureTests(TestCase):
    """KATRINA-BACKUP-002: a backup run — and who triggered it — must be
    reconstructable from the audit log, not only from the Backup Status
    page."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='bk-cap-owner', password='pw', role=User.Role.OWNER,
        )

    def test_a_backup_run_is_captured_with_its_triggering_user(self):
        run = BackupRun.objects.create(
            run_id='r1', kind=BackupRun.Kind.DATABASE, trigger='manual',
            started_at=timezone.now(), triggered_by=self.owner,
        )
        run.status = BackupRun.Status.SUCCESS
        run.finished_at = timezone.now()
        run.save()

        (created,) = _logs('backups.BackupRun', 'create')
        self.assertEqual(created.record_id, str(run.pk))

        (updated,) = _logs('backups.BackupRun', 'update')
        self.assertEqual(updated.after.get('status'), 'success')

    def test_a_scheduled_run_is_captured_with_no_triggering_user(self):
        BackupRun.objects.create(
            run_id='r2', kind=BackupRun.Kind.DATABASE, trigger='cron',
            started_at=timezone.now(),
        )

        (entry,) = _logs('backups.BackupRun', 'create')
        self.assertIsNone(entry.after.get('triggered_by_id'))

    def test_a_restore_test_record_is_captured(self):
        RestoreTestRecord.objects.create(
            tested_on=timezone.localdate(), backup_used='daily/database-x.sql.gz.enc',
            outcome=RestoreTestRecord.Outcome.PASSED, recorded_by=self.owner,
        )

        (entry,) = _logs('backups.RestoreTestRecord', 'create')
        self.assertEqual(entry.record_repr, 'daily/database-x.sql.gz.enc')


class RegistryTests(TestCase):
    def test_every_entry_matches_a_real_model_and_real_fields(self):
        for label, cfg in REGISTRY.items():
            model = apps.get_model(label)
            names = {f.name for f in model._meta.concrete_fields}
            self.assertLessEqual(cfg.mask, names, f'{label} mask')
            self.assertLessEqual(cfg.exclude, names, f'{label} exclude')
            if cfg.repr_field:
                self.assertIn(cfg.repr_field, names, f'{label} repr_field')
            if cfg.branch_path:
                first = cfg.branch_path.split('.')[0]
                self.assertTrue(hasattr(model, first), f'{label} branch_path')

    def test_secrets_are_excluded_globally(self):
        self.assertLessEqual({'password', 'last_login'}, GLOBAL_EXCLUDE)


class AuditLogViewTests(TestCase):
    def setUp(self):
        # The action label is a module-level ContextVar. A test that calls a
        # service which sets one outside a request (the floor-refill and
        # transfer tests do) leaves it behind, and every row written by the
        # next test would then read as that action instead of "Created Branch
        # Tarcan". Start from none so these tests do not depend on run order.
        set_action_label('')
        self.owner = User.objects.create_user(
            username='view-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.url = reverse('audit_log_list')
        self.client.force_login(self.owner)

    def test_the_owner_sees_entries(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Created Branch Tarcan')

    def test_a_superuser_who_is_not_an_owner_can_also_view(self):
        admin = User.objects.create_user(
            username='root-admin', password='pw', role=User.Role.STAFF, is_superuser=True,
        )
        self.client.force_login(admin)

        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_nobody_else_can_view_it(self):
        for role in (User.Role.BRANCH_MANAGER, User.Role.STAFF, User.Role.SUPPLIER, User.Role.CUSTOMER):
            with self.subTest(role=role):
                user = User.objects.create_user(username=f'v-{role}', password='pw', role=role)
                self.client.force_login(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 403)

    def test_an_anonymous_visitor_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_filter_by_record_type_action_and_text(self):
        self.branch.location = 'Bulacan'
        self.branch.save()

        by_entity = self.client.get(self.url, {'entity': 'branches.Branch'})
        self.assertContains(by_entity, 'Created Branch Tarcan')
        self.assertNotContains(by_entity, 'Created User account')

        by_action = self.client.get(self.url, {'action': 'update'})
        self.assertContains(by_action, 'location Baliuag -&gt; Bulacan')
        self.assertNotContains(by_action, 'Created Branch Tarcan')

        by_text = self.client.get(self.url, {'q': 'Bulacan'})
        self.assertContains(by_text, 'location Baliuag -&gt; Bulacan')

    def test_filter_by_branch(self):
        """KATRINA-AUDIT-001: an Owner triaging one branch's activity could
        not narrow the list to it — every row already carried its own
        branch_id, nothing filtered on it."""
        other = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner,
        )

        by_this_branch = self.client.get(self.url, {'branch': self.branch.pk})
        self.assertContains(by_this_branch, 'Created Branch Tarcan')
        self.assertNotContains(by_this_branch, 'Created Branch Makinabang')

        by_other_branch = self.client.get(self.url, {'branch': other.pk})
        self.assertContains(by_other_branch, 'Created Branch Makinabang')
        self.assertNotContains(by_other_branch, 'Created Branch Tarcan')

        unfiltered = self.client.get(self.url)
        self.assertContains(unfiltered, 'Created Branch Tarcan')
        self.assertContains(unfiltered, 'Created Branch Makinabang')

    def test_a_non_numeric_branch_value_is_ignored_not_an_error(self):
        response = self.client.get(self.url, {'branch': 'not-a-number'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Created Branch Tarcan')

    def test_search_matches_the_actor_username(self):
        AuditLog.objects.create(
            action='update', entity='branches.Branch', record_id='1',
            actor_id=self.owner.pk, actor_role='OWNER', summary='Owner did this',
        )
        AuditLog.objects.create(
            action='update', entity='branches.Branch', record_id='1',
            actor_id=None, summary='System did this',
        )

        response = self.client.get(self.url, {'q': 'view-owner'})

        self.assertContains(response, 'Owner did this')
        self.assertNotContains(response, 'System did this')

    def test_date_range_presets_and_custom_range(self):
        today = timezone.localdate()
        AuditLog.objects.create(
            action='create', entity='branches.Branch', record_id='9', summary='Ancient entry',
            timestamp=timezone.now() - timedelta(days=45),
        )

        default = self.client.get(self.url)  # Last 30 days
        self.assertContains(default, 'Created Branch Tarcan')
        self.assertNotContains(default, 'Ancient entry')

        everything = self.client.get(self.url, {'range': 'all'})
        self.assertContains(everything, 'Ancient entry')
        self.assertContains(everything, 'Created Branch Tarcan')

        # The custom "to" day is inclusive, and a range ending before today
        # leaves today's entry out.
        past = self.client.get(self.url, {
            'range': 'custom',
            'date_from': (today - timedelta(days=60)).isoformat(),
            'date_to': (today - timedelta(days=10)).isoformat(),
        })
        self.assertContains(past, 'Ancient entry')
        self.assertNotContains(past, 'Created Branch Tarcan')

        just_today = self.client.get(self.url, {'range': 'today'})
        self.assertContains(just_today, 'Created Branch Tarcan')
        self.assertNotContains(just_today, 'Ancient entry')

    def test_an_unparseable_custom_range_falls_back_not_a_crash(self):
        response = self.client.get(self.url, {
            'range': 'custom', 'date_from': 'not-a-date', 'date_to': '2026-99-99',
        })

        self.assertEqual(response.status_code, 200)

    def test_the_before_and_after_values_are_shown_on_the_detail_page(self):
        """The list page shows the summary and a View processes link; the
        field-level before/after diff lives on the action's own detail page
        now, not inline (audit_action_detail)."""
        self.branch.location = 'Bulacan'
        self.branch.save()

        list_response = self.client.get(self.url, {'action': 'update'})
        self.assertContains(list_response, 'location Baliuag')
        self.assertContains(list_response, 'View processes')
        self.assertNotContains(list_response, 'View 1 field')

        (entry,) = _logs('branches.Branch', 'update')
        detail = self.client.get(reverse('audit_action_detail', args=[f'row_{entry.pk}']))
        self.assertContains(detail, 'Baliuag')
        self.assertContains(detail, 'Bulacan')
        self.assertContains(detail, 'View 1 field')

    def test_the_what_happened_cell_stays_a_table_cell_so_long_text_grows_its_row(self):
        """A long summary overlapped the rows below it because `.audit-what`
        put display:flex on the <td> itself: a td that is not a table-cell
        drops out of row-height layout, and .gv-table td's `height: 48px`
        (a minimum on a real cell) became an exact box. The flex layout has
        to live on a wrapper INSIDE the td."""
        AuditLog.objects.create(
            action='update', entity='branches.Branch', record_id='7',
            record_repr='x' * 200, actor_role='STAFF',
            summary='Updated Branch ' + ' '.join(['very-long-summary'] * 24),
        )

        html = self.client.get(self.url).content.decode()

        # Markup: the flex row is the inner wrapper, and the table opts in to
        # the audit wrapping rules on the elements that can hold a long token.
        self.assertIn('class="gv-table audit-table"', html)
        self.assertRegex(
            html, r'<td data-label="What happened" class="audit-what">\s*<div class="audit-what-body">',
        )
        self.assertIn('audit-wrap', html)

        # Stylesheet: no rule may turn the cell itself into a flex/grid box.
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'gerlyn.css').read_text(encoding='utf-8')
        css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
        for rule in re.finditer(r'(?P<sel>[^{}]+)\{(?P<body>[^{}]*)\}', css):
            selectors = [s.strip() for s in rule.group('sel').split(',')]
            if any(re.fullmatch(r'\.audit-what|td\.audit-what|\.audit-table td(\.audit-what)?', s)
                   for s in selectors):
                self.assertNotRegex(
                    rule.group('body'), r'display\s*:\s*(inline-)?(flex|grid)',
                    f'{rule.group("sel").strip()} must stay a table cell',
                )

    def test_a_removed_actor_is_labelled_not_a_crash(self):
        AuditLog.objects.create(
            action='delete', entity='branches.Branch', record_id='5',
            actor_id=999999, actor_role='STAFF', summary='By someone gone',
        )
        self.assertContains(self.client.get(self.url), 'User #999999 (removed)')

    def test_an_erased_actor_is_labelled_without_the_scrubbed_username(self):
        victim = User.objects.create_user(username='erase-me', password='pw', role=User.Role.STAFF)
        AuditLog.objects.create(
            action='update', entity='branches.Branch', record_id='5',
            actor_id=victim.pk, actor_role='STAFF', summary='Done by victim',
        )
        hard_delete_personal_data(victim, performed_by=self.owner)

        response = self.client.get(self.url)

        self.assertContains(response, f'User #{victim.pk} (personal data erased)')
        self.assertNotContains(response, 'deleted-')

    def test_pagination(self):
        AuditLog.objects.bulk_create([
            AuditLog(action='create', entity='branches.Branch', record_id=str(i), summary=f'Bulk entry {i}')
            for i in range(60)
        ])
        first = self.client.get(self.url)
        self.assertEqual(len(first.context['groups']), 50)
        self.assertTrue(first.context['page'].has_next())

        second = self.client.get(self.url, {'page': 2})
        self.assertGreater(len(second.context['groups']), 0)
        self.assertFalse(second.context['page'].has_next())

    def test_the_sidebar_link_is_owner_only(self):
        owner_page = self.client.get(reverse('reports_dashboard'))
        self.assertContains(owner_page, self.url)

        manager = User.objects.create_user(
            username='nav-manager', password='pw', role=User.Role.BRANCH_MANAGER
        )
        self.client.force_login(manager)
        manager_page = self.client.get(reverse('reports_dashboard'))
        self.assertNotContains(manager_page, self.url)

    def test_the_mobile_more_menu_links_it_too(self):
        # Below 860px the sidebar is hidden and the More menu is the only nav.
        page = self.client.get(reverse('reports_dashboard'))

        self.assertContains(page, f'href="{self.url}"', count=2)  # sidebar + More menu
        html = page.content.decode()
        more_menu = html.split('class="more-menu-panel"', 1)[1].split('</details>', 1)[0]
        self.assertIn(f'href="{self.url}">Audit Log</a>', more_menu)

    def test_the_dashboard_recent_activity_card_links_it_for_the_owner(self):
        page = self.client.get(reverse('accounts:owner_dashboard'))

        # Sidebar, More menu, and the Recent Activity card's header link.
        self.assertContains(page, f'href="{self.url}"', count=3)


class GroupedActionTests(TestCase):
    """One user-facing action can write several AuditLog rows (audit.views.
    _group_key groups them by request_id). These pin down that the list
    shows one row per action — not per write — with a human summary where
    one was set (audit.context.set_action_label) and the full detail
    reachable through it, while genuinely separate actions never merge."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='grp-owner', password='pw', role=User.Role.OWNER
        )
        self.list_url = reverse('audit_log_list')
        self.client.force_login(self.owner)

    def _supplier(self):
        supplier = User.objects.create_user(
            username='grp-supplier', password='pw', role=User.Role.SUPPLIER,
        )
        profile = supplier.supplier_profile
        profile.contact_number = '09170000000'
        profile.save()
        return supplier

    def test_a_multi_row_request_becomes_one_group_on_the_list(self):
        branch = Branch.objects.create(branch_name='B', location='L', owner=self.owner)
        self.client.post(reverse('accounts:user_add'), {
            'email': 'grp-invitee@example.com', 'role': User.Role.STAFF,
            'branch': branch.pk,
        })
        # The invite writes at least 2 rows (the User, its auto-created
        # StaffProfile) sharing one request_id — see
        # ActorTests.test_rows_from_one_request_share_a_request_id.
        invited = User.objects.get(email='grp-invitee@example.com')
        request_id = AuditLog.objects.filter(
            entity='accounts.User', record_id=str(invited.pk), action='create',
        ).get().request_id
        self.assertGreaterEqual(
            AuditLog.objects.filter(request_id=request_id).count(), 2,
        )

        response = self.client.get(self.list_url)

        groups = {g['key']: g for g in response.context['groups']}
        self.assertIn(request_id, groups)
        self.assertEqual(groups[request_id]['process_count'],
                          AuditLog.objects.filter(request_id=request_id).count())

    def test_hard_deleting_a_supplier_shows_the_labeled_summary(self):
        supplier = self._supplier()

        self.client.post(reverse('accounts:user_hard_delete', args=[supplier.pk]))

        response = self.client.get(self.list_url)
        self.assertContains(response, 'Deleted a user account')

        request_id = AuditLog.objects.filter(
            entity='accounts.User', record_id=str(supplier.pk), action='update',
        ).latest('id').request_id
        self.assertTrue(request_id)
        self.assertGreaterEqual(AuditLog.objects.filter(request_id=request_id).count(), 2)

        detail = self.client.get(reverse('audit_action_detail', args=[request_id]))
        self.assertContains(detail, 'Deleted a user account')
        self.assertContains(detail, 'User account')
        self.assertContains(detail, 'Supplier profile')

    def test_toggling_active_shows_deactivated_then_reactivated(self):
        target = User.objects.create_user(
            username='grp-toggle', password='pw', role=User.Role.STAFF,
        )

        self.client.post(reverse('accounts:user_toggle_active', args=[target.pk]))
        self.assertContains(self.client.get(self.list_url), 'Deactivated a user account')

        self.client.post(reverse('accounts:user_toggle_active', args=[target.pk]))
        self.assertContains(self.client.get(self.list_url), 'Reactivated a user account')

    def test_two_unrelated_system_rows_never_merge(self):
        """Both rows have request_id='' (no bound request) — grouping them
        together would wrongly present two unrelated direct writes as one
        action. Each must stay its own group."""
        Branch.objects.create(branch_name='First', location='L', owner=self.owner)
        Branch.objects.create(branch_name='Second', location='L', owner=self.owner)

        response = self.client.get(self.list_url)

        keys = [g['key'] for g in response.context['groups']]
        self.assertEqual(len(keys), len(set(keys)))  # no accidental merge
        self.assertTrue(all(k.startswith('row_') for k in keys))

    def test_filtering_by_a_secondary_records_entity_returns_the_whole_group(self):
        """The Supplier profile row is not the group's primary (the User
        row is, created first) — filtering to just the profile's entity
        must still surface the whole action, per the user's own
        requirement that a filter match on any row in the group. _supplier()
        itself also touches SupplierProfile (creating it, then setting a
        contact number) outside of any request, each its own unrelated
        singleton group — those must stay separate, not merge into or stand
        in for the hard-delete's own group."""
        supplier = self._supplier()
        self.client.post(reverse('accounts:user_hard_delete', args=[supplier.pk]))
        request_id = AuditLog.objects.filter(
            entity='accounts.User', record_id=str(supplier.pk), action='update',
        ).latest('id').request_id

        response = self.client.get(self.list_url, {'entity': 'accounts.SupplierProfile'})

        groups = {g['key']: g for g in response.context['groups']}
        self.assertIn(request_id, groups)  # the whole action, found via a non-primary row
        self.assertEqual(groups[request_id]['entity_label'], 'User account')  # its primary, not the filter hit
        self.assertEqual(groups[request_id]['summary_text'], 'Deleted a user account')

    def test_the_detail_page_is_owner_only(self):
        branch = Branch.objects.create(branch_name='B', location='L', owner=self.owner)
        entry = AuditLog.objects.filter(entity='branches.Branch', record_id=str(branch.pk)).get()
        url = reverse('audit_action_detail', args=[f'row_{entry.pk}'])

        manager = User.objects.create_user(
            username='grp-manager', password='pw', role=User.Role.BRANCH_MANAGER
        )
        self.client.force_login(manager)
        response = self.client.get(url)

        self.assertEqual(response.status_code, 403)

    def test_the_detail_page_404s_for_an_unknown_group(self):
        self.assertEqual(
            self.client.get(reverse('audit_action_detail', args=['row_999999'])).status_code, 404,
        )
        self.assertEqual(
            self.client.get(reverse('audit_action_detail', args=['not-a-real-request-id'])).status_code,
            404,
        )
