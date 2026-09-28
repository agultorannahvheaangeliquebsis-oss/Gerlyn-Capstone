"""Order Detail as the single source of truth for one order.

The page folds what used to be a Next Action card, a "View billing record"
link and two Status History tables into the status strip, the Payment Status
card and one collapsed timeline. These tests pin down what has to stay
reachable from the order itself, and what must no longer be drawn.
"""
from django.urls import reverse

from billing.models import BillingRecord
from sales.models import OrderStatusChange, OrderStatusHistory
from sales.tests_order_status_confirmation import Status, _OrderStatusBase
from sales.views import _status_timeline


class OrderDetailLayoutTests(_OrderStatusBase):
    def _html(self, txn, query=''):
        response = self.client.get(reverse('transaction_detail', args=[txn.pk]) + query)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    # -- status strip ------------------------------------------------------------

    def test_next_action_card_is_gone_and_its_steps_sit_in_the_status_strip(self):
        txn = self._order(Status.PENDING)
        html = self._html(txn)
        self.assertNotIn('Next Action', html)
        # The steps come before the order details rather than in a card of
        # their own beneath the progress track.
        self.assertLess(html.index('Mark Approved'), html.index('Order Details'))
        self.assertLess(html.index('Order Progress'), html.index('Mark Approved'))

    def test_the_status_message_is_gone_because_the_progress_track_says_it(self):
        txn = self._order(Status.PENDING)
        self.assertNotIn('This order is', self._html(txn))

    def test_next_steps_are_one_dropdown_not_a_button_each(self):
        txn = self._order(Status.PENDING)
        html = self._html(txn)
        select = html.split('data-status-select>')[1].split('</select>')[0]
        self.assertIn('Change status', select)
        self.assertIn('Mark Approved', select)
        self.assertIn('Cancel Order', select)
        # Each option points at its own step's form, which keeps its own URL.
        for target in ('Approved', 'Cancelled'):
            self.assertIn(f'value="od-status-form-{target}"', select)
            self.assertIn(
                f'id="od-status-form-{target}" class="od-action"', html
            )
            self.assertIn(reverse('transaction_set_status', args=[txn.pk, target]), html)
        # One Update button, unusable until a step is picked.
        self.assertEqual(html.count('data-status-apply'), 1)
        self.assertIn('data-status-apply disabled', html)

    def test_completed_is_listed_but_disabled_until_the_payment_is_paid(self):
        txn = self._order(Status.PREPARING)
        html = self._html(txn)
        option = html.split('value="od-status-form-Completed"')[1].split('</option>')[0]
        self.assertIn('disabled', option)
        self.assertIn('Payment must be verified as Paid first.', html)

        BillingRecord.objects.filter(transaction=txn).update(status=BillingRecord.PaymentStatus.PAID)
        html = self._html(txn)
        option = html.split('value="od-status-form-Completed"')[1].split('</option>')[0]
        self.assertNotIn('disabled', option)
        self.assertNotIn('Payment must be verified as Paid first.', html)

    def test_no_dropdown_for_a_cancelled_order(self):
        txn = self._order(Status.PENDING)
        self._post(txn, 'Cancelled', {'reason': 'Duplicate'})
        html = self._html(txn)
        self.assertNotIn('data-status-select', html)
        self.assertNotIn('data-status-apply', html)

    def test_a_cancelled_order_shows_its_reason_in_the_status_strip(self):
        txn = self._order(Status.PENDING)
        self._post(txn, 'Cancelled', {'reason': 'Customer changed their mind'})
        html = self._html(txn)
        self.assertIn('was cancelled', html)
        self.assertIn('Customer changed their mind', html)
        self.assertNotIn('<h3>Cancellation</h3>', html)

    # -- payment status ----------------------------------------------------------

    def test_payment_status_shows_the_billing_record_inline(self):
        txn = self._order(Status.APPROVED, paid=True)
        BillingRecord.objects.filter(transaction=txn).update(
            payment_method=BillingRecord.PaymentMethod.GCASH,
            reference_number='GC-INLINE-42', confirmed_by=self.owner,
        )
        record = txn.billing_record
        html = self._html(txn)
        self.assertIn('Payment Status', html)
        self.assertIn(f'#{record.pk}', html)
        self.assertIn('GCash', html)
        self.assertIn('GC-INLINE-42', html)
        self.assertIn('Confirmed by', html)
        self.assertIn('Paid', html)
        self.assertNotIn('View billing record', html)
        self.assertNotIn(f'href="/billing/{record.pk}/"', html)

    def test_view_payment_proof_only_appears_when_a_proof_is_on_file(self):
        txn = self._order(Status.PENDING)
        record = txn.billing_record
        proof_url = reverse('billing_proof', args=[record.pk])
        self.assertNotIn('View payment proof', self._html(txn))

        BillingRecord.objects.filter(pk=record.pk).update(proof_of_payment='payment_proofs/p.png')
        html = self._html(txn)
        self.assertIn('View payment proof', html)
        self.assertIn(f'href="{proof_url}"', html)

    def test_record_payment_is_offered_only_while_nothing_is_verified(self):
        txn = self._order(Status.PENDING)
        create_url = reverse('billing_create', args=[txn.pk])
        self.assertIn(create_url, self._html(txn))

        BillingRecord.objects.filter(transaction=txn).update(status=BillingRecord.PaymentStatus.PAID)
        self.assertNotIn(create_url, self._html(txn))

    def test_payment_method_is_not_printed_in_both_cards(self):
        txn = self._order(Status.PENDING)
        # With a billing record, the method is that card's "Method" tile...
        self.assertNotIn('<dt>Payment method</dt>', self._html(txn))
        # ...and without one, Order Details is the only place left to read it.
        BillingRecord.objects.filter(transaction=txn).delete()
        self.assertIn('<dt>Payment method</dt>', self._html(txn))

    # -- items -------------------------------------------------------------------

    def test_payment_comes_first_then_progress_then_the_details(self):
        txn = self._order(Status.PENDING)
        html = self._html(txn)
        payment = html.index('Payment Status</h3>')
        progress = html.index('Order Progress</h3>')
        select = html.index('data-status-select')
        details = html.index('Order Details</h3>')
        # The status control depends on the payment, so it is read first, and
        # the dropdown sits with the progress track it moves.
        self.assertLess(payment, progress)
        self.assertLess(progress, select)
        self.assertLess(select, details)
        self.assertEqual(html.count('class="od-top"'), 1)

    def test_from_billing_the_top_row_is_the_progress_card_alone(self):
        txn = self._order(Status.PENDING)
        html = self._html(txn, '?from=billing')
        self.assertNotIn('Payment Status</h3>', html)
        self.assertIn('Order Progress</h3>', html)
        self.assertIn('Order Details</h3>', html)
        self.assertIn('View order items', html)

    # -- items -------------------------------------------------------------------

    def test_items_are_a_disclosure_inside_order_details_not_a_card_of_their_own(self):
        txn = self._order(Status.PENDING)
        html = self._html(txn)
        self.assertNotIn('Items Ordered', html)
        self.assertNotIn('class="od-col"', html)
        details = html.index('Order Details</h3>')
        toggle = html.index('<details class="od-items-toggle">')
        total = html.index('<div class="od-total">')
        refund_or_history = html.index('<details class="card od-history">')
        # Facts, then the items disclosure, then the totals they add up to.
        self.assertLess(details, toggle)
        self.assertLess(toggle, total)
        self.assertLess(total, refund_or_history)
        # Closed by default.
        self.assertNotIn('<details class="od-items-toggle" open', html)
        self.assertIn('View order items', html)

    def test_items_are_listed_once_in_either_view(self):
        txn = self._order(Status.PENDING)
        for query in ('', '?from=billing'):
            html = self._html(txn, query)
            self.assertEqual(html.count('Rice (Sack)'), 1, query)
            self.assertIn('View order items', html)
            self.assertLess(html.index('Order Details'), html.index('View order items'))

    # -- discount ----------------------------------------------------------------

    def test_discount_form_sits_behind_a_checkbox_that_is_never_posted(self):
        txn = self._order(Status.PENDING)
        html = self._html(txn)
        self.assertIn('Apply PWD / Senior Citizen discount', html)
        toggle = html.index('id="discount_toggle"')
        body = html.index('class="od-discount-body"')
        self.assertLess(toggle, body)
        # The extra fields are inside the collapsible body, after the tick box,
        # and the tick box itself carries no name so it adds nothing to the POST.
        for field in ('id="discount_type"', 'id="discount_id_number"', 'id="discount_holder_name"'):
            self.assertLess(body, html.index(field))
        tag = html[html.rindex('<input', 0, toggle):toggle + 30]
        self.assertNotIn('name=', tag)

    # -- status history ----------------------------------------------------------

    def test_status_history_is_one_collapsed_timeline_with_each_reason_once(self):
        txn = self._order(Status.PREPARING, paid=True)
        self._post(txn, 'Completed', {'reason': 'Handed to rider'})
        html = self._html(txn)
        self.assertEqual(html.count('Status History'), 1)
        self.assertEqual(html.count('Handed to rider'), 1)
        self.assertIn('<details class="card od-history">', html)
        self.assertNotIn('<details class="card od-history" open', html)


class StatusTimelineTests(_OrderStatusBase):
    def test_reason_from_the_change_row_is_folded_onto_its_history_step(self):
        txn = self._order(Status.PREPARING, paid=True)
        self._post(txn, 'Completed', {'reason': 'Handed to rider'})
        steps = _status_timeline(txn)
        self.assertEqual([s['to_status'] for s in steps], ['Preparing', 'Completed'])
        self.assertEqual(steps[0]['from_status'], '')
        self.assertEqual(steps[-1]['note'], 'Handed to rider')
        self.assertEqual(steps[-1]['by'], self.owner)

    def test_a_change_with_no_history_row_still_appears(self):
        txn = self._order(Status.PENDING)
        OrderStatusHistory.objects.filter(transaction=txn).delete()
        OrderStatusChange.objects.create(
            transaction=txn, from_status=Status.PENDING, to_status=Status.APPROVED,
            reason='Legacy step', changed_by=self.owner,
        )
        steps = _status_timeline(txn)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['note'], 'Legacy step')

    def test_an_order_with_no_trail_gives_an_empty_timeline(self):
        txn = self._order(Status.PENDING)
        OrderStatusHistory.objects.filter(transaction=txn).delete()
        self.assertEqual(_status_timeline(txn), [])
