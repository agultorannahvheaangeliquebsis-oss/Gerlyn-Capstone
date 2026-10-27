"""Where a branch delivers: the one place the rule lives.

Checkout, the customer sales API, the Order Console's approve step and the
public "serviceable areas" pages all ask this module, so the answer cannot
differ between them.

The rule (``decide``):

* A branch with NO service-area rows is *unconfigured*: delivery is not
  restricted for it. Once it has any row, delivery is limited to what its
  active rows cover. (The Administration page warns about an unconfigured
  branch so that "open to everyone" is never a silent state.)
* A place is looked up by PSGC code at three levels: its barangay, its
  municipality, its province. The MOST SPECIFIC entry that exists decides:
  active covers the place, inactive excludes it, whatever a broader entry
  says. A place with no entry at any level is outside.
* Fee and minimum order come from the most specific ACTIVE entry in that
  chain that sets one; a blank value inherits from the broader entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from accounts import geo

from .models import Branch, ServiceArea

# Coverage statuses.
OPEN = 'open'                    # branch has no rows: delivery is unrestricted
COVERED = 'covered'
OUTSIDE = 'outside'              # no entry covers it, or an inactive one excludes it
INCOMPLETE = 'incomplete'        # province / municipality / barangay not all given
UNRECOGNISED = 'unrecognised'    # words that match nothing in the PSGC list
NEEDS_BARANGAY = 'needs_barangay'  # barangay unmatched where one is excluded
BELOW_MINIMUM = 'below_minimum'  # covered, but the order is under the area's minimum

CHOOSE_PLACE = (
    'Choose your province, municipality and barangay from the lists so we can '
    'check whether we deliver there.'
)


def _peso(amount):
    return f'₱{Decimal(amount):,.2f}'


@dataclass(frozen=True)
class Coverage:
    status: str
    message: str = ''
    area: ServiceArea | None = None
    delivery_fee: Decimal | None = None
    minimum_order: Decimal | None = None
    place_label: str = ''
    shortfall: Decimal | None = None

    @property
    def covered(self):
        """The place itself may be delivered to (the order size aside)."""
        return self.status in (OPEN, COVERED, BELOW_MINIMUM)

    @property
    def ok(self):
        """Delivery may go ahead."""
        return self.status in (OPEN, COVERED)


def _by_key(rows):
    index = {}
    for row in rows:
        index[(row.area_type, row.province_code, row.city_code, row.barangay_code)] = row
    return index


def _chain(rows, place):
    """The entries that exist for ``place``, most specific first."""
    index = _by_key(rows)
    keys = []
    if place.barangay_code:
        keys.append((ServiceArea.AreaType.BARANGAY, place.province_code,
                     place.city_code, place.barangay_code))
    if place.city_code:
        keys.append((ServiceArea.AreaType.MUNICIPALITY, place.province_code,
                     place.city_code, ''))
    if place.province_code:
        keys.append((ServiceArea.AreaType.PROVINCE, place.province_code, '', ''))
    return [index[key] for key in keys if key in index]


def _inherited(chain, field):
    for row in chain:
        if row.is_active and getattr(row, field) is not None:
            return getattr(row, field)
    return None


def decide(branch_name, rows, place, order_total=None):
    """Coverage of ``place`` by a branch's ``rows`` (a list of ServiceArea)."""
    if not rows:
        return Coverage(OPEN, place_label=place.label())

    label = place.label()
    if place.is_empty:
        return Coverage(INCOMPLETE, CHOOSE_PLACE)
    if not place.province_code:
        return Coverage(
            UNRECOGNISED,
            f'We could not match {label} to a place on our list. Choose your '
            f'province and municipality from the lists, or choose Pickup.',
            place_label=label,
        )
    if not place.city_code:
        return Coverage(
            INCOMPLETE if not place.city_text else UNRECOGNISED,
            CHOOSE_PLACE if not place.city_text else
            f'We could not match the municipality “{place.city_text}” in '
            f'{place.province_name}. Choose it from the list, or choose Pickup.',
            place_label=label,
        )
    if not place.barangay_code and not place.barangay_name:
        return Coverage(INCOMPLETE, CHOOSE_PLACE, place_label=label)
    if not place.barangay_code:
        # A barangay that is not on the list cannot be tested against a
        # barangay-level exclusion, so where this municipality has one the
        # shopper must pick from the list rather than slip past it by typing.
        excluded_here = any(
            r.area_type == ServiceArea.AreaType.BARANGAY and not r.is_active
            and r.city_code == place.city_code
            for r in rows
        )
        if excluded_here:
            return Coverage(
                NEEDS_BARANGAY,
                f'Choose your barangay in {place.city_name} from the list so we '
                f'can check it.',
                place_label=label,
            )

    chain = _chain(rows, place)
    if not chain:
        return Coverage(
            OUTSIDE,
            f'{label} is outside {branch_name}’s delivery areas.',
            place_label=label,
        )
    deciding = chain[0]
    if not deciding.is_active:
        return Coverage(
            OUTSIDE,
            f'{branch_name} does not deliver to {label} right now.',
            area=deciding, place_label=label,
        )

    fee = _inherited(chain, 'delivery_fee')
    minimum = _inherited(chain, 'minimum_order')
    if minimum is not None and order_total is not None and Decimal(order_total) < minimum:
        shortfall = minimum - Decimal(order_total)
        return Coverage(
            BELOW_MINIMUM,
            f'Delivery to {label} needs an order of at least {_peso(minimum)} '
            f'(add {_peso(shortfall)} more), or choose Pickup.',
            area=deciding, delivery_fee=fee, minimum_order=minimum,
            place_label=label, shortfall=shortfall,
        )
    return Coverage(
        COVERED, area=deciding, delivery_fee=fee, minimum_order=minimum,
        place_label=label,
    )


