"""Philippine Region -> Province -> City/Municipality -> Barangay lookups,
backing the cascading address dropdowns. Data is a static PSGC-derived
snapshot in accounts/data/ph_addresses/ (no external calls, no API key).
"""
import json
import re
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / 'data' / 'ph_addresses'

_BARANGAY_PREFIX = re.compile(r'^(brgy|bgy|barangay)\b\.?\s*', re.IGNORECASE)


@lru_cache(maxsize=1)
def _load(name):
    with open(DATA_DIR / f'{name}.json', encoding='utf-8') as f:
        return json.load(f)


def regions():
    # Sorted by region_code, not name: an alphabetical name sort puts
    # "Region IX" ("I" < "V") before Region V-VIII and X, out of the order
    # shoppers expect from the Roman-numeral labels.
    return sorted(
        ({'code': r['region_code'], 'name': r['region_name']} for r in _load('region')),
        key=lambda r: r['code']
    )


def provinces(region_code):
    return sorted(
        ({'code': p['province_code'], 'name': p['province_name']}
         for p in _load('province') if p['region_code'] == region_code),
        key=lambda p: p['name']
    )


def cities(province_code):
    return sorted(
        ({'code': c['city_code'], 'name': c['city_name']}
         for c in _load('city') if c['province_code'] == province_code),
        key=lambda c: c['name']
    )


def barangays(city_code):
    return sorted(
        ({'code': b['brgy_code'], 'name': b['brgy_name']}
         for b in _load('barangay') if b['city_code'] == city_code),
        key=lambda b: b['name']
    )


@lru_cache(maxsize=1)
def _region_index():
    return {r['region_code']: r['region_name'] for r in _load('region')}


@lru_cache(maxsize=1)
def _province_index():
    return {p['province_code']: p['province_name'] for p in _load('province')}


@lru_cache(maxsize=1)
def _city_index():
    return {c['city_code']: c['city_name'] for c in _load('city')}


@lru_cache(maxsize=1)
def _barangay_index():
    return {b['brgy_code']: b['brgy_name'] for b in _load('barangay')}


def region_name(code):
    return _region_index().get(code, '')


def province_name(code):
    return _province_index().get(code, '')


def city_name(code):
    return _city_index().get(code, '')


def barangay_name(code):
    return _barangay_index().get(code, '')


def compose_address(region='', province='', city='', barangay='', street_detail=''):
    """Human-readable address assembled from the bundled PSGC snapshot."""
    from .geocoding import normalized_address

    return normalized_address(
        region=region, province=province, city=city,
        barangay=barangay, street_detail=street_detail,
    )


def _plain_place(text):
    """Lower-cased place name without the "City of" / "Municipality of" dressing."""
    value = ' '.join((text or '').casefold().split())
    for prefix in ('city of ', 'municipality of '):
        if value.startswith(prefix):
            value = value[len(prefix):]
    for suffix in (' city', ' municipality'):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
    return value


def match_places(city_text, barangay_text=''):
    """Best-effort PSGC codes for a municipality and barangay typed as words.

    Returns ``{}`` unless exactly one municipality in the snapshot has that
    name (a handful repeat across provinces, and a guess there would file the
    address under the wrong province). The barangay code is added only when it
    matches inside that municipality. Used when a typed checkout address is
    saved to the account, so the saved row has the same structured fields the
    address picker would have given it.
    """
    wanted = _plain_place(city_text)
    if not wanted:
        return {}
    found = [c for c in _load('city') if _plain_place(c['city_name']) == wanted]
    if len(found) != 1:
        return {}
    city = found[0]
    province = next(
        (p for p in _load('province') if p['province_code'] == city['province_code']), None
    )
    codes = {
        'region': province['region_code'] if province else '',
        'province': city['province_code'],
        'city': city['city_code'],
    }
    barangay = _plain_place(_BARANGAY_PREFIX.sub('', barangay_text or ''))
    if barangay:
        hits = [
            b for b in _load('barangay')
            if b['city_code'] == city['city_code'] and _plain_place(b['brgy_name']) == barangay
        ]
        if len(hits) == 1:
            codes['barangay'] = hits[0]['brgy_code']
    return codes
