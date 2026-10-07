"""core/pagination.py — the one way list pages are paginated.

Every long list used to render all of its rows. `paginate()` slices any
queryset or list, `{% pager page_obj noun="product" %}` (see
core/templatetags/pager.py) draws the Previous / page-numbers / Next bar and
carries the page's current filters through every link, so a view only needs:

    page = paginate(request, queryset)
    ... {'page_obj': page, 'rows': page.object_list} ...

A bad `?page=` (junk, 0, past the end) is clamped by Paginator.get_page —
never a 404 — so a bookmarked page that has since shrunk still opens.
"""
from django.core.paginator import Paginator

DEFAULT_PAGE_SIZE = 15
PAGE_PARAM = 'page'


def paginate(request, items, per_page=DEFAULT_PAGE_SIZE, param=PAGE_PARAM):
    """Return the Page for `request.GET[param]` of `items`.

    `items` may be a queryset (give it an ordering — an unordered queryset can
    repeat or skip rows between pages) or a list.
    """
    try:
        number = int(request.GET.get(param, 1))
    except (TypeError, ValueError):
        number = 1
    # Paginator.get_page() sends 0 / negative to the LAST page and only junk
    # to the first; a hand-edited ?page=0 reads as "the start", so say so.
    return Paginator(items, per_page).get_page(max(number, 1))


def page_window(page_obj, around=2):
    """Page numbers to draw as links: first, last, and `around` either side of
    the current page, with None standing for each skipped run ("…")."""
    total = page_obj.paginator.num_pages
    current = page_obj.number
    wanted = {1, total} | set(range(current - around, current + around + 1))
    numbers = sorted(n for n in wanted if 1 <= n <= total)
    window = []
    for n in numbers:
        if window:
            gap = n - window[-1]
            if gap == 2:
                # One skipped page: print it, a "…" would hide nothing.
                window.append(window[-1] + 1)
            elif gap > 2:
                window.append(None)
        window.append(n)
    return window
