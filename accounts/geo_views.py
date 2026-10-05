"""Same-origin endpoints for local PSGC data and configured map services.

They remain available before login because registration uses the same picker.
All external requests happen in ``accounts.geocoding`` rather than the browser.
"""
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from . import geo
from .geocoding import (
    GeocodingError, GeocodingRateLimited, GeocodingUnavailable,
    address_queries, forward_geocode, map_tile, normalized_address, reverse_geocode,
)
from .rate_limit import get_client_ip, is_rate_limited


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


def _limited(request):
    return is_rate_limited(
        'geocoding', get_client_ip(request), settings.GEOCODING_CLIENT_MAX_REQUESTS,
        settings.GEOCODING_CLIENT_WINDOW_SECONDS,
    )


def _service_error(exc):
    status = 503 if isinstance(exc, GeocodingUnavailable) else 429 if isinstance(exc, GeocodingRateLimited) else 502
    payload = {'status': exc.code, 'message': str(exc)}
    if exc.retry_after:
        payload['retry_after'] = exc.retry_after
    return JsonResponse(payload, status=status)


@require_GET
def geo_search(request):
    if _limited(request):
        return JsonResponse({
            'status': 'rate_limited',
            'message': 'Too many location requests. Wait a moment and try again.',
        }, status=429)
    parts = dict(
        region=request.GET.get('region', ''),
        province=request.GET.get('province', ''),
        city=request.GET.get('city', ''),
        barangay=request.GET.get('barangay', ''),
        street_detail=request.GET.get('street_detail', '')[:255],
    )
    address = normalized_address(**parts)
    if not address:
        return JsonResponse({'status': 'invalid', 'message': 'Select an address before locating it.'}, status=400)
    try:
        result = forward_geocode(address, fallbacks=address_queries(**parts)[1:])
    except GeocodingError as exc:
        return _service_error(exc)
    if not result:
        return JsonResponse({
            'status': 'no_result',
            'message': (
                'We could not find that address on the map. Check the spelling, '
                'or click the map to place the pin yourself.'
            ),
        })
    payload = {'status': 'ok', 'address': address, 'result': result.as_dict()}
    if result.approximate:
        payload['message'] = (
            'We found the area but not the exact street. Drag the pin to your '
            'exact location.'
        )
    return JsonResponse(payload)


@require_GET
def geo_reverse(request):
    if _limited(request):
        return JsonResponse({
            'status': 'rate_limited',
            'message': 'Too many location requests. Coordinates were kept; try again shortly.',
        }, status=429)
    try:
        label = reverse_geocode(request.GET.get('lat'), request.GET.get('lon'))
    except ValueError as exc:
        return JsonResponse({'status': 'invalid', 'message': str(exc)}, status=400)
    except GeocodingError as exc:
        return _service_error(exc)
    if not label:
        return JsonResponse({'status': 'no_result', 'message': 'Coordinates updated; no street address was found.'})
    return JsonResponse({'status': 'ok', 'label': label})


@require_GET
def geo_tile(request, zoom, x, y):
    if not (0 <= zoom <= 19 and 0 <= x < 2 ** zoom and 0 <= y < 2 ** zoom):
        return HttpResponse(status=404)
    try:
        payload = map_tile(zoom, x, y)
    except GeocodingError:
        return HttpResponse(status=204)
    response = HttpResponse(payload, content_type='image/png')
    response['Cache-Control'] = f'public, max-age={settings.MAP_TILE_CACHE_SECONDS}'
    return response
