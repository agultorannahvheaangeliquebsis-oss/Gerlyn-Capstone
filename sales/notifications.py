"""Notification events emitted by sales/order state changes."""
from django.urls import reverse

from core.notifications import EVENT_ORDER_DELIVERED, notify_event


def notify_order_delivered(transaction, actor):
    """Tell the ordering customer when a Delivery order is completed."""
    return notify_event(
        EVENT_ORDER_DELIVERED,
        f'Order delivered: {transaction.order_number}',
        f'{transaction.order_number} has been marked delivered by '
        f'{transaction.branch.branch_name}.',
        branch=transaction.branch,
        actor=actor,
        related_users={'customer': transaction.customer},
        action_url=reverse(
            'orders:track_order', kwargs={'transaction_id': transaction.pk}
        ),
        action_label='View Order',
        reference_type='sales_transaction',
        reference_id=transaction.pk,
    )
