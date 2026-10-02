"""Philippine Region -> Province -> City/Municipality -> Barangay lookups,
backing the cascading address dropdowns. Data is a static PSGC-derived
snapshot in accounts/data/ph_addresses/ (no external calls, no API key).
"""
import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / 'data' / 'ph_addresses'


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
