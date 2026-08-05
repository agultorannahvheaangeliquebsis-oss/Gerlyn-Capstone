"""Endpoints backing the shared Leaflet address picker:
  - geo_regions/provinces/cities/barangays: local PSGC lookups (accounts/geo.py)
  - geo_search/geo_reverse: thin proxies to Nominatim (OpenStreetMap's free
    geocoder), so the browser never needs to talk to a third party directly
    and we can set the identifying User-Agent Nominatim's usage policy requires.

Deliberately NOT behind @login_required — the registration form's address
step uses the same picker before the user has an account, so these can't be
gated on auth. Client-side debouncing (in the picker JS) keeps request
volume low and polite toward Nominatim's rate limits.
"""
import json
import urllib.parse
import urllib.request

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from . import geo

NOMINATIM_BASE = 'https://nominatim.openstreetmap.org'
NOMINATIM_USER_AGENT = 'GerlynVarietyStore/1.0 (Baliuag, Bulacan; storefront address picker)'
NOMINATIM_TIMEOUT = 6


def _nominatim_get(path, params):
    url = f'{NOMINATIM_BASE}{path}?{urllib.parse.urlencode(params)}'
    req = urllib.request.Request(url, headers={'User-Agent': NOMINATIM_USER_AGENT})
    with urllib.request.urlopen(req, timeout=NOMINATIM_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


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


@require_GET
def geo_search(request):
    """Forward-geocodes a street/building query, scoped to a locality string
    built from the user's Region/Province/City/Barangay selection."""
    q = request.GET.get('q', '').strip()
    locality = request.GET.get('locality', '').strip()
    if not q:
        return JsonResponse({'results': []})

    query = f'{q}, {locality}, Philippines' if locality else f'{q}, Philippines'
    try:
        data = _nominatim_get('/search', {
            'q': query,
            'format': 'jsonv2',
            'addressdetails': 1,
            'countrycodes': 'ph',
            'limit': 6,
        })
    except Exception:
        return JsonResponse({'results': []})

    results = [
        {'label': d.get('display_name', ''), 'lat': d.get('lat'), 'lon': d.get('lon')}
        for d in data
    ]
    return JsonResponse({'results': results})


@require_GET
def geo_reverse(request):
    """Reverse-geocodes a dragged/tapped pin back into a readable address,
    so the text field stays in sync with wherever the pin actually is."""
    lat = request.GET.get('lat')
    lon = request.GET.get('lon')
    if not lat or not lon:
        return JsonResponse({'label': ''})

    try:
        data = _nominatim_get('/reverse', {
            'lat': lat,
            'lon': lon,
            'format': 'jsonv2',
            'addressdetails': 1,
        })
    except Exception:
        return JsonResponse({'label': ''})

    return JsonResponse({'label': data.get('display_name', '')})
