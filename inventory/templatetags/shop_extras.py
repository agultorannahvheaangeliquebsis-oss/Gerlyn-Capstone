"""Small presentation helpers for the storefront."""
from django import template

register = template.Library()

# Products have no image field, so the catalog shows a category glyph.
CATEGORY_EMOJI = {
    'Canned Goods': '🥫',
    'Noodles & Pasta': '🍜',
    'Beverages': '☕',
    'Dairy & Milk': '🥛',
    'Condiments': '🍅',
    'Baby & Personal Care': '👶',
    'Rice & Grains': '🍚',
    'Snacks': '🍪',
}
DEFAULT_EMOJI = '📦'


@register.filter
def product_emoji(product):
    """Glyph for a product, chosen by its category."""
    category = getattr(product, 'category', None)
    name = getattr(category, 'category_name', '') if category else ''
    return CATEGORY_EMOJI.get(name, DEFAULT_EMOJI)


@register.filter
def category_emoji(category_name):
    return CATEGORY_EMOJI.get(category_name, DEFAULT_EMOJI)


# How many tints .cat .ring defines in storefront.css. Kept as a constant so
# the two cannot drift apart silently — adding a ninth swatch there means
# raising this, and nothing else.
CATEGORY_TINT_COUNT = 8


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
