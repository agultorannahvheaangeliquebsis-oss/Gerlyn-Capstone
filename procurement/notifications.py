"""Notification events emitted by procurement state changes."""
from django.urls import reverse

from core.notifications import (
    EVENT_INCOMING_DELIVERY,
    EVENT_PO_APPROVED,
    EVENT_PO_TERMS_FOR_BUYER,
    EVENT_PO_TERMS_FOR_SUPPLIER,
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
                    f'GERLYN placed {procurement.po_number} with '
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


def notify_terms_for_buyer(procurement, actor, *, countered):
    """The supplier submitted an offer (first proposal or a counter) —
    the buyer's (Owner/Manager) turn to respond."""
    verb = 'countered on' if countered else 'proposed terms for'
    return notify_event(
        EVENT_PO_TERMS_FOR_BUYER,
        f'{procurement.po_number} needs your review',
        f'{procurement.supplier} {verb} {procurement.po_number} '
        f'({procurement.branch.branch_name}) — delivery '
        f'{procurement.proposed_delivery_date:%Y-%m-%d}.',
        branch=procurement.branch,
        actor=actor,
        action_url=reverse('procurement_detail', kwargs={'pk': procurement.pk}),
        action_label='Review terms',
        reference_type='procurement',
        reference_id=procurement.pk,
    )


def notify_terms_for_supplier(procurement, actor, *, agreed):
    """The buyer (Owner/Manager) countered, or accepted outright — the
    supplier's turn (respond to the counter, or give the final confirmation
    once terms are agreed)."""
    title = (
        f'{procurement.po_number} — terms agreed, please confirm' if agreed
        else f'{procurement.po_number} — the buyer proposed different terms'
    )
    message = (
        f'{procurement.branch.branch_name} accepted your terms on '
        f'{procurement.po_number} — confirm to start preparing it.' if agreed
        else f'{procurement.branch.branch_name} countered on '
             f'{procurement.po_number} — delivery '
             f'{procurement.proposed_delivery_date:%Y-%m-%d}.'
    )
    return notify_event(
        EVENT_PO_TERMS_FOR_SUPPLIER,
        title, message,
        branch=procurement.branch,
        actor=actor,
        related_users={'supplier': procurement.supplier},
        action_url=reverse('supplier_portal'),
        action_label='View order',
        reference_type='procurement',
        reference_id=procurement.pk,
    )
