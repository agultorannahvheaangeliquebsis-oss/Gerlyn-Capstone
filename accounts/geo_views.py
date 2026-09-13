"""Endpoints backing the address picker's cascading dropdowns:
geo_regions/provinces/cities/barangays are local PSGC lookups
(accounts/geo.py) — a bundled static dataset, no external calls, no API key.

Deliberately NOT behind @login_required — the registration form's address
step uses the same picker before the user has an account, so these can't be
gated on auth.
"""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from . import geo


@require_GET
def geo_regions(request):
    return JsonResponse({'results': geo.regions()})


@require_GET
def geo_provinces(request):
    region = request.GET.get('region', '')
    return JsonResponse({'results': geo.provinces(region) if region else []})


@require_GET
def geo_cities(request):
    province = request.GET.get('province', '')
    return JsonResponse({'results': geo.cities(province) if province else []})


@require_GET
def geo_barangays(request):
    city = request.GET.get('city', '')
    return JsonResponse({'results': geo.barangays(city) if city else []})
