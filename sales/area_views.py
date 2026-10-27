"""Order Console: override a delivery order's serviceable-area restriction."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from core.module_permissions import EDIT, SALES_POS, user_can_perform_module_action
from core.scoping import user_can_access_branch

from .area_override import OVERRIDE_ROLES, AreaOverrideError, override_area
from .models import SalesTransaction


@login_required(login_url='accounts:login')
@require_POST
def transaction_area_override(request, pk):
    """Owner / Branch Manager only; the reason is required (sales.area_override)."""
    txn = get_object_or_404(SalesTransaction, pk=pk)

    if request.user.role not in OVERRIDE_ROLES:
        raise PermissionDenied('Only the Owner or a Branch Manager can override the delivery area.')
    if not user_can_access_branch(request.user, txn.branch_id):
        messages.error(request, 'That order belongs to another branch.')
        return redirect('transaction_list')
    if not user_can_perform_module_action(request.user, SALES_POS, EDIT):
        raise PermissionDenied('Your account cannot change orders.')

    try:
        override_area(pk, request.user, request.POST.get('override_reason', ''))
    except AreaOverrideError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'Delivery area overridden on {txn.order_number}. The reason is on the '
            f'order and in the Audit Log.',
        )
    return redirect('transaction_detail', pk=pk)
