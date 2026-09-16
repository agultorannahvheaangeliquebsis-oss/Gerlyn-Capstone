"""Small presentation helpers for the storefront."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import template
from django.utils.safestring import mark_safe

register = template.Library()

# Products have no image field, so the catalog shows a category glyph where a
# photo would go.
#
# Line art, not emoji. These used to be 🥫 / 🍜 / ☕ and so on, which had
# three problems at once: an emoji is a font, so the store's own catalog was
# drawn by whatever set the shopper's OS ships (Apple's glossy 3D can, Google's
# flat one, Microsoft's outline) and looked like a different shop on each; it
# arrives in full colour beside a page whose every other mark is a 2px grey
# stroke; and a screen reader announces "canned food" in the middle of a
# product name. The SVGs below are the same drawing everywhere, inherit
# currentColor from the tile, and are aria-hidden.
#
# 1em, not a pixel size: .pcard .thumb, .cart-thumb and .pdp-frame--empty each
# size this glyph with font-size (46px, 32px and 110px), which is how the
# emoji scaled. Keeping the unit means those three rules still work untouched.
_SVG = (
    '<svg class="prod-glyph" width="1em" height="1em" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '{}</svg>'
)

CATEGORY_PATHS = {
    # a can: cylinder with a rim seam top and bottom
    'Canned Goods': '<rect x="6" y="4" width="12" height="16" rx="2"/>'
                    '<path d="M6 8h12M6 16h12"/>',
    # a noodle bowl, steam rising
    'Noodles & Pasta': '<path d="M3 11h18a9 9 0 0 1-18 0z"/>'
                       '<path d="M9 7c0-1.5 1-2 1-3M13 7c0-1.5 1-2 1-3"/>',
    # a cup with a handle
    'Beverages': '<path d="M5 8h11v7a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4z"/>'
                 '<path d="M16 10h2a2.5 2.5 0 0 1 0 5h-2"/>'
                 '<path d="M8 3v2M12 3v2"/>',
    # a milk carton, gable top
    'Dairy & Milk': '<path d="M8 3h8l2 4v14H6V7z"/><path d="M8 3 6 7M16 3l2 4"/>'
                    '<path d="M10 13h4"/>',
    # a sauce bottle with a label band
    'Condiments': '<path d="M10 3h4v3l2 3v12H8V9l2-3z"/><path d="M8 13h8"/>',
    # a feeding bottle
    'Baby & Personal Care': '<path d="M9.5 2h5v3h-5z"/>'
                            '<path d="M10 5h4a3 3 0 0 1 2 3v11a2 2 0 0 1-2 2h-4'
                            'a2 2 0 0 1-2-2V8a3 3 0 0 1 2-3z"/>'
                            '<path d="M9 11h6"/>',
    # a tied sack
    'Rice & Grains': '<path d="M7 8h10l2 9a3 3 0 0 1-3 4H8a3 3 0 0 1-3-4z"/>'
                     '<path d="M9 8V6a3 3 0 0 1 6 0v2"/><path d="M10 14h4"/>',
    # a cookie
    'Snacks': '<circle cx="12" cy="12" r="9"/><circle cx="9.5" cy="10" r=".9"/>'
              '<circle cx="14" cy="9.5" r=".9"/><circle cx="12.5" cy="14.5" r=".9"/>',
}
# The same box as partials/_status_icon.html draws for "stock" — one parcel in
# the app, not two.
DEFAULT_PATH = '<path d="M20 7 12 3 4 7v10l8 4 8-4z"/><path d="m4 7 8 4 8-4M12 21V11"/>'


@register.filter
def product_icon(product):
    """Glyph for a product, chosen by its category."""
    category = getattr(product, 'category', None)
    name = getattr(category, 'category_name', '') if category else ''
    return category_icon(name)


@register.filter
def category_icon(category_name):
    """Glyph for a category name. Safe: the markup is this module's own, and
    nothing from the database reaches the output — category_name is only ever
    a dictionary key here."""
    return mark_safe(_SVG.format(CATEGORY_PATHS.get(category_name, DEFAULT_PATH)))


# How many tints .cat .ring defines in storefront.css. Kept as a constant so
# the two cannot drift apart silently — adding a ninth swatch there means
# raising this, and nothing else.
CATEGORY_TINT_COUNT = 8


@register.filter
def percent_off(tier_price, retail_price):
    """Whole-number % a bulk tier undercuts the retail price by, for the
    product page's buy-more-save-more ladder.

    Computed here rather than stored on ProductPriceTier: it is retail vs.
    this one tier, always derived, and storing it would let the two drift
    apart the moment either price changed. Never negative — a tier priced at
    or above retail (a data-entry slip, not a real deal) reads as 0% rather
    than a negative discount, which would be a stranger number on the page
    than no badge at all.
    """
    try:
        retail = Decimal(retail_price)
        tier = Decimal(tier_price)
    except (InvalidOperation, TypeError):
        return 0
    if retail <= 0 or tier >= retail:
        return 0
    return int(((retail - tier) / retail * 100).to_integral_value(rounding=ROUND_HALF_UP))


@register.filter
def category_tint(category):
    """Stable tint class (`t0`–`t7`) for a category's tile.

    Keyed off the primary key rather than a hash of the name, so renaming a
    category does not change its colour, and the same category is the same
    colour on every page and after every restart — which is the only thing
    that makes the tint a recognition cue rather than decoration.
    """
    pk = getattr(category, 'pk', None) or 0
    return 't{}'.format(pk % CATEGORY_TINT_COUNT)
