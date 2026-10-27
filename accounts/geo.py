"""Philippine Region -> Province -> City/Municipality -> Barangay lookups,
backing the cascading address dropdowns. Data is a static PSGC-derived
snapshot in accounts/data/ph_addresses/ (no external calls, no API key).
"""
import json
import re
import unicodedata
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Resolving a shopper's place to PSGC codes (delivery coverage).
#
# The checkout form sends PSGC codes when the shopper used the dropdowns, and
# words when they picked "Other" or when an older client / the API sent a typed
# name. Both end up as the same three codes here, so a service area can be
# matched by code and never by a spelling.
# ---------------------------------------------------------------------------

# The value the checkout dropdowns post for "Other"; the typed words travel in
# a sibling field named <field>_other.
OTHER = '__other__'

# Common spellings that differ from the PSGC name. Only entries that are known
# to be used for a place served by the store belong here.
_PLACE_ALIASES = {'baliwag': 'baliuag'}


def place_key(text):
    """Comparison key for a place name: case, accents, punctuation, the
    "City of" / "Province" dressing and a "(Capital)" or "(Pob.)" tail all
    ignored. Used to match words a shopper typed against the PSGC names."""
    value = unicodedata.normalize('NFKD', str(text or ''))
    value = ''.join(ch for ch in value if not unicodedata.combining(ch)).casefold()
    value = re.sub(r'\(.*?\)', ' ', value)
    value = re.sub(r'\bsta\b\.?', 'santa', value)
    value = re.sub(r'\bsto\b\.?', 'santo', value)
    value = re.sub(r'[^a-z0-9]+', ' ', value).strip()
    value = _BARANGAY_PREFIX.sub('', value).strip()
    for prefix in ('city of ', 'municipality of ', 'province of '):
        if value.startswith(prefix):
            value = value[len(prefix):]
    for suffix in (' city', ' municipality', ' province'):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
    return _PLACE_ALIASES.get(value, value)


@lru_cache(maxsize=1)
def _city_to_province():
    return {c['city_code']: c['province_code'] for c in _load('city')}


@lru_cache(maxsize=1)
def _cities_by_province():
    out = {}
    for c in _load('city'):
        out.setdefault(c['province_code'], []).append(c)
    return out


@lru_cache(maxsize=1)
def _barangays_by_city():
    out = {}
    for b in _load('barangay'):
        out.setdefault(b['city_code'], []).append(b)
    return out


def all_provinces():
    """Every province in the snapshot, by name, for a province dropdown."""
    return sorted(
        ({'code': p['province_code'], 'name': p['province_name']} for p in _load('province')),
        key=lambda p: p['name'],
    )


def province_of_city(city_code):
    return _city_to_province().get(city_code, '')


def region_of_province(province_code):
    for p in _load('province'):
        if p['province_code'] == province_code:
            return p['region_code']
    return ''


@dataclass(frozen=True)
class Place:
    """Where a delivery goes, as far as it could be pinned to the PSGC list.

    A level that matched carries its code; one that did not carries the words
    the shopper gave in ``*_text`` so the message and the order can still name
    it. ``city`` is the municipality or city.
    """
    province_code: str = ''
    city_code: str = ''
    barangay_code: str = ''
    province_text: str = ''
    city_text: str = ''
    barangay_text: str = ''

    @property
    def province_name(self):
        return province_name(self.province_code) or self.province_text

    @property
    def city_name(self):
        return city_name(self.city_code) or self.city_text

    @property
    def barangay_name(self):
        return barangay_name(self.barangay_code) or self.barangay_text

    @property
    def is_empty(self):
        return not (self.province_name or self.city_name or self.barangay_name)

    def label(self):
        """"Brgy. Poblacion, Baliuag, Bulacan" - whatever levels are known."""
        parts = []
        if self.barangay_name:
            name = self.barangay_name
            parts.append(name if _BARANGAY_PREFIX.match(name) else f'Brgy. {name}')
        parts.extend(p for p in (self.city_name, self.province_name) if p)
        return ', '.join(parts)


