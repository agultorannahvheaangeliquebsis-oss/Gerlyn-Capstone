"""What a Delivery order is allowed to carry, decided in one place.

The storefront checkout and the customer sales API both call into here, so the
rule — a delivery needs a pinned location or a complete typed address, a
pickup needs neither — cannot be enforced in one and forgotten in the other.

Location is always optional. Nothing here can refuse an order *because* the
customer declined to share a location, or because the browser failed to give
one: a missing, malformed or unwanted pin is simply dropped, and the typed
address rules decide whether the order can go ahead.

Coordinates are kept only when the customer shared them for this order (or
pinned the saved address they chose to deliver to), and are rounded to four
decimal places, about 11 m: enough for a rider to find the door, less precise
than the raw GPS reading.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError

from accounts.geo import OTHER, Place, resolve_place
from accounts.geocoding import parse_coordinates
from accounts.validators import validate_ph_mobile_number
from core.limits import parse_text

PIN_PLACES = Decimal('0.0001')

MAX_STREET_LENGTH = 255
MAX_PLACE_LENGTH = 100
MAX_LANDMARK_LENGTH = 150
MAX_ADDRESS_LENGTH = 500

INCOMPLETE_ADDRESS = (
    'Delivery orders need a pinned location or a complete typed address '
    '(street, barangay and municipality). Add one, or choose pickup.'
)
NEEDS_CONTACT = 'Add a contact number so the rider can reach you.'

# The browser-side statuses the checkout form can post. Only GRANTED carries
# coordinates; the other two are the customer saying no (DECLINED) and the
# browser saying no (DENIED: permission refused, timed out or unsupported).
LOCATION_GRANTED = 'granted'
LOCATION_DECLINED = 'declined'
LOCATION_DENIED = 'denied'

_BARANGAY_PREFIX = re.compile(r'^(brgy|bgy|barangay)\b\.?\s*', re.IGNORECASE)


@dataclass(frozen=True)
class DeliveryDetails:
    address: str
    latitude: Decimal | None
    longitude: Decimal | None
    contact_number: str
    # Typed at checkout and ticked "also save to my addresses". The pin is
    # never saved to the account this way, only the typed address.
    save_typed_address: bool = False
    typed_street: str = ''
    typed_barangay: str = ''
    typed_city: str = ''
    # The province / municipality / barangay the delivery goes to, pinned to
    # the PSGC list where it could be. This is what a branch's serviceable
    # areas are checked against (branches.coverage); the address text is for
    # the rider.
    place: Place = Place()

    @property
    def has_pin(self):
        return self.latitude is not None and self.longitude is not None


def round_pin(value):
    return Decimal(value).quantize(PIN_PLACES, rounding=ROUND_HALF_UP)


def clean_pin(latitude, longitude):
    """A validated, rounded ``(lat, lon)`` pair, or ``(None, None)`` when blank.

    Raises ValueError for a malformed or out-of-range pair; the form path
    catches that and drops the pin, the API path reports it.
    """
    lat, lon = parse_coordinates(latitude, longitude)
    if lat is None:
        return None, None
    return round_pin(lat), round_pin(lon)


def contact_number_on_file(user):
    profile = getattr(user, 'customer_profile', None)
    return (profile.contact_number if profile else '') or ''


def _shared_pin(post):
    """The pin the customer shared at checkout, or None. Never raises."""
    if post.get('location_status') != LOCATION_GRANTED:
        return None
    try:
        lat, lon = clean_pin(post.get('delivery_latitude'), post.get('delivery_longitude'))
    except ValueError:
        return None
    return (lat, lon) if lat is not None else None


def _clean_contact(post, user):
    raw = (post.get('addr_contact') or '').strip() or contact_number_on_file(user).strip()
    if not raw:
        return '', NEEDS_CONTACT
    try:
        validate_ph_mobile_number(raw)
    except ValidationError as exc:
        return raw, exc.messages[0]
    return raw, None


def compose_typed_address(street, barangay, city, landmark='', province=''):
    parts = [street]
    if barangay:
        parts.append(barangay if _BARANGAY_PREFIX.match(barangay) else f'Brgy. {barangay}')
    parts.append(city)
    parts.append(province)
    text = ', '.join(part for part in parts if part)
    if landmark:
        text = f'{text} (Landmark: {landmark})' if text else f'Landmark: {landmark}'
    return text


def _place_input(post, name):
    """What the shopper gave for one place level: the dropdown's PSGC code, or
    the words typed under "Other" (``<name>_other``), or plain words from a
    client that has no dropdown."""
    raw = (post.get(name) or '').strip()
    if raw == OTHER:
        raw = (post.get(f'{name}_other') or '').strip()
    return raw


def _shown(raw, resolved_name):
    """The shopper's own words win; a PSGC code is shown as its name."""
    return resolved_name if raw.isdigit() and resolved_name else raw


