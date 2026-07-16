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
