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
