"""core/limits.py — the allowed range of every numeric input, in one place.

Before this module each view parsed its own prices and quantities, usually
checking only "is it a number" and "is it above zero". Nothing stopped a
₱99,000,000 retail price or a 4-billion-unit stock count from being typed in
(or posted by hand), and every form wording differed.

Two things live here:

* The LIMITS — MAX_PRICE and friends. Models, DRF serializers, views and the
  HTML `min`/`max` attributes all read these, so the browser hint, the server
  check and the database validator cannot drift apart.
* The PARSERS — parse_money / parse_quantity / parse_text. Each returns
  `(value, error)` where `error` is None or a sentence ready to show the user
  ("Retail price cannot be more than ₱250,000.00."). The server is always the
  authority; the HTML attributes are only a convenience.

Money is Decimal, never float. NaN and Infinity are refused explicitly:
`Decimal('NaN')` parses without error and then raises on the first comparison.
"""
import re
from decimal import ROUND_HALF_UP, Decimal

from django.core.validators import MaxValueValidator, MinValueValidator

# ---------------------------------------------------------------- the limits

# Retail price, bulk (tier) price, supplier cost / offer, PO unit cost, credit
# memo lines — anything that is "what one unit costs".
MIN_PRICE = Decimal('0.01')
MAX_PRICE = Decimal('250000.00')

# A cash amount that is not a unit price: what a customer hands over at the
# register, a refund, a credit. Larger than MAX_PRICE because it can cover
# many units at once, but still bounded.
MAX_AMOUNT = Decimal('10000000.00')

# Units: stock on hand, a reorder level, an order/transfer/receipt line, a
# quantity sold. PositiveIntegerField would take 2,147,483,647; no shelf or
# warehouse in this system holds anything near that.
MAX_QUANTITY = 100_000

# Lines in one bulk floor-refill submit (Alerts Center multi-select). The
# selection travels in a query string and every line costs a stock lookup, so
# it is bounded; 100 is several screens of alerts at 10 rows per page.
MAX_BULK_REFILL_ITEMS = 100

# Days a supplier needs to deliver.
MAX_LEAD_TIME_DAYS = 365

MONEY_PLACES = 2

_DECIMAL_TEXT = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z')

# Free text. Product name / barcode match the column sizes in
# inventory.models.Product; the rest are sensible bounds for the field's job.
MAX_NAME_LENGTH = 150
MAX_BARCODE_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1000
MAX_NOTE_LENGTH = 500

# Uploaded product / banner images.
MAX_IMAGE_MB = 5
MAX_IMAGES_PER_PRODUCT = 10
ALLOWED_IMAGE_TYPES = ('image/jpeg', 'image/png', 'image/webp', 'image/gif')


def peso(amount):
    """₱250,000.00 — how a limit reads in a message."""
    return f'₱{Decimal(amount):,.2f}'


# ------------------------------------------------- model / serializer hooks

def price_validators(minimum=MIN_PRICE, maximum=MAX_PRICE):
    """Validators for a DecimalField, so the admin and the DRF API enforce the
    same range as the web forms."""
    return [
        MinValueValidator(minimum, message=f'Must be at least {peso(minimum)}.'),
        MaxValueValidator(maximum, message=f'Cannot be more than {peso(maximum)}.'),
    ]


def quantity_validators(minimum=0, maximum=MAX_QUANTITY):
    return [
        MinValueValidator(minimum, message=f'Must be at least {minimum:,}.'),
        MaxValueValidator(maximum, message=f'Cannot be more than {maximum:,}.'),
    ]


# ------------------------------------------------------------------ parsers

def parse_money(raw, label, *, minimum=MIN_PRICE, maximum=MAX_PRICE, required=True,
                round_extra=False):
    """Parse a typed amount of money.

    Returns `(Decimal, None)` on success, `(None, message)` on failure, and
    `(None, None)` for an optional blank. `minimum`/`maximum` are inclusive.

    A third decimal place is refused by default (12.505 would otherwise be
    rounded without the person noticing); a caller whose existing rule is to
    round half-up to the centavo passes `round_extra=True`.
    """
    text = ('' if raw is None else str(raw)).strip().replace(',', '')
    if not text:
        return (None, f'{label} is required.') if required else (None, None)
    # Plain decimal notation only. Decimal() itself would also take "1e3",
    # "1_000", "NaN", "Infinity" and non-ASCII digits.
    if not _DECIMAL_TEXT.match(text):
        return None, f'{label} must be a valid number.'
    value = Decimal(text)
    if minimum is not None and value < minimum:
        if minimum == MIN_PRICE:
            return None, f'{label} must be more than ₱0.'
        return None, f'{label} must be at least {peso(minimum)}.'
    if maximum is not None and value > maximum:
        return None, f'{label} cannot be more than {peso(maximum)}.'
    cents = value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if cents != value and not round_extra:
        # 12.505 would be silently rounded by the column; say so instead.
        return None, f'{label} can have at most {MONEY_PLACES} decimal places.'
    return cents, None


def parse_quantity(raw, label, *, minimum=0, maximum=MAX_QUANTITY, required=True):
    """Parse a typed whole number of units.

    Same return shape as parse_money. "12.0", "1e3", "-3" and "٣" are refused:
    a quantity is plain ASCII digits.
    """
    text = ('' if raw is None else str(raw)).strip().replace(',', '')
    if not text:
        return (None, f'{label} is required.') if required else (None, None)
    if not (text.isascii() and text.isdigit()):
        if text.lstrip('-').isascii() and text.lstrip('-').isdigit():
            return None, f'{label} cannot be negative.'
        return None, f'{label} must be a whole number.'
    # int() refuses a string of more than ~4,300 digits with a ValueError, so
    # anything this long is "over the maximum" without ever being converted.
    if len(text) > 12:
        return None, (
            f'{label} cannot be more than {maximum:,}.' if maximum is not None
            else f'{label} is too large.'
        )
    value = int(text)
    if minimum is not None and value < minimum:
        return None, f'{label} must be at least {minimum:,}.'
    if maximum is not None and value > maximum:
        return None, f'{label} cannot be more than {maximum:,}.'
    return value, None


def parse_text(raw, label, *, max_length, required=False, min_length=0):
    """Trim and length-check free text. Returns `(text, error)`."""
    text = ('' if raw is None else str(raw)).strip()
    if not text:
        return ('', f'{label} is required.') if required else ('', None)
    if len(text) < min_length:
        return text, f'{label} must be at least {min_length} characters.'
    if len(text) > max_length:
        return text, f'{label} cannot be longer than {max_length} characters.'
    return text, None


def validate_image_uploads(files, label='Image'):
    """Error messages for a list of uploaded images (empty list = all fine)."""
    errors = []
    if len(files) > MAX_IMAGES_PER_PRODUCT:
        errors.append(f'You can upload at most {MAX_IMAGES_PER_PRODUCT} images at a time.')
    limit = MAX_IMAGE_MB * 1024 * 1024
    for f in files:
        name = getattr(f, 'name', 'file')
        if getattr(f, 'content_type', '') not in ALLOWED_IMAGE_TYPES:
            errors.append(f'{label} "{name}" must be a JPG, PNG, WebP or GIF picture.')
        elif f.size > limit:
            errors.append(f'{label} "{name}" is larger than {MAX_IMAGE_MB} MB.')
    return errors
