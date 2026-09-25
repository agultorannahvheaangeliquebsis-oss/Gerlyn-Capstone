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
