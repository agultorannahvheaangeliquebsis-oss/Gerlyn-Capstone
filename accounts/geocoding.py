"""Server-side geocoding and map-tile clients.

Provider URLs and credentials stay in environment-backed settings. The JSON
parsing is Nominatim-compatible, while compatible self-hosted services can be
substituted entirely through configuration.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache

from . import geo


class GeocodingError(Exception):
    code = 'service_error'

    def __init__(self, message='The location service is temporarily unavailable.', *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class GeocodingUnavailable(GeocodingError):
    code = 'unavailable'


class GeocodingRateLimited(GeocodingError):
    code = 'rate_limited'


@dataclass(frozen=True)
class GeocodingResult:
    latitude: Decimal
    longitude: Decimal
    label: str

    def as_dict(self):
        return {
            'latitude': f'{self.latitude:.6f}',
            'longitude': f'{self.longitude:.6f}',
            'label': self.label,
        }


def normalized_address(*, region='', province='', city='', barangay='', street_detail=''):
    """Resolve PSGC codes locally and produce a stable provider query."""
    parts = []
    street = ' '.join((street_detail or '').split())
    if street:
        parts.append(street)
    for name in (
        geo.barangay_name(barangay), geo.city_name(city),
        geo.province_name(province), geo.region_name(region),
    ):
        clean = ' '.join((name or '').split())
        if clean and clean.casefold() not in {part.casefold() for part in parts}:
            parts.append(clean)
    if parts and 'philippines' not in {part.casefold() for part in parts}:
        parts.append('Philippines')
    return ', '.join(parts)


def parse_coordinates(latitude, longitude):
    """Return a validated coordinate pair, or ``(None, None)`` when blank."""
    lat_raw = str(latitude or '').strip()
    lon_raw = str(longitude or '').strip()
    if not lat_raw and not lon_raw:
        return None, None
    if not lat_raw or not lon_raw:
        raise ValueError('Latitude and longitude must be provided together.')
    try:
        lat = Decimal(lat_raw)
        lon = Decimal(lon_raw)
    except InvalidOperation as exc:
        raise ValueError('Enter valid numeric coordinates.') from exc
    if not lat.is_finite() or not lon.is_finite():
        raise ValueError('Enter finite numeric coordinates.')
    if not Decimal('-90') <= lat <= Decimal('90'):
        raise ValueError('Latitude must be between -90 and 90.')
    if not Decimal('-180') <= lon <= Decimal('180'):
        raise ValueError('Longitude must be between -180 and 180.')
    return lat.quantize(Decimal('0.000001')), lon.quantize(Decimal('0.000001'))


def _cache_key(prefix, value):
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()
    return f'geocoding:{prefix}:{digest}'


def _cache_get(key):
    try:
        return cache.get(key)
    except Exception:
        return None


def _cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout)
    except Exception:
        pass


def _reserve_provider_slot():
    """Apply the provider-wide minimum interval after cache misses."""
    interval = settings.GEOCODING_PROVIDER_MIN_INTERVAL_SECONDS
    if interval <= 0:
        return
    try:
        allowed = cache.add('geocoding:provider-request-slot', 1, interval)
    except Exception:
        return
    if not allowed:
        raise GeocodingRateLimited(
            'Automatic location lookup is busy. Try again in a moment.',
            retry_after=str(interval),
        )


def _provider_get(url, params, *, binary=False, include_geocoding_key=True):
    if not url:
        raise GeocodingUnavailable('Automatic location lookup is not configured.')
    params = dict(params)
    if include_geocoding_key and settings.GEOCODING_API_KEY:
        params[settings.GEOCODING_API_KEY_PARAM] = settings.GEOCODING_API_KEY
    separator = '&' if '?' in url else '?'
    request_url = f'{url}{separator}{urllib.parse.urlencode(params)}' if params else url
    request = urllib.request.Request(request_url, headers={
        'User-Agent': settings.GEOCODING_USER_AGENT,
        'Accept': '*/*' if binary else 'application/json',
    })
    try:
        with urllib.request.urlopen(request, timeout=settings.GEOCODING_TIMEOUT) as response:
            payload = response.read()
            return payload if binary else json.loads(payload.decode('utf-8'))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise GeocodingRateLimited(
                'The location service rate limit was reached. Try again shortly.',
                retry_after=exc.headers.get('Retry-After'),
            ) from exc
        raise GeocodingError() from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
        raise GeocodingError() from exc


def forward_geocode(address):
    normalized = ' '.join((address or '').split())
    if not normalized:
        return None
    key = _cache_key('forward', normalized.casefold())
    cached = _cache_get(key)
    if cached is not None:
        return GeocodingResult(
            Decimal(cached['latitude']), Decimal(cached['longitude']), cached['label']
        ) if cached else None

    _reserve_provider_slot()
    data = _provider_get(settings.GEOCODING_SEARCH_URL, {
        'q': normalized, 'format': 'jsonv2', 'addressdetails': 1,
        'countrycodes': settings.GEOCODING_COUNTRY_CODE, 'limit': 1,
    })
    item = data[0] if isinstance(data, list) and data else None
    if not item:
        _cache_set(key, {}, settings.GEOCODING_NEGATIVE_CACHE_SECONDS)
        return None
    try:
        lat, lon = parse_coordinates(item.get('lat'), item.get('lon'))
    except (AttributeError, ValueError) as exc:
        raise GeocodingError() from exc
    result = GeocodingResult(lat, lon, str(item.get('display_name') or normalized))
    _cache_set(key, result.as_dict(), settings.GEOCODING_CACHE_SECONDS)
    return result


def reverse_geocode(latitude, longitude):
    lat, lon = parse_coordinates(latitude, longitude)
    value = f'{lat:.6f},{lon:.6f}'
    key = _cache_key('reverse', value)
    cached = _cache_get(key)
    if cached is not None:
        return cached or None
    _reserve_provider_slot()
    data = _provider_get(settings.GEOCODING_REVERSE_URL, {
        'lat': str(lat), 'lon': str(lon), 'format': 'jsonv2', 'addressdetails': 1,
    })
    label = str(data.get('display_name') or '').strip() if isinstance(data, dict) else ''
    _cache_set(
        key, label,
        settings.GEOCODING_CACHE_SECONDS if label else settings.GEOCODING_NEGATIVE_CACHE_SECONDS,
    )
    return label or None


def map_tile(zoom, x, y):
    """Fetch a configured raster tile without exposing provider credentials."""
    template = settings.MAP_TILE_URL_TEMPLATE
    if not template:
        raise GeocodingUnavailable('Map tiles are not configured.')
    try:
        url = template.format(
            z=zoom, x=x, y=y,
            api_key=urllib.parse.quote(settings.MAP_TILE_API_KEY, safe=''),
        )
    except (KeyError, ValueError) as exc:
        raise GeocodingError('The map tile URL template is invalid.') from exc
    key = _cache_key('tile', url)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    payload = _provider_get(url, {}, binary=True, include_geocoding_key=False)
    _cache_set(key, payload, settings.MAP_TILE_CACHE_SECONDS)
    return payload