def check_delivery(branch, place, order_total=None):
    """Can ``branch`` deliver to ``place`` (a ``geo.Place``)?"""
    return decide(
        branch.branch_name, list(ServiceArea.objects.filter(branch=branch)),
        place, order_total,
    )


def is_configured(branch):
    return ServiceArea.objects.filter(branch=branch).exists()


def covering_branches(place, exclude_id=None, order_total=None):
    """Active branches that can deliver to ``place``, for "Choose another
    branch". Unconfigured branches are left out: they are open to anywhere,
    which says nothing about this place."""
    rows_by_branch = {}
    for row in ServiceArea.objects.filter(branch__is_active=True).select_related('branch'):
        rows_by_branch.setdefault(row.branch_id, []).append(row)
    found = []
    for branch_id, rows in rows_by_branch.items():
        if branch_id == exclude_id:
            continue
        result = decide(rows[0].branch.branch_name, rows, place, order_total)
        if result.ok:
            found.append(rows[0].branch)
    return sorted(found, key=lambda b: b.branch_name)


# ----------------------------------------------------------------- display

_TYPE_ORDER = {
    ServiceArea.AreaType.PROVINCE: 0,
    ServiceArea.AreaType.MUNICIPALITY: 1,
    ServiceArea.AreaType.BARANGAY: 2,
}


def parent_label(row):
    """"Baliuag, Bulacan" for a barangay, "Bulacan" for a municipality."""
    if row.area_type == ServiceArea.AreaType.MUNICIPALITY:
        return geo.province_name(row.province_code)
    if row.area_type == ServiceArea.AreaType.BARANGAY:
        return ', '.join(p for p in (geo.city_name(row.city_code), geo.province_name(row.province_code)) if p)
    return ''


def _row_view(row):
    return {
        'area': row,
        'type': row.get_area_type_display(),
        'name': row.area_name,
        'parent': parent_label(row),
        'delivery_fee': row.delivery_fee,
        'minimum_order': row.minimum_order,
        'notes': row.notes,
        'is_active': row.is_active,
    }


def sorted_areas(rows):
    """Province entries first, then municipalities, then barangays; by name."""
    return sorted(rows, key=lambda r: (_TYPE_ORDER[r.area_type], r.area_name.casefold()))


