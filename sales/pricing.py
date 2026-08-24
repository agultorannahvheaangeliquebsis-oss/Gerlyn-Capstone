"""VAT and statutory-discount arithmetic for checkout.

One module rather than inline maths in each view, because three surfaces have
to agree exactly: the POS cart preview, the sale that gets written, and the
receipt printed afterwards. A rounding difference between any two of them
shows up as a customer dispute at the counter.

The rules implemented here
--------------------------
Shelf prices (Product.unit_price) are VAT-INCLUSIVE — that is how they are
entered today and how they are displayed to customers, so nothing in the
catalog changes. VAT is therefore extracted out of a price, never added on
top of one.

The PWD / Senior Citizen discount is NOT "20% off the sticker price". Under
RA 10754 (PWD) and RA 9994 (Senior Citizens) the sale becomes VAT-exempt and
the 20% comes off the VAT-EXCLUSIVE amount. Taking 20% off the shelf price
directly would over-discount by the VAT the customer no longer owes, so the
order of operations is: strip VAT first, then discount.

Worked example — a ₱112.00 VAT-inclusive item, discount claimed:
    VAT-exclusive base   112.00 / 1.12  = 100.00
    Discount             100.00 * 0.20  =  20.00
    Customer pays        100.00 - 20.00 =  80.00      (no VAT added back)
Taking 20% off ₱112.00 instead would have charged ₱89.60 — ₱9.60 too much.
"""

from decimal import Decimal, ROUND_HALF_UP

# Philippine VAT rate. Kept as a module constant rather than a setting: a rate
# change is a legislative event, not a per-deployment configuration, and it
# would need a migration of historical rows to mean anything anyway.
VAT_RATE = Decimal('0.12')
VAT_DIVISOR = Decimal('1') + VAT_RATE

# Statutory PWD / Senior Citizen discount.
PWD_SENIOR_DISCOUNT_RATE = Decimal('0.20')

PESO = Decimal('0.01')


def peso(amount):
    """Round to centavos, half-up.

    Half-up, not Python's default banker's rounding: receipts are read by
    people who expect .005 to go up, and the difference is visible on a
    printed total.
    """
    return Decimal(amount).quantize(PESO, rounding=ROUND_HALF_UP)


def vat_exclusive(vat_inclusive_price):
    """The price with VAT stripped out."""
    return Decimal(vat_inclusive_price) / VAT_DIVISOR


def price_line(product, quantity, unit_price=None, discount_claimed=False):
    """Work out what one cart line actually costs.

    `unit_price` overrides the product's own price — the caller passes
    wholesale_price for a wholesale order, so the tier decision stays in the
    caller and the tax decision stays here.

    `discount_claimed` is the transaction-level fact (a card was presented).
    Whether it reaches THIS line is decided here, by the product's own
    eligibility flag.

    Returns a dict with the per-unit net price plus the per-line breakdown
    that gets frozen onto SalesItem.
    """
    quantity = int(quantity)
    listed = Decimal(product.unit_price if unit_price is None else unit_price)

    applies = bool(discount_claimed) and product.is_pwd_senior_eligible

    if applies:
        # Strip VAT first (if the product carried any), then discount. The
        # line ends up VAT-exempt either way — that is the point of the
        # statutory discount, not a side effect of the product's own flag.
        base = listed if product.is_vat_exempt else vat_exclusive(listed)
        unit_discount = base * PWD_SENIOR_DISCOUNT_RATE
        net_unit = base - unit_discount
        return {
            'unit_price': peso(net_unit),
            'subtotal': peso(net_unit * quantity),
            'discount_amount': peso(unit_discount * quantity),
            'vat_amount': Decimal('0.00'),
            'is_vat_exempt_line': True,
            'discount_applied': True,
        }

    if product.is_vat_exempt:
        # Exempt product, no discount in play: the shelf price never had VAT
        # in it to begin with, so it passes straight through.
        return {
            'unit_price': peso(listed),
            'subtotal': peso(listed * quantity),
            'discount_amount': Decimal('0.00'),
            'vat_amount': Decimal('0.00'),
            'is_vat_exempt_line': True,
            'discount_applied': False,
        }

    # Ordinary VATable line. The customer pays exactly the shelf price — the
    # VAT figure is recorded for reporting, not charged on top.
    line_total = listed * quantity
    return {
        'unit_price': peso(listed),
        'subtotal': peso(line_total),
        'discount_amount': Decimal('0.00'),
        'vat_amount': peso(line_total - vat_exclusive(line_total)),
        'is_vat_exempt_line': False,
        'discount_applied': False,
    }


def price_cart(lines, discount_claimed=False):
    """Price a whole cart.

    `lines` is an iterable of (product, quantity) or (product, quantity,
    unit_price). Returns the priced lines plus the transaction-level totals
    that get written onto SalesTransaction.
    """
    priced = []
    for line in lines:
        product, quantity = line[0], line[1]
        unit_price = line[2] if len(line) > 2 else None
        result = price_line(
            product, quantity,
            unit_price=unit_price,
            discount_claimed=discount_claimed,
        )
        result['product'] = product
        result['quantity'] = int(quantity)
        priced.append(result)

    return {
        'lines': priced,
        'total_amount': peso(sum((l['subtotal'] for l in priced), Decimal('0'))),
        'total_discount_amount': peso(sum((l['discount_amount'] for l in priced), Decimal('0'))),
        'total_vat_amount': peso(sum((l['vat_amount'] for l in priced), Decimal('0'))),
        'vat_exempt_sales': peso(sum(
            (l['subtotal'] for l in priced if l['is_vat_exempt_line']), Decimal('0')
        )),
        # What the cart would have cost with no discount claimed — the
        # "Subtotal" line a receipt shows above the discount deduction.
        'gross_amount': peso(sum(
            (l['subtotal'] + l['discount_amount'] for l in priced), Decimal('0')
        )),
    }
