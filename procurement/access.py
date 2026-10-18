"""Shared record-level access policy for purchase orders and deliveries.

Module grants decide which section a user may open. This policy is the
second, record-level layer used by both server-rendered views and the API:
branch scope, supplier ownership, and the workflow stages visible to Staff.
"""

from core.permissions import OWNER, BRANCH_MANAGER, STAFF, SUPPLIER
from core.scoping import scope_queryset

from .models import Procurement


# Every status a Staff user may see at all (record-visibility scope, used by
# procurements_visible_to). Includes RECEIVED so a completed delivery stays
# visible after the fact.
STAFF_VISIBLE_STATUSES = frozenset({
    Procurement.Status.APPROVED,
    *Procurement.NEGOTIATION_STATUSES,
    Procurement.Status.ORDERED,
    Procurement.Status.INCOMING,
    Procurement.Status.PARTIALLY_RECEIVED,
    Procurement.Status.RECEIVED,
})

# Subset of the above still awaiting delivery action (excludes RECEIVED) —
# used to filter the Staff deliveries queue down to outstanding deliveries.
STAFF_PENDING_DELIVERY_STATUSES = frozenset({Procurement.Status.APPROVED}) \
    | Procurement.NEGOTIATION_STATUSES | Procurement.AWAITING_DELIVERY_STATUSES


def procurements_visible_to(user, queryset=None):
    """Return only procurement records this user's role may read."""
    qs = queryset if queryset is not None else Procurement.objects.all()

    if user.role == OWNER:
        return qs
    if user.role == BRANCH_MANAGER:
        return scope_queryset(qs, user)
    if user.role == STAFF:
        return scope_queryset(qs, user).filter(status__in=STAFF_VISIBLE_STATUSES)
    if user.role == SUPPLIER:
        return qs.filter(supplier=user)
    return qs.none()


def user_can_read_procurement(user, procurement):
    """Object-level counterpart to ``procurements_visible_to``."""
    return procurements_visible_to(
        user, Procurement.objects.filter(pk=procurement.pk)
    ).exists()