def public_listing(branches=None):
    """What shoppers are told, grouped by branch: the areas delivered to and,
    separately, the places carved out of them. Branches with no rows are left
    out (they are unrestricted and have nothing to list)."""
    qs = ServiceArea.objects.select_related('branch').filter(branch__is_active=True)
    if branches is not None:
        qs = qs.filter(branch__in=branches)
    grouped = {}
    for row in qs:
        grouped.setdefault(row.branch_id, []).append(row)
    listing = []
    for rows in grouped.values():
        branch = rows[0].branch
        listing.append({
            'branch': branch,
            'areas': [_row_view(r) for r in sorted_areas(r for r in rows if r.is_active)],
            'excluded': [_row_view(r) for r in sorted_areas(r for r in rows if not r.is_active)],
        })
    return sorted(listing, key=lambda g: g['branch'].branch_name)


BANNER_ITEM_LIMIT = 3


def _summary_item(row):
    if row.area_type == ServiceArea.AreaType.PROVINCE:
        return f'all of {row.area_name}'
    if row.area_type == ServiceArea.AreaType.MUNICIPALITY:
        return row.area_name
    return f'Brgy. {row.area_name}, {geo.city_name(row.city_code)}'.rstrip(', ')


def banner_summary(branches):
    """``(configured, text)`` for the "We deliver to:" banner.

    ``configured`` is False when none of ``branches`` has any rows, in which
    case there is nothing true to say and no banner is shown. The text names
    the broadest active entries only (a municipality inside an active
    province, say, adds nothing), at most ``BANNER_ITEM_LIMIT`` of them.
    """
    rows = list(ServiceArea.objects.filter(branch__in=branches))
    if not rows:
        return False, ''
    active = [r for r in rows if r.is_active]
    provinces = {(r.branch_id, r.province_code) for r in active
                 if r.area_type == ServiceArea.AreaType.PROVINCE}
    cities = {(r.branch_id, r.city_code) for r in active
              if r.area_type == ServiceArea.AreaType.MUNICIPALITY}
    items = []
    for row in sorted_areas(active):
        if row.area_type != ServiceArea.AreaType.PROVINCE and (row.branch_id, row.province_code) in provinces:
            continue
        if row.area_type == ServiceArea.AreaType.BARANGAY and (row.branch_id, row.city_code) in cities:
            continue
        item = _summary_item(row)
        if item not in items:
            items.append(item)
    if not items:
        return True, ''
    shown, extra = items[:BANNER_ITEM_LIMIT], len(items) - BANNER_ITEM_LIMIT
    text = ', '.join(shown)
    if extra > 0:
        text += f' and {extra} more'
    return True, text


# -------------------------------------------------------------------- seed

PLACEHOLDER_NOTE = 'Placeholder - the Owner should confirm this in Administration.'


def seed_default_areas(branches=None):
    """Give each branch the placeholder Bulacan / Baliuag entries it ships with.

    Idempotent and non-destructive: an entry that exists is left exactly as the
    Owner last edited it (including one they deactivated). Returns the number
    of rows created. The fees are placeholders and no minimum order is set, so
    seeding never makes a real order impossible.
    """
    province = geo.resolve_place('Bulacan')
    place = geo.resolve_place('Bulacan', 'Baliuag')
    created = 0
    for branch in (Branch.objects.all() if branches is None else branches):
        _, made = ServiceArea.objects.get_or_create(
            branch=branch, area_type=ServiceArea.AreaType.PROVINCE,
            province_code=province.province_code, city_code='', barangay_code='',
            defaults={'area_name': province.province_name, 'is_active': True,
                      'delivery_fee': Decimal('100.00'), 'notes': PLACEHOLDER_NOTE},
        )
        created += made
        _, made = ServiceArea.objects.get_or_create(
            branch=branch, area_type=ServiceArea.AreaType.MUNICIPALITY,
            province_code=place.province_code, city_code=place.city_code,
            barangay_code='',
            defaults={'area_name': place.city_name, 'is_active': True,
                      'delivery_fee': Decimal('50.00'), 'notes': PLACEHOLDER_NOTE},
        )
        created += made
    return created
