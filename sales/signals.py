from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse

from core.module_permissions import SALES_POS
from core.notifications import CATEGORY_SALES, LARGE_ORDER_THRESHOLD, PRIORITY_HIGH, notify

from .models import OrderStatusHistory, SalesTransaction


@receiver(post_save, sender=SalesTransaction)
def notify_large_order(sender, instance, created, update_fields, **kwargs):
    """Owner Notification Center trigger: an unusually large order, walk-in
    or online — both create a SalesTransaction, so one signal covers both
    entry points instead of duplicating the check in sales/views.py and
    orders/customer_views.py.

    SalesTransaction.save() does an initial insert with order_number still
    blank (created=True), then immediately a second save assigning it
    (update_fields={'order_number'}, created=False) — this signal fires on
    BOTH, and only the second carries a real order_number, so that's the one
    this waits for. Gating on `created` alone (as a first version of this did)
    fired on the blank-order_number insert and produced a notification titled
    "Large order placed — " with no number in it.
    """
    if not (update_fields and 'order_number' in update_fields):
        return
    if instance.total_amount < LARGE_ORDER_THRESHOLD or not instance.branch_id:
        return

    notify(
        CATEGORY_SALES, PRIORITY_HIGH,
        f'Large order placed — {instance.order_number}',
        f'{instance.order_number} at {instance.branch.branch_name} totals '
        f'₱{instance.total_amount} — above the ₱{LARGE_ORDER_THRESHOLD} '
        f'review threshold.',
        branch=instance.branch,
        action_url=reverse('transaction_detail', kwargs={'pk': instance.pk}),
        action_label='View Order',
        required_module=SALES_POS,
    )


@receiver(post_save, sender=SalesTransaction)
def record_initial_status(sender, instance, created, **kwargs):
    """First row of the order's status history: the status it was created in
    (Pending for an online order, Completed for a paid walk-in). Every later
    step is written by sales.views.transaction_set_status. The actor is
    whoever operated the till, or the customer for an online order."""
    if not created:
        return
    OrderStatusHistory.objects.create(
        transaction=instance, from_status='', to_status=instance.order_status,
        actor=instance.recorded_by or instance.customer,
        note='Order created.',
    )