def _clean_words(value):
    return ' '.join(str(value or '').split())


def _province_code_for(value):
    value = _clean_words(value)
    if not value or value == OTHER:
        return ''
    if value in _province_index():
        return value
    key = place_key(value)
    hits = [p['province_code'] for p in _load('province') if place_key(p['province_name']) == key]
    return hits[0] if len(hits) == 1 else ''


def _city_code_for(value, province_code):
    value = _clean_words(value)
    if not value or value == OTHER:
        return ''
    if value in _city_index():
        # A code from a stale or tampered form that belongs to another
        # province is not this shopper's municipality.
        if province_code and province_of_city(value) != province_code:
            return ''
        return value
    key = place_key(value)
    pool = _cities_by_province().get(province_code, []) if province_code else _load('city')
    hits = [c['city_code'] for c in pool if place_key(c['city_name']) == key]
    return hits[0] if len(hits) == 1 else ''


def _barangay_code_for(value, city_code):
    value = _clean_words(value)
    if not value or value == OTHER or not city_code:
        return ''
    if value in _barangay_index():
        belongs = any(b['brgy_code'] == value for b in _barangays_by_city().get(city_code, []))
        return value if belongs else ''
    key = place_key(value)
    hits = [
        b['brgy_code'] for b in _barangays_by_city().get(city_code, [])
        if place_key(b['brgy_name']) == key
    ]
    return hits[0] if len(hits) == 1 else ''


_LANDMARK_TAIL = re.compile(r'\s*\(\s*landmark\s*:.*\)\s*$', re.IGNORECASE | re.DOTALL)


@lru_cache(maxsize=1)
def _region_keys():
    return {place_key(r['region_name']) for r in _load('region')}


def place_from_text(text):
    """Read the place out of a saved address's text, strictly by position.

    The app's own composers write ``street, barangay, municipality, province``
    (then optionally a region and "Philippines"), so the text is read from the
    end: the last segment must be a province, the one before it a municipality
    inside *that* province, and the one before that a barangay inside that
    municipality. A level is filled only when its segment matches exactly one
    PSGC name; otherwise it stays blank. Position matters because the names
    repeat (the municipality of Bulacan is in the province of Bulacan), so a
    match anywhere in the text would be a guess.

    Returns a ``Place`` carrying codes only; never raises.
    """
    text = _LANDMARK_TAIL.sub('', str(text or ''))
    segments = [s for s in (' '.join(part.split()) for part in text.split(',')) if s]
    ignorable = {'philippines'} | _region_keys()
    while segments and (segments[-1].isdigit() or place_key(segments[-1]) in ignorable):
        segments.pop()
    if not segments:
        return Place()
    province_code = _province_code_for(segments.pop())
    if not province_code or not segments:
        return Place(province_code=province_code)
    city_code = _city_code_for(segments.pop(), province_code)
    if not city_code or not segments:
        return Place(province_code=province_code, city_code=city_code)
    return Place(
        province_code=province_code,
        city_code=city_code,
        barangay_code=_barangay_code_for(segments.pop(), city_code),
    )


def resolve_place(province='', city='', barangay=''):
    """Pin a province / municipality / barangay to PSGC codes.

    Each argument may be a PSGC code or words. A municipality is looked up
    inside the province when there is one, else by name across the country
    (only when exactly one municipality has that name); a barangay only inside
    its municipality. The province is filled in from the municipality when the
    shopper did not give one. Never raises: what cannot be matched stays as
    text on the returned Place.
    """
    province_code = _province_code_for(province)
    city_code = _city_code_for(city, province_code)
    if city_code and not province_code:
        province_code = province_of_city(city_code)
    barangay_code = _barangay_code_for(barangay, city_code)

    def leftover(raw, code):
        raw = _clean_words(raw)
        return '' if code or raw == OTHER else raw

    return Place(
        province_code=province_code,
        city_code=city_code,
        barangay_code=barangay_code,
        province_text=leftover(province, province_code),
        city_text=leftover(city, city_code),
        barangay_text=leftover(barangay, barangay_code),
    )
