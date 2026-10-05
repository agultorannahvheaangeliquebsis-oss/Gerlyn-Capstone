"""
core/pagination.py — Shared list-page pagination.

No list view in this project paginates today — each one builds up a fully
filtered, fully ordered queryset and hands the whole thing to the template
for a bare `{% for %}`. paginate() is the one place that turns that queryset
into a `Page` of `per_page` records, read from `?page=`.

Call it last, after any `.count()`-based tab badges and after every
`.filter()`/`.order_by()` step — the paginated Page is just a window onto
whatever queryset it's given, so counts computed on the unpaginated queryset
earlier in the view stay correct.

    records = records.order_by('-date_recorded')
    ...
    page_obj = paginate(request, records)
    return render(request, 'billing/billing_list.html', {'page_obj': page_obj, ...})

Then in the template: `{% for r in page_obj %}` in place of `{% for r in records %}`,
plus `{% include "partials/_pagination.html" %}` right after the table.
"""
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator

DEFAULT_PER_PAGE = 20


def paginate(request, queryset, per_page=DEFAULT_PER_PAGE, page_param='page'):
    """Return the requested Page of `queryset`, clamped to a valid page.

    `page_param` only needs to change when a single page hosts more than
    one paginated list (e.g. transfer_list.html's Transfers tab and its
    Movement Log tab) — each needs its own querystring key so paging one
    doesn't reset the other.
    """
    paginator = Paginator(queryset, per_page)
    page_number = request.GET.get(page_param, 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Django's {% for %} tag can't call a method with arguments, so the
    # elided "1 … 4 5 6 … 12" range is computed here and attached to the
    # Page for the pagination partial to just iterate.
    page_obj.elided_page_range = list(
        paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1)
    )
    return page_obj
