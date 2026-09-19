"""Notification events emitted by procurement state changes."""
from django.urls import reverse

from core.notifications import (
    EVENT_INCOMING_DELIVERY,
    EVENT_PO_APPROVED,
    notify_event,
)


def notify_po_approved(procurement, actor):
    return notify_event(
        EVENT_PO_APPROVED,
        f'{procurement.po_number} approved',
        f'{procurement.po_number} for {procurement.branch.branch_name} was '
        f'approved by {actor.username}.',
        branch=procurement.branch,
        actor=actor,
        related_users={
            'requester': procurement.created_by,
            'supplier': procurement.supplier,
        },
        action_url=reverse('procurement_detail', kwargs={'pk': procurement.pk}),
        action_label='View PO',
        reference_type='procurement',
        reference_id=procurement.pk,
        audience_overrides={
            'supplier': {
                'title': f'New purchase order: {procurement.po_number}',
                'message': (
                    f'Gerlyn Variety Store placed {procurement.po_number} with '
                    f'you for delivery to {procurement.branch.branch_name}.'
                ),
                'action_url': reverse('supplier_portal'),
                'action_label': 'View order',
            },
        },
    )


def notify_incoming_delivery(procurement, actor):
    return notify_event(
        EVENT_INCOMING_DELIVERY,
        f'Incoming delivery: {procurement.po_number}',
        f'{procurement.supplier} confirmed {procurement.po_number} for '
        f'delivery to {procurement.branch.branch_name} on '
        f'{procurement.delivery_date:%Y-%m-%d}.',
        branch=procurement.branch,
        actor=actor,
        action_url=reverse('delivery_list'),
        action_label='View Deliveries',
        reference_type='procurement',
        reference_id=procurement.pk,
    )
