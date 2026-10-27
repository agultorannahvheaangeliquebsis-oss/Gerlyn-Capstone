"""A delivery order's serviceable area in the Order Console, and the override.

A customer cannot place a delivery the branch does not serve, so an order only
ends up outside when the branch's list changed after it was placed. These
tests pin down what staff see, what is held, and who may let it through:

  * Staff and Branch Managers see the delivery area on the order and in the
    list, and any override is flagged;
  * Approving an outside delivery is refused until an Owner or the order's
    Branch Manager overrides it with a reason (Staff never can);
  * the override is recorded on the order and in the Audit Log;
  * an order whose area was never recorded is not held at all.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, StaffProfile, User
from audit.models import AuditLog
from branches.models import Branch, ServiceArea
from branches.tests_service_areas import BALIUAG, BULACAN, POBLACION, Type, area
from sales import area_override
from sales.models import SalesItem, SalesTransaction

Status = SalesTransaction.OrderStatus
REASON = 'Regular customer, rider passes there daily'


class AreaOrderBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='ao-owner', password='pw', role=User.Role.OWNER)
        self.branch = Branch.objects.create(branch_name='Tarcan', location='Baliuag', owner=self.owner)
        self.other_branch = Branch.objects.create(branch_name='Makinabang', location='Baliuag', owner=self.owner)
        self.manager = self.make_user('ao-mgr', User.Role.BRANCH_MANAGER, BranchManagerProfile,
                                      'manager_name', self.branch)
        self.other_manager = self.make_user('ao-mgr2', User.Role.BRANCH_MANAGER, BranchManagerProfile,
                                            'manager_name', self.other_branch)
        self.staff = self.make_user('ao-staff', User.Role.STAFF, StaffProfile, 'staff_name', self.branch)
        self.customer = User.objects.create_user(username='ao-cust', password='pw', role=User.Role.CUSTOMER)
        self.province = area(self.branch, BULACAN, Type.PROVINCE)

    @staticmethod
    def make_user(username, role, profile_model, name_field, branch):
        user = User.objects.create_user(username=username, password='pw', role=role)
        profile_model.objects.update_or_create(
            user=user, defaults={name_field: username, 'branch': branch})
        return user

    def order(self, *, place=POBLACION, status=Status.PENDING, fulfillment='Delivery'):
        txn = SalesTransaction.objects.create(
            branch=self.branch, customer=self.customer,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            order_status=status, fulfillment_type=fulfillment,
            mode_of_payment=SalesTransaction.ModeOfPayment.COD, total_amount=224,
            delivery_address='14 Rizal St., Brgy. Poblacion, Baliuag, Bulacan' if fulfillment == 'Delivery' else '',
            delivery_province_code=place.province_code if place else '',
            delivery_city_code=place.city_code if place else '',
            delivery_barangay_code=place.barangay_code if place else '',
        )
        SalesItem.objects.create(transaction=txn, product=self.product(), quantity_sold=2, unit_price=112)
        return txn

    def product(self):
        from inventory.models import Product
        product, _ = Product.objects.get_or_create(
            sku_code='SKU-AO', defaults={'product_name': 'Rice', 'unit_of_measure': 'Sack', 'unit_price': 112})
        return product

    def exclude_baliuag(self):
        return area(self.branch, BALIUAG, Type.MUNICIPALITY, active=False)

    def approve(self, txn):
        return self.client.post(reverse('transaction_set_status', args=[txn.pk, 'Approved']))

    def override(self, txn, reason=REASON):
        return self.client.post(reverse('transaction_area_override', args=[txn.pk]),
                                {'override_reason': reason})

    def detail(self, txn):
        return self.client.get(reverse('transaction_detail', args=[txn.pk])).content.decode()


class AreaStateTests(AreaOrderBase):
    def test_a_delivery_inside_the_areas_is_not_outside(self):
        state = area_override.area_state(self.order())
        self.assertTrue(state['applies'])
        self.assertFalse(state['outside'])
        self.assertEqual(state['label'], 'Brgy. Poblacion, Baliuag, Bulacan')

    def test_a_delivery_the_branch_stopped_serving_is_outside_and_can_be_overridden(self):
        txn = self.order()
        self.exclude_baliuag()

        state = area_override.area_state(txn)

        self.assertTrue(state['outside'])
        self.assertTrue(state['can_be_overridden'])

    def test_an_order_with_no_recorded_area_is_unknown_never_outside(self):
        txn = self.order(place=None)
        self.exclude_baliuag()

        state = area_override.area_state(txn)

        self.assertFalse(state['recorded'])
        self.assertFalse(state['outside'])
        self.assertEqual(area_override.approval_block_message(txn), '')

    def test_pickup_has_no_delivery_area(self):
        self.assertFalse(area_override.area_state(self.order(fulfillment='Pickup', place=None))['applies'])

    def test_a_branch_with_no_areas_never_holds_an_order(self):
        txn = self.order()
        ServiceArea.objects.all().delete()
        self.assertFalse(area_override.area_state(txn)['outside'])

    def test_a_completed_order_can_no_longer_be_overridden(self):
        txn = self.order(status=Status.COMPLETED)
        self.exclude_baliuag()
        self.assertFalse(area_override.area_state(txn)['can_be_overridden'])


class StaffViewsTests(AreaOrderBase):
    def test_staff_and_managers_see_the_delivery_area_on_the_order(self):
        txn = self.order()
        for user in (self.staff, self.manager, self.owner):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                html = self.detail(txn)
                self.assertIn('data-delivery-area', html)
                self.assertIn('Brgy. Poblacion, Baliuag, Bulacan', html)
                self.assertIn('data-area-flag="covered"', html)

    def test_the_list_shows_the_area_of_a_delivery(self):
        self.order()
        self.client.force_login(self.manager)

        html = self.client.get(reverse('transaction_list')).content.decode()

        self.assertIn('data-delivery-area-label', html)
        self.assertIn('Brgy. Poblacion, Baliuag, Bulacan', html)

    def test_an_outside_order_is_flagged_and_only_owner_and_manager_get_the_form(self):
        txn = self.order()
        self.exclude_baliuag()

        self.client.force_login(self.staff)
        staff_html = self.detail(txn)
        self.assertIn('data-area-flag="outside"', staff_html)
        self.assertIn('data-area-held', staff_html)
        self.assertNotIn('data-area-override-form', staff_html)
        self.assertIn('Only the Owner or a Branch Manager can override this', staff_html)

        for user in (self.manager, self.owner):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                self.assertIn('data-area-override-form', self.detail(txn))

        self.client.force_login(self.other_manager)
        self.assertEqual(self.client.get(reverse('transaction_detail', args=[txn.pk])).status_code, 302)

    def test_an_overridden_order_is_flagged_everywhere_staff_look(self):
        txn = self.order()
        self.exclude_baliuag()
        self.client.force_login(self.manager)
        self.override(txn)

        for user in (self.staff, self.manager, self.owner):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                html = self.detail(txn)
                self.assertIn('data-area-flag="overridden"', html)
                self.assertIn(REASON, html)
                self.assertIn('ao-mgr', html)
                self.assertNotIn('data-area-override-form', html)
                lst = self.client.get(reverse('transaction_list')).content.decode()
                self.assertIn('data-area-flag="overridden"', lst)

    def test_an_order_that_was_never_overridden_carries_no_override_flag_in_the_list(self):
        self.order()
        self.client.force_login(self.owner)
        html = self.client.get(reverse('transaction_list')).content.decode()
        self.assertNotIn('data-area-flag="overridden"', html)


class ApprovalGateTests(AreaOrderBase):
    def test_approving_an_outside_delivery_is_held_for_everyone_until_overridden(self):
        txn = self.order()
        self.exclude_baliuag()

        for user in (self.staff, self.manager, self.owner):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                response = self.approve(txn)
                txn.refresh_from_db()
                self.assertEqual(txn.order_status, Status.PENDING)
                self.assertIn(
                    'does not deliver to',
                    ' '.join(m.message for m in response.wsgi_request._messages))

    def test_cancelling_a_held_order_is_still_allowed(self):
        txn = self.order()
        self.exclude_baliuag()
        self.client.force_login(self.staff)

        self.client.post(reverse('transaction_set_status', args=[txn.pk, 'Cancelled']),
                         {'reason': 'Outside our delivery areas'})

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.CANCELLED)

    def test_an_overridden_order_can_be_approved(self):
        txn = self.order()
        self.exclude_baliuag()
        self.client.force_login(self.owner)
        self.override(txn)

        self.client.force_login(self.staff)
        self.approve(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.APPROVED)

    def test_orders_the_branch_serves_or_cannot_judge_are_approved_as_before(self):
        covered = self.order()
        unrecorded = self.order(place=None)
        pickup = self.order(fulfillment='Pickup', place=None)
        self.client.force_login(self.staff)

        for txn in (covered, unrecorded, pickup):
            self.approve(txn)
            txn.refresh_from_db()
            self.assertEqual(txn.order_status, Status.APPROVED, txn.pk)


class OverridePermissionTests(AreaOrderBase):
    def setUp(self):
        super().setUp()
        self.txn = self.order()
        self.exclude_baliuag()

    def unchanged(self):
        self.txn.refresh_from_db()
        self.assertFalse(self.txn.has_area_override)
        self.assertEqual(self.txn.area_override_reason, '')
        self.assertIsNone(self.txn.area_override_by)

    def test_the_owner_can_override(self):
        self.client.force_login(self.owner)
        self.override(self.txn)

        self.txn.refresh_from_db()
        self.assertTrue(self.txn.has_area_override)
        self.assertEqual(self.txn.area_override_reason, REASON)
        self.assertEqual(self.txn.area_override_by, self.owner)
        self.assertIsNotNone(self.txn.area_override_at)

    def test_the_orders_branch_manager_can_override(self):
        self.client.force_login(self.manager)
        self.override(self.txn)

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.area_override_by, self.manager)

    def test_staff_cannot_override(self):
        self.client.force_login(self.staff)

        self.assertEqual(self.override(self.txn).status_code, 403)

        self.unchanged()

    def test_a_manager_of_another_branch_cannot_override(self):
        self.client.force_login(self.other_manager)

        response = self.override(self.txn)

        self.assertRedirects(response, reverse('transaction_list'))
        self.unchanged()

    def test_a_customer_and_a_stranger_cannot_override(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.override(self.txn).status_code, 403)
        self.client.logout()
        self.assertEqual(self.override(self.txn).status_code, 302)
        self.unchanged()

    def test_a_manager_whose_account_may_not_change_orders_cannot_override(self):
        self.manager.module_actions = {'sales_pos': []}
        self.manager.save()
        self.client.force_login(self.manager)
        self.override(self.txn)
        self.txn.refresh_from_db()
        self.assertFalse(self.txn.has_area_override)

    def test_only_a_post_overrides(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse('transaction_area_override', args=[self.txn.pk])).status_code, 405)
        self.unchanged()


class OverrideRulesTests(AreaOrderBase):
    def setUp(self):
        super().setUp()
        self.txn = self.order()
        self.exclude_baliuag()
        self.client.force_login(self.owner)

    def refused(self, response, text):
        self.txn.refresh_from_db()
        self.assertFalse(self.txn.has_area_override)
        self.assertIn(text, ' '.join(m.message for m in response.wsgi_request._messages))

    def test_a_reason_is_required(self):
        for reason in ('', '   ', 'short'):
            with self.subTest(reason=reason):
                self.refused(self.override(self.txn, reason), 'Give a reason')

    def test_the_reason_has_a_length_limit(self):
        self.refused(self.override(self.txn, 'x' * 256), 'cannot be longer than 255')

    def test_the_reason_is_tidied_but_otherwise_kept(self):
        self.override(self.txn, '  Regular   customer,\n rider passes there  ')
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.area_override_reason, 'Regular customer, rider passes there')

    def test_there_is_nothing_to_override_on_an_order_inside_the_areas(self):
        inside = self.order(place=self.place_in_malolos())
        self.assertFalse(area_override.area_state(inside)['outside'])
        response = self.override(inside)
        inside.refresh_from_db()
        self.assertFalse(inside.has_area_override)
        self.assertIn('nothing to override', ' '.join(m.message for m in response.wsgi_request._messages))

    def place_in_malolos(self):
        from accounts import geo
        return geo.resolve_place('Bulacan', 'Malolos', 'Mojon')

    def test_an_order_is_overridden_once(self):
        self.override(self.txn)
        first = SalesTransaction.objects.get(pk=self.txn.pk).area_override_at

        response = self.override(self.txn, 'Another reason entirely')

        again = SalesTransaction.objects.get(pk=self.txn.pk)
        self.assertEqual(again.area_override_at, first)
        self.assertEqual(again.area_override_reason, REASON)
        self.assertIn('already has', ' '.join(m.message for m in response.wsgi_request._messages))

    def test_a_finished_order_cannot_be_overridden(self):
        done = self.order(status=Status.COMPLETED)
        response = self.override(done)
        done.refresh_from_db()
        self.assertFalse(done.has_area_override)
        self.assertIn('can no longer be overridden', ' '.join(m.message for m in response.wsgi_request._messages))

    def test_pickup_has_nothing_to_override(self):
        pickup = self.order(fulfillment='Pickup', place=None)
        self.override(pickup)
        pickup.refresh_from_db()
        self.assertFalse(pickup.has_area_override)


class OverrideAuditTests(AreaOrderBase):
    def test_the_override_is_in_the_audit_log_with_who_why_and_which_order(self):
        txn = self.order()
        self.exclude_baliuag()
        self.client.force_login(self.manager)

        self.override(txn)

        entries = AuditLog.objects.filter(
            entity='sales.SalesTransaction', record_id=str(txn.pk),
            action_label='Overrode a delivery area restriction')
        self.assertEqual(entries.count(), 1)
        entry = entries.get()
        self.assertEqual(entry.action, 'update')
        self.assertEqual(entry.actor_id, self.manager.pk)
        self.assertEqual(entry.actor_role, 'BRANCH_MANAGER')
        self.assertEqual(entry.branch_id, self.branch.pk)
        self.assertEqual(entry.record_repr, txn.order_number)
        self.assertEqual(entry.after['area_override_reason'], REASON)
        self.assertEqual(entry.after['area_override_by_id'], self.manager.pk)

    def test_a_refused_override_leaves_no_entry(self):
        txn = self.order()
        self.exclude_baliuag()
        self.client.force_login(self.staff)

        self.override(txn)
        self.client.force_login(self.owner)
        self.override(txn, '')

        self.assertFalse(AuditLog.objects.filter(
            action_label='Overrode a delivery area restriction').exists())
