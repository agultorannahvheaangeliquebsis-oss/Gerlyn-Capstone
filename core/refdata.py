"""Short-TTL cache for small, rarely-changing reference data — branches,
categories, the product catalog — that nearly every internal list/filter
view re-queries on every single request just to populate a dropdown.

Exists because of what the Railway DB latency investigation found: each
query here is cheap by row count (a handful of branches, a couple dozen
products) but not by round-trip cost, and this exact shape
(Branch.objects.all() / ProductCategory.objects.all()) is duplicated
across a dozen+ views. A short cache window trades a few seconds of
staleness on data that only ever changes via an explicit admin action,
for cutting one full DB round trip off nearly every internal page.
"""
from django.core.cache import cache

REFDATA_CACHE_SECONDS = 60


def cached_branches():
    branches = cache.get('refdata:branches')
    if branches is None:
        from branches.models import Branch

        branches = list(Branch.objects.all())
        cache.set('refdata:branches', branches, REFDATA_CACHE_SECONDS)
    return branches


def cached_categories():
    """Every category, departments first and each department's shelves
    grouped under it.

    select_related('parent') is not an optimisation here, it is what makes
    the list usable: since categories became two levels deep, the internal
    pickers print ProductCategory.__str__, which is "Fresh > Fresh Beef" for
    a shelf. Without the join that is one extra query per option — thirty-odd
    on a real catalog, on a list whose entire reason for being cached is to
    save a single round trip.

    parent__category_name leads the ordering so a shelf sorts beside its
    department instead of alphabetically among the departments — NULLs sort
    first in MySQL, which puts the departments that have no shelves at the
    top rather than scattered through.
    """
    categories = cache.get('refdata:categories')
    if categories is None:
        from inventory.models import ProductCategory

        categories = list(
            ProductCategory.objects.select_related('parent')
            .order_by('parent__category_name', 'category_name')
        )
        cache.set('refdata:categories', categories, REFDATA_CACHE_SECONDS)
    return categories


def bust_branches_cache():
    """Call after any Branch create/update/delete so the next read is fresh
    instead of serving a stale list for up to REFDATA_CACHE_SECONDS."""
    cache.delete('refdata:branches')


def bust_categories_cache():
    """Call after any ProductCategory create/update/delete — same reasoning
    as bust_branches_cache()."""
    cache.delete('refdata:categories')
