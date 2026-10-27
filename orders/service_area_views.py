"""Storefront views for serviceable areas.

* ``service_areas``   - the public "where we deliver" page, grouped by branch.
* ``service_banner_dismiss`` - the strip's "x" (remembered for the session).
* ``area_check``      - the checkout page's live check of the address it holds.
  Advisory only: ``checkout()`` and the sales API run the same rule again on
  submit, and are what actually stops an order.
* ``area_switch_to_pickup`` - records that the shopper stepped away from a
  delivery address the branch does not serve, so placing the order as Pickup
  can be confirmed to them in the app.
"""
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from branches import coverage as cov
from branches.models import Branch
from core.context_processors import SERVICE_BANNER_DISMISSED_KEY

from . import service_areas
from .customer_views import _cart_items, _safe_get_next, _safe_next, customer_required


@require_GET
def service_areas_page(request):
    return render(request, 'orders/service_areas.html', {
        'listing': cov.public_listing(),
        'back_url': _safe_get_next(request) or '',
    })


@require_POST
def service_banner_dismiss(request):
    request.session[SERVICE_BANNER_DISMISSED_KEY] = True
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'ok': True})
    return redirect(_safe_next(request) or 'orders:catalog')


def _session_branch(request):
    return Branch.objects.filter(pk=request.session.get('branch_id'), is_active=True).first()


@customer_required
@require_GET
def area_check(request):
    branch = _session_branch(request)
    if branch is None:
        return JsonResponse({'ok': False, 'status': 'no_branch', 'covered': False,
                             'message': 'Pick a branch first.'}, status=409)
    saved = request.user.addresses.filter(is_default=True).first()
    place = service_areas.place_from_form(request.GET, saved)
    _, total, _ = _cart_items(request)
    coverage = cov.check_delivery(branch, place, order_total=total)
    return JsonResponse(service_areas.check_payload(
        branch, coverage, place, order_total=total,
        saved=service_areas.uses_saved_address(request.GET, saved),
    ))


@customer_required
@require_POST
def area_switch_to_pickup(request):
    branch = _session_branch(request)
    if branch is None:
        return JsonResponse({'ok': False, 'message': 'Pick a branch first.'}, status=409)
    saved = request.user.addresses.filter(is_default=True).first()
    place = service_areas.place_from_form(request.POST, saved)
    _, total, _ = _cart_items(request)
    coverage = cov.check_delivery(branch, place, order_total=total)
    # Only a delivery the rule really refuses can be "switched": otherwise this
    # endpoint would let anyone plant a switch notice on an ordinary pickup.
    if coverage.ok:
        return JsonResponse({'ok': False, 'recorded': False})
    service_areas.remember_area_block(request.session, branch, coverage)
    return JsonResponse({'ok': True, 'recorded': True})
