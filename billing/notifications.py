"""Notification events emitted by payment-verification decisions."""
from django.urls import reverse

from core.notifications import (
    EVENT_PAYMENT_REJECTED,
    EVENT_PAYMENT_VERIFIED,
    notify_event,
)

from .models import BillingRecord


def notify_payment_decision(record, actor, *, include_internal=True):
    txn = record.transaction
    customer_url = reverse(
        'orders:track_order', kwargs={'transaction_id': txn.pk}
    )

    if record.status == BillingRecord.PaymentStatus.PAID:
        return notify_event(
            EVENT_PAYMENT_VERIFIED,
            f'Payment verified: {txn.order_number}',
            f'Your payment of \u20b1{record.amount_due} for {txn.order_number} '
            f'has been verified.',
            branch=txn.branch,
            actor=actor,
            related_users={'customer': txn.customer},
            action_url=customer_url,
            action_label='View Order',
            reference_type='billing_record',
            reference_id=record.pk,
        )

    if record.status == BillingRecord.PaymentStatus.REJECTED:
        return notify_event(
            EVENT_PAYMENT_REJECTED,
            f'Payment Alert: {txn.order_number} rejected',
            f'Payment for {txn.order_number} ({txn.branch.branch_name}, '
            f'\u20b1{txn.total_amount}) was rejected by {actor.username}.',
            branch=txn.branch,
            actor=actor,
            related_users={'customer': txn.customer},
            action_url=reverse('billing_detail', kwargs={'pk': record.pk}),
            action_label='View Billing Record',
            reference_type='billing_record',
            reference_id=record.pk,
            audience_overrides={
                'customer': {
                    'title': f'Payment needs attention: {txn.order_number}',
                    'message': (
                        f'Your payment for {txn.order_number} could not be '
                        f'verified. Review the order and contact the branch '
                        f'with corrected payment details.'
                    ),
                    'action_url': customer_url,
                    'action_label': 'View Order',
                },
            },
            audiences=None if include_internal else ('customer',),
        )

    return 0
