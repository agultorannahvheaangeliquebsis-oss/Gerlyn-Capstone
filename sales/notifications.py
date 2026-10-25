"""Notification events emitted by sales/order state changes."""
from django.urls import reverse

from core.notifications import (
    EVENT_ORDER_DELIVERED, EVENT_ORDER_RECEIPT_CONFIRMED, notify_event,
)


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


def notify_receipt_confirmed(transaction, conf, actor, *, by_customer):
    """Tell the ordering customer a receipt confirmation was recorded.

    Sent for either side: when staff record the hand-over (the message carries
    the receipt number and links to the receipt), and when the customer taps
    Confirm themselves (so it also lands in their notification inbox, and says
    when the order has become fully confirmed). Skipped quietly for an order
    whose customer account is gone — related_users then has no recipient.
    """
    order = transaction.order_number
    if by_customer:
        title = f'Receipt confirmed: {order}'
        message = (
            f'Thanks — you confirmed you received {order}.'
            + (' Both you and the store have now confirmed the hand-over.'
               if conf.is_fully_confirmed else
               ' The store will record the hand-over on their side.')
        )
    else:
        title = f'Order handed over: {order}'
        message = (
            f'{transaction.branch.branch_name} recorded {order} as received '
            f'(receipt {conf.receipt_number}).'
            + (' Both you and the store have now confirmed the hand-over.'
               if conf.is_fully_confirmed else
               ' Open your order and confirm that you received it.')
        )
    # The receipt exists once staff have recorded the hand-over; until then
    # (customer confirmed first) the Track Order page is the useful link.
    if conf.staff_confirmed:
        action_url = reverse('orders:receipt', kwargs={'transaction_id': transaction.pk})
        action_label = 'View Receipt'
    else:
        action_url = reverse('orders:track_order', kwargs={'transaction_id': transaction.pk})
        action_label = 'View Order'
    return notify_event(
        EVENT_ORDER_RECEIPT_CONFIRMED,
        title, message,
        branch=transaction.branch,
        actor=actor,
        related_users={'customer': transaction.customer},
        action_url=action_url,
        action_label=action_label,
        reference_type='sales_transaction',
        reference_id=transaction.pk,
    )


def _refund_notice(refund, title, message):
    # Deferred imports — same as the notify() call sites in sales.views, so a
    # test patching core.notifications.notify sees these too.
    from core.module_permissions import SALES_POS
    from core.notifications import CATEGORY_SALES, PRIORITY_HIGH, PRIORITY_MEDIUM, notify

    txn = refund.transaction
    return notify(
        CATEGORY_SALES,
        PRIORITY_HIGH if refund.status == refund.Status.PENDING else PRIORITY_MEDIUM,
        title, message,
        branch=txn.branch,
        action_url=reverse('transaction_detail', kwargs={'pk': txn.pk}),
        action_label='View Order',
        required_module=SALES_POS,
        reference_type='refund',
        reference_id=refund.pk,
    )


def notify_refund_requested(refund, actor):
    """A refund is waiting for an Owner/Branch Manager decision."""
    txn = refund.transaction
    return _refund_notice(
        refund,
        f'Refund needs approval — {txn.order_number}',
        f'₱{refund.total_amount} ({refund.get_reason_code_display()}) was requested on '
        f'{txn.order_number} ({txn.branch.branch_name}) by {actor.username}.',
    )


def notify_refund_decided(refund, actor):
    txn = refund.transaction
    if refund.status == refund.Status.APPROVED:
        title = f'Refund issued — {txn.order_number}'
        message = (
            f'₱{refund.total_amount} refunded on {txn.order_number} '
            f'({txn.branch.branch_name}) — approved by {actor.username}.'
        )
    else:
        title = f'Refund rejected — {txn.order_number}'
        message = (
            f'The ₱{refund.total_amount} refund request on {txn.order_number} '
            f'({txn.branch.branch_name}) was rejected by {actor.username}: {refund.decision_note}'
        )
    return _refund_notice(refund, title, message)
