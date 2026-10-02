"""core/querystrings.py — shared query-string builder for filter toolbars.

O-3 — the Order Console and Billing list each hand-built their status tabs'
hrefs, one string literal per tab, and each only listed SOME of the filters
their own toolbar actually offered. A filter added after the tabs were
written (Order Console's `type`) was silently dropped on every tab click,
and neither one ran the search term through `|urlencode`, so a query
containing "&" or "#" broke every tab link on the page.

One function, used by both list_filter_querystring callers, so a filter
added to a toolbar tomorrow can't be forgotten by its tabs again — the
template only ever appends its own `&status=...` on top.
"""
from django.http import QueryDict


def list_filter_querystring(**params):
    """Every non-empty keyword param, urlencoded. `status` is deliberately
    never one of them — it's the one part of the query string a status tab
    supplies itself."""
    qs = QueryDict(mutable=True)
    for key, value in params.items():
        if value:
            qs[key] = value
    return qs.urlencode()


def sort_header(label, current, asc, desc=None, **params):
    """One clickable column header for a server-sorted table.

    The reusable half of the sortable-header pattern (first used by the
    Dashboard's Open Orders table). Pair it with `partials/_sort_th.html`:

      view      cols = [sort_header('Total', sort, 'total', '-total', **keep), ...]
      template  {% include "partials/_sort_th.html" with col=cols.0 cls="num" %}

    `current` is the sort key now applied (already whitelisted by the view),
    `asc`/`desc` are this column's whitelist keys. A column with both toggles
    on repeat clicks; one with only `asc` always links to `asc`. `params` are
    every OTHER query param the page must keep (branch, range, filters) —
    they ride through `list_filter_querystring`, so a sort click can never
    drop a filter. Returned `href` is a bare query string (no leading "?").
    """
    if current == asc:
        direction, target = 'asc', desc or asc
    elif desc and current == desc:
        direction, target = 'desc', asc
    else:
        direction, target = None, asc
    return {
        'label': label,
        'href': list_filter_querystring(**params, sort=target),
        'active': direction is not None,
        'direction': direction,
    }
