"""
core/validation.py — Shared numeric-field parsing for hand-rolled views.

There is no Django Form/ModelForm anywhere in this project — every view
parses request.POST itself and collects messages into an `errors` list
(see inventory/views.py product_add for the convention). That style used to
mean every view re-invented its own int()/Decimal() parsing, which is how
inventory/views.py, sales/views.py and procurement/views.py each ended up
with their own near-identical "_safe_positive_int" / "_safe_nonneg_int"
helper, and why none of them enforced an upper bound.

parse_decimal_field() / parse_int_field() are the one place that now owns
"is this a valid number, and is it in range" — callers just do:

    value, error = parse_decimal_field(request.POST.get('unit_price'), 'Retail price', max_value=MAX_PRICE)
    if error:
        errors.append(error)
"""
from decimal import Decimal, InvalidOperation

# A price above this is rejected outright (owner-set ceiling, not a soft
# warning) — applies to any peso-denominated input: unit_price,
# wholesale_price, current_cost_price, etc.
MAX_PRICE = Decimal('250000')

# Generic ceiling for quantity-shaped fields that have no other natural
# bound (a PO line, a POS cart line, a restock request...). Fields that
# already carry their own dynamic ceiling — e.g. a received quantity can't
# exceed what's still owed on the order — keep that business rule instead
# of this flat constant.
MAX_QUANTITY = 100000

# Supplier lead time, in days.
MAX_LEAD_TIME_DAYS = 365


def parse_decimal_field(raw, label, *, required=True, min_value=Decimal('0'), max_value=None):
    """Parse a decimal (peso amount) from a raw POST string.

    Returns (value, error_message) — exactly one of the two is None/not-None.
    A blank, optional field returns (None, None); the caller treats that the
    same as "not provided" (e.g. wholesale_price).
    """
    raw = (raw or '').strip()
    if not raw:
        if required:
            return None, f'{label} is required.'
        return None, None

    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None, f'{label} must be a valid number.'

    if min_value is not None and value < min_value:
        return None, f'{label} cannot be negative.'
    if max_value is not None and value > max_value:
        return None, f'{label} cannot exceed ₱{max_value:,.2f}.'

    return value, None


def parse_int_field(raw, label, *, required=True, min_value=0, max_value=None):
    """Parse a whole number from a raw POST string.

    Returns (value, error_message) — same contract as parse_decimal_field().
    """
    raw = (raw or '').strip()
    if not raw:
        if required:
            return None, f'{label} is required.'
        return None, None

    try:
        value = int(raw)
    except ValueError:
        return None, f'{label} must be a whole number.'

    if min_value is not None and value < min_value:
        return None, f'{label} must be at least {min_value}.'
    if max_value is not None and value > max_value:
        return None, f'{label} cannot exceed {max_value:,}.'

    return value, None