def clean_delivery_form(post, user, saved_address):
    """Validate the Delivery part of the checkout form.

    Returns ``(DeliveryDetails | None, errors)``; details are None when there
    are errors. ``saved_address`` is the account's default Address or None.
    """
    errors = []
    contact, contact_error = _clean_contact(post, user)
    if contact_error:
        errors.append(contact_error)

    use_saved = saved_address is not None and post.get('address_source') != 'typed'
    shared = _shared_pin(post)

    if use_saved:
        address = (saved_address.text or '').strip()
        if not address:
            errors.append(
                'Your saved address is empty. Type an address instead, or fix it '
                'in Settings.'
            )
        pin = shared
        if (
            pin is None
            and post.get('location_status') != LOCATION_DECLINED
            and saved_address.latitude is not None
            and saved_address.longitude is not None
        ):
            pin = (round_pin(saved_address.latitude), round_pin(saved_address.longitude))
        # Saved addresses made with the address picker carry PSGC codes; an
        # older free-typed one has none and reads as an unrecognised place.
        details_extra = {'place': resolve_place(
            saved_address.province, saved_address.city, saved_address.barangay,
        )}
    else:
        street, e1 = parse_text(post.get('addr_street'), 'Street', max_length=MAX_STREET_LENGTH)
        barangay, e2 = parse_text(
            _place_input(post, 'addr_barangay'), 'Barangay', max_length=MAX_PLACE_LENGTH)
        city, e3 = parse_text(
            _place_input(post, 'addr_city'), 'Municipality', max_length=MAX_PLACE_LENGTH)
        province, e5 = parse_text(
            _place_input(post, 'addr_province'), 'Province', max_length=MAX_PLACE_LENGTH)
        landmark, e4 = parse_text(post.get('addr_landmark'), 'Landmark', max_length=MAX_LANDMARK_LENGTH)
        errors.extend(e for e in (e1, e2, e3, e4, e5) if e)
        pin = shared
        if not (street and barangay and city) and pin is None and not (e1 or e2 or e3 or e4 or e5):
            errors.append(INCOMPLETE_ADDRESS)
        place = resolve_place(province, city, barangay)
        address = compose_typed_address(
            street,
            _shown(barangay, place.barangay_name),
            _shown(city, place.city_name),
            landmark,
            province=_shown(province, place.province_name),
        )
        if not address and pin is not None:
            address = f'Pinned location ({pin[0]}, {pin[1]})'
        details_extra = {
            'save_typed_address': bool(post.get('addr_save')) and bool(street and barangay and city),
            'typed_street': street,
            'typed_barangay': _shown(barangay, place.barangay_name),
            'typed_city': _shown(city, place.city_name),
            'place': place,
        }

    if len(address) > MAX_ADDRESS_LENGTH:
        errors.append(f'Address cannot be longer than {MAX_ADDRESS_LENGTH} characters.')
    if errors:
        return None, errors
    return DeliveryDetails(
        address=address,
        latitude=pin[0] if pin else None,
        longitude=pin[1] if pin else None,
        contact_number=contact,
        **details_extra,
    ), []


def draft_from_post(post):
    """The typed (non-location) checkout fields, kept in the session when a
    submit is refused so the shopper does not retype them. No coordinates."""
    keys = ('address_source', 'addr_street', 'addr_province', 'addr_province_other',
            'addr_city', 'addr_city_other', 'addr_barangay', 'addr_barangay_other',
            'addr_landmark', 'addr_contact', 'addr_save')
    return {key: post.get(key, '') for key in keys if post.get(key)}
