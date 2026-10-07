from django import template

from accounts.translations import translate
from core.pagination import PAGE_PARAM, page_window

register = template.Library()

# Query parameters that open a modal for one specific record (or a one-off
# import step). They describe an action in progress, not a filter, so paging
# must not carry them along — turning the page would otherwise reopen the
# same dialog on every page.
TRANSIENT_PARAMS = (
    'receive', 'edit', 'import', 'open_create', 'open_request', 'return',
)


def _plural(noun):
    """category -> categories, order -> orders. Only the regular cases the
    page nouns here need; pass plural= for anything else."""
    if noun.endswith('y') and noun[-2:-1] not in ('a', 'e', 'i', 'o', 'u', ''):
        return noun[:-1] + 'ies'
    return noun + 's'


@register.inclusion_tag('partials/_pager.html', takes_context=True)
def pager(context, page_obj, noun='record', param=PAGE_PARAM, plural=None):
    """Previous / numbered pages / Next for `page_obj`.

    Links keep every other query-string filter the page was opened with
    (search, status tab, date range ...) and replace only `param`.
    """
    request = context.get('request')
    query = request.GET.copy() if request is not None else None
    if query is not None:
        query.pop(param, None)
        for key in TRANSIENT_PARAMS:
            query.pop(key, None)
    base = query.urlencode() if query else ''
    # Storefront pages are bilingual; the back office is English throughout
    # (request.LANGUAGE is only ever set for a customer's choice).
    language = getattr(request, 'LANGUAGE', 'en') if request is not None else 'en'
    labels = {
        key: translate(text, language)
        for key, text in (
            ('previous', 'Previous'), ('next', 'Next'),
            ('showing', 'Showing'), ('of', 'of'),
        )
    }
    return {
        'labels': labels,
        'page_obj': page_obj,
        'noun_label': noun if page_obj.paginator.count == 1 else (plural or _plural(noun)),
        'param': param,
        'base_query': f'{base}&' if base else '',
        'window': page_window(page_obj),
    }
