"""Cache for small, rarely-changing reference data — branches, categories,
the product catalog — that nearly every internal list/filter view
re-queries on every single request just to populate a dropdown.

Exists because of what the Railway DB latency investigation found: each
query here is cheap by row count (a handful of branches, a couple dozen
products) but not by round-trip cost, and this exact shape
(Branch.objects.all() / ProductCategory.objects.all()) is duplicated
across a dozen+ views. Caching it trades a little staleness for cutting
one full DB round trip off nearly every internal page.

The TTL is a safety net, not the invalidation mechanism — every write path
(branches/views.py, inventory/views.py, the categories API) already calls
bust_branches_cache()/bust_categories_cache() on the same request that
changes the data, so a real edit is never actually stale. That makes the
TTL free to set long: measured locally, a cold DatabaseCache miss here
costs 5 queries (SELECT, DELETE, COUNT, re-check SELECT, INSERT) against 1
for a hit, so a short TTL mostly bought more frequent expensive misses,
not fresher data.
"""
from django.core.cache import cache

REFDATA_CACHE_SECONDS = 600


def cached_branches():
    # A cache backend failure here (Redis unreachable, misconfigured) must
    # degrade to "cache miss", not an unhandled exception — this is a pure
    # performance cache called from nearly every internal page, so letting
    # a Redis hiccup propagate up would turn "a bit slower" into "the whole
    # admin side is down". The DB row is the real source of truth either
    # way; unlike the login-lockout/OTP caches, there is no security
    # property being enforced here that a silent fallback would weaken.
    try:
        branches = cache.get('refdata:branches')
    except Exception:
        branches = None
    if branches is None:
        from branches.models import Branch

        branches = list(Branch.objects.all())
        try:
            cache.set('refdata:branches', branches, REFDATA_CACHE_SECONDS)
        except Exception:
            pass
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
    # See cached_branches()'s comment — same fallback-to-DB-on-any-cache-
    # error reasoning.
    try:
        categories = cache.get('refdata:categories')
    except Exception:
        categories = None
    if categories is None:
        from inventory.models import ProductCategory

        categories = list(
            ProductCategory.objects.select_related('parent')
            .order_by('parent__category_name', 'category_name')
        )
        try:
            cache.set('refdata:categories', categories, REFDATA_CACHE_SECONDS)
        except Exception:
            pass
    return categories


def bust_branches_cache():
    """Call after any Branch create/update/delete so the next read is fresh
    instead of serving a stale list for up to REFDATA_CACHE_SECONDS."""
    try:
        cache.delete('refdata:branches')
    except Exception:
        pass


def bust_categories_cache():
    """Call after any ProductCategory create/update/delete — same reasoning
    as bust_branches_cache()."""
    try:
        cache.delete('refdata:categories')
    except Exception:
        pass
