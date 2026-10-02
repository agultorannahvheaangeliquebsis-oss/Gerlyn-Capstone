"""Automatic selling-price computation.

A product's shelf price is *suggested* from supplier cost, never silently
rewritten. The formula follows how shelf prices are defined in sales.pricing
(VAT-inclusive): markup applies to the VAT-exclusive cost and VAT is added on
top,

    suggested = cost x (1 + markup%) x 1.12     (rounded half-up to centavos)

Cost basis is the preferred approved supplier link's current_cost_price; with
no preferred link that carries a cost, the cheapest approved link with a cost.
With no usable cost, or no markup configured anywhere, there is no suggestion
and the price stays a manual entry. Bulk tiers are untouched.

The markup resolves product -> category -> store-wide default.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db import transaction
from django.urls import reverse

from sales.pricing import VAT_DIVISOR, peso

SOURCE_PRODUCT = 'product'
SOURCE_CATEGORY = 'category'
SOURCE_STORE = 'store'


class PriceSuggestionError(Exception):
    """The suggested price cannot be applied (none exists, or it breaks tiers)."""


@dataclass(frozen=True)
class PriceSuggestion:
    cost: Optional[Decimal] = None
    markup_percent: Optional[Decimal] = None
    markup_source: Optional[str] = None
    suggested_price: Optional[Decimal] = None

    @property
    def available(self):
        return self.suggested_price is not None


def resolve_markup(product, category=None):
    """(percent, source) for `product`, or (None, None) when none is set.

    `category` lets a form that is still choosing one ask about a category
    the product is not saved under yet.
    """
    if product is not None and product.markup_percent is not None:
        return product.markup_percent, SOURCE_PRODUCT
    if category is None and product is not None:
        category = product.category
    if category is not None and category.default_markup_percent is not None:
        return category.default_markup_percent, SOURCE_CATEGORY
    from core.models import StoreSetting
    store_default = StoreSetting.load().default_markup_percent
    if store_default is not None:
        return store_default, SOURCE_STORE
    return None, None


def cost_basis(product):
    """The supplier cost the suggestion is computed from, or None."""
    from procurement.models import SupplierProduct

    links = list(SupplierProduct.objects.approved().filter(
        product=product, current_cost_price__isnull=False,
    ))
    if not links:
        return None
    preferred = [link for link in links if link.is_preferred]
    return min(link.current_cost_price for link in (preferred or links))


def parse_markup_percent(raw, label='Markup'):
    """(Decimal | None, error | None) for an optional markup-percent field."""
    raw = (raw or '').strip()
    if not raw:
        return None, None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None, f'{label} must be a number.'
    if not value.is_finite() or value < 0 or value > Decimal('9999.99'):
        return None, f'{label} must be between 0 and 9999.99.'
    return value.quantize(Decimal('0.01')), None


def price_from_cost(cost, markup_percent):
    """The formula on its own, for callers that already hold both numbers."""
    multiplier = Decimal('1') + Decimal(markup_percent) / Decimal('100')
    return peso(Decimal(cost) * multiplier * VAT_DIVISOR)


def suggest_price(product):
    cost = cost_basis(product)
    markup, source = resolve_markup(product)
    suggested = None
    if cost is not None and markup is not None:
        suggested = price_from_cost(cost, markup)
    return PriceSuggestion(cost, markup, source, suggested)


def tier_conflict(product, price):
    """A message when `price` would not stay above every bulk tier, else None.

    Tier prices must stay below retail (the product form enforces it) and
    tiers are never recomputed here, so a too-low suggestion is refused
    instead of leaving an inverted ladder.
    """
    top = max((t.unit_price for t in product.price_tiers.all()), default=None)
    if top is not None and price <= top:
        return (
            f'The computed price ₱{price} is not above the highest bulk '
            f'tier (₱{top}). Review the tiers first.'
        )
    return None


def sync_manual_flag(product, new_price):
    """Record whether `new_price` is a deliberate departure from the suggestion.

    Call just before saving a price a person chose. With no suggestion
    available there is nothing to depart from, so the flag is left alone.
    """
    if not product.pk:
        return
    suggestion = suggest_price(product)
    if suggestion.available:
        product.price_is_manual = Decimal(new_price) != suggestion.suggested_price


def apply_suggested_price(product, *, reason='Applied the computed selling price'):
    """Move the product's retail price to the suggestion and return the product.

    Row-locks the product so two clicks can't interleave. Clears
    price_is_manual: taking the computed price is how an Owner reverts an
    override. Audit rows come from the registered Product model's signals.
    """
    from audit.context import set_action_label
    from inventory.models import Product

    with transaction.atomic():
        locked = Product.objects.select_for_update().get(pk=product.pk)
        suggestion = suggest_price(locked)
        if not suggestion.available:
            raise PriceSuggestionError(
                'No price can be computed: the product needs an approved '
                'supplier cost and a markup.'
            )
        conflict = tier_conflict(locked, suggestion.suggested_price)
        if conflict:
            raise PriceSuggestionError(conflict)
        set_action_label(reason)
        locked.unit_price = suggestion.suggested_price
        locked.price_is_manual = False
        locked.save(update_fields=['unit_price', 'price_is_manual'])
    product.unit_price = locked.unit_price
    product.price_is_manual = False
    return product


def on_cost_change(product, actor=None):
    """React to a change in what the supplier charges for `product`.

    Suggest-only by default: tell the Owner when the computed price now
    differs from the shelf price. A product that opted in to auto-apply is
    repriced instead (and the Owner told). Manual overrides stay quiet.
    Returns 'applied', 'notified' or None.
    """
    from core.notifications import EVENT_PRICE_SUGGESTION, notify_event

    suggestion = suggest_price(product)
    if (not suggestion.available or product.price_is_manual
            or suggestion.suggested_price == product.unit_price):
        return None

    applied = False
    if product.auto_apply_suggested_price:
        try:
            apply_suggested_price(
                product, reason='Auto-applied the computed selling price',
            )
            applied = True
        except PriceSuggestionError:
            pass

    if applied:
        title = f'Price updated: {product.product_name}'
        message = (
            f'Supplier cost changed; the retail price of {product.product_name} '
            f'was set to ₱{product.unit_price} automatically.'
        )
    else:
        title = f'Price suggestion: {product.product_name}'
        message = (
            f'Supplier cost changed. Suggested retail price for '
            f'{product.product_name} is ₱{suggestion.suggested_price} '
            f'(currently ₱{product.unit_price}).'
        )
    notify_event(
        EVENT_PRICE_SUGGESTION, title, message,
        actor=actor,
        action_url=reverse('product_edit', kwargs={'pk': product.pk}),
        action_label='Review price',
        reference_type='product', reference_id=product.pk,
    )
    return 'applied' if applied else 'notified'
