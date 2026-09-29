"""Automatic, whole-cart branch assignment.

Distances are deliberately approximate straight-line distances. Decimal is
used for stored coordinates and validation, then values are converted to
``float`` for the trigonometric Haversine calculation because Python's
``math`` functions operate on floats. The result is display/ranking data, not
money or a routed delivery quote, so double-precision is appropriate.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import asin, cos, radians, sin, sqrt
import time

from django.conf import settings
from django.db.models import Sum

from branches.models import Branch
from inventory.models import Inventory
from sales.models import SalesItem, SalesTransaction


EARTH_RADIUS_KM = 6371.0088
FULFILLMENT_LOCATION_TTL_SECONDS = 30 * 60
OPEN_ORDER_STATUSES = (
    SalesTransaction.OrderStatus.PENDING,
    SalesTransaction.OrderStatus.APPROVED,
    SalesTransaction.OrderStatus.PREPARING,
)


@dataclass(frozen=True)
class FulfillmentResolution:
    branch: Branch | None
    distance_km: float | None
    strategy: str
    unavailable_product_ids: tuple[int, ...] = ()

    @property
    def can_fulfill(self):
        return self.branch is not None


def _coordinate(value, minimum, maximum):
    if value in (None, ''):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < minimum or parsed > maximum:
        return None
    return parsed


def valid_coordinate_pair(latitude, longitude):
    """Return a Decimal pair or ``None`` for incomplete/invalid coordinates."""
    lat = _coordinate(latitude, Decimal('-90'), Decimal('90'))
    lon = _coordinate(longitude, Decimal('-180'), Decimal('180'))
    return (lat, lon) if lat is not None and lon is not None else None


def customer_fulfillment_location(user, session):
    """Return private location inputs for resolution and expire browser data.

    The human-readable label and browser accuracy are display metadata only.
    They expire with the temporary coordinates and never influence branch
    ranking.
    """
    location = session.get('fulfillment_location') or {}
    try:
        captured_at = float(location.get('captured_at', 0))
    except (TypeError, ValueError):
        captured_at = 0
    temporary = valid_coordinate_pair(
        location.get('latitude'), location.get('longitude')
    )
    if not temporary or time.time() - captured_at > FULFILLMENT_LOCATION_TTL_SECONDS:
        session.pop('fulfillment_location', None)
        temporary = None

    saved = user.addresses.filter(is_default=True).first() if user.is_authenticated else None
    coordinates = temporary or (
        valid_coordinate_pair(saved.latitude, saved.longitude) if saved else None
    )
    uses_saved_address = bool(
        not temporary and saved and (coordinates or saved.city or saved.barangay)
    )
    return {
        'latitude': coordinates[0] if coordinates else None,
        'longitude': coordinates[1] if coordinates else None,
        'city': saved.city if saved else '',
        'barangay': saved.barangay if saved else '',
        'source': 'browser' if temporary else ('address' if uses_saved_address else 'fallback'),
        'label': (
            str(location.get('label') or '').strip()
            if temporary else (saved.text if uses_saved_address else '')
        ),
        'accuracy_m': location.get('accuracy_m') if temporary else None,
        'captured_at': captured_at if temporary else None,
    }


def remember_assignment(session, resolution):
    """Persist no customer coordinates—only branch and rounded display data."""
    session['branch_id'] = resolution.branch.pk
    session['fulfillment_assignment'] = {
        'branch_id': resolution.branch.pk,
        'distance_km': (
            round(resolution.distance_km, 1)
            if resolution.distance_km is not None else None
        ),
        'strategy': resolution.strategy,
    }


def haversine_distance_km(latitude, longitude, branch_latitude, branch_longitude):
    """Return great-circle distance in kilometres for two validated points."""
    origin = valid_coordinate_pair(latitude, longitude)
    destination = valid_coordinate_pair(branch_latitude, branch_longitude)
    if origin is None or destination is None:
        raise ValueError('Both locations require valid latitude and longitude.')

    lat1, lon1 = map(float, origin)
    lat2, lon2 = map(float, destination)
    delta_lat = radians(lat2 - lat1)
    delta_lon = radians(lon2 - lon1)
    a = (
        sin(delta_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(delta_lon / 2) ** 2
    )
    # Floating-point rounding can put an antipodal result a hair outside the
    # mathematical [0, 1] interval accepted by sqrt/asin.
    a = max(0.0, min(1.0, a))
    return EARTH_RADIUS_KM * 2 * asin(sqrt(a))


def normalize_cart_lines(cart_lines):
    """Collapse supported cart-line shapes into ``{product_id: quantity}``."""
    requirements = {}
    iterable = cart_lines.items() if hasattr(cart_lines, 'items') else cart_lines
    for line in iterable:
        if isinstance(line, tuple) and len(line) >= 2:
            product, quantity = line[0], line[1]
        else:
            product = getattr(line, 'product_id', None) or getattr(line, 'product', None)
            quantity = getattr(line, 'quantity', None) or getattr(line, 'quantity_sold', None)
        product_id = getattr(product, 'pk', product)
        try:
            product_id = int(product_id)
            quantity = int(quantity)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and quantity > 0:
            requirements[product_id] = requirements.get(product_id, 0) + quantity
    return requirements


def fulfillable_quantities(branch_ids, product_ids):
    """Available sellable units after open online-order commitments."""
    branch_ids = list(dict.fromkeys(int(value) for value in branch_ids))
    product_ids = list(dict.fromkeys(int(value) for value in product_ids))
    if not branch_ids or not product_ids:
        return {}

    physical = {
        (inventory.branch_id, inventory.product_id): inventory.available_qty
        for inventory in Inventory.objects.sellable().filter(
            branch_id__in=branch_ids, product_id__in=product_ids
        )
    }
    committed = {
        (row['transaction__branch_id'], row['product_id']): row['committed']
        for row in (
            SalesItem.objects.filter(
                transaction__branch_id__in=branch_ids,
                product_id__in=product_ids,
                transaction__transaction_type=SalesTransaction.TransactionType.ONLINE,
                transaction__order_status__in=OPEN_ORDER_STATUSES,
            )
            .values('transaction__branch_id', 'product_id')
            .annotate(committed=Sum('quantity_sold'))
        )
    }
    return {
        (branch_id, product_id): max(
            0,
            physical.get((branch_id, product_id), 0)
            - committed.get((branch_id, product_id), 0),
        )
        for branch_id in branch_ids
        for product_id in product_ids
    }


def _fallback_rank(branch, *, city, barangay, preferred_branch_id, default_branch_id):
    exact_barangay = bool(barangay and branch.barangay and branch.barangay == barangay)
    same_city = bool(city and branch.city and branch.city == city)
    return (
        0 if exact_barangay else 1,
        0 if same_city else 1,
        0 if branch.pk == preferred_branch_id else 1,
        0 if branch.pk == default_branch_id else 1,
        branch.pk,
    )


def resolve_nearest_branch(
    latitude,
    longitude,
    cart_lines,
    *,
    city='',
    barangay='',
    preferred_branch_id=None,
):
    """Choose one active branch that can fulfill every cart line.

    Located branches are ranked by Haversine distance with primary-key tie
    breaking. Branches without usable coordinates follow them and are ranked
    by service-area match, the existing automatic assignment, the configured
    default, then primary key. Without customer coordinates, that same
    deterministic fallback ordering applies to every active branch.

    No partial result is ever returned: if no candidate covers the complete
    requirement mapping, ``branch`` is ``None``.
    """
    requirements = normalize_cart_lines(cart_lines)
    branches = list(Branch.objects.filter(is_active=True).order_by('pk'))
    if not branches:
        return FulfillmentResolution(None, None, 'no_active_branches', tuple(requirements))

    origin = valid_coordinate_pair(latitude, longitude)
    default_branch_id = getattr(settings, 'DEFAULT_FULFILLMENT_BRANCH_ID', None)
    ranked = []
    for branch in branches:
        branch_coordinates = valid_coordinate_pair(branch.latitude, branch.longitude)
        if origin and branch_coordinates:
            distance = haversine_distance_km(*origin, *branch_coordinates)
            rank = (0, distance, branch.pk)
            strategy = 'coordinates'
        else:
            distance = None
            rank = (1,) + _fallback_rank(
                branch,
                city=city,
                barangay=barangay,
                preferred_branch_id=preferred_branch_id,
                default_branch_id=default_branch_id,
            )
            strategy = 'service_area' if city or barangay else 'default'
        ranked.append((rank, branch, distance, strategy))
    ranked.sort(key=lambda row: row[0])

    quantities = fulfillable_quantities(
        [branch.pk for branch in branches], requirements.keys()
    )
    unavailable = set()
    for _rank, branch, distance, strategy in ranked:
        missing = {
            product_id
            for product_id, needed in requirements.items()
            if quantities.get((branch.pk, product_id), 0) < needed
        }
        if not missing:
            return FulfillmentResolution(branch, distance, strategy)
        unavailable.update(missing)

    return FulfillmentResolution(
        None,
        None,
        'unavailable',
        tuple(sorted(unavailable or requirements)),
    )
