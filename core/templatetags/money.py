"""Money and percentage filters that cope with the cases a profit report hits
and ``floatformat`` does not: a negative amount (a loss) and a figure that
does not exist (a margin on zero sales)."""
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def peso(value, places=2):
    """₱1,234.50, or -₱1,234.50 for a loss (never ₱-1,234.50). Empty for None."""
    if value in (None, ''):
        return ''
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return ''
    places = int(places)
    text = f'{abs(amount):,.{places}f}'
    return f'-₱{text}' if amount < 0 and any(c in '123456789' for c in text) else f'₱{text}'


@register.filter
def pct(value):
    """68.3%, or an em dash when there is no figure to show."""
    if value in (None, ''):
        return '—'
    return f'{Decimal(value):,.1f}%'
