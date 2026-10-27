"""
The PDF and Excel downloads behind each report page's Export PDF / Export Excel
buttons.

Same access rule as the report pages (management_report_required: Owner and
Branch Manager), the same branch scoping, and the same date range the page was
showing: the buttons carry the page's own `export_query`. All the work happens
in reports.exports; this module only resolves the request's filters.
"""
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.text import slugify

from core.permissions import management_report_required
from core.timebounds import range_from_request, range_problem
from reports import exports

# Where a failed export sends the reader back to: the report they came from.
_REPORT_PAGE = {
    'sales': 'sales_report',
    'billing': 'billing_report',
    'inventory': 'inventory_report',
    'procurement': 'procurement_report',
    'profitability': 'profitability_report',
}


def _document_or_redirect(request, kind):
    """(document, None) for a valid request, (None, redirect) when the range is
    unusable. Raises Http404 for a report that does not exist."""
    if kind not in exports.REPORT_KINDS:
        raise Http404(f'Unknown report "{kind}".')

    start = end = None
    active_range = '30'
    if kind != 'inventory':
        # A range the page would quietly replace with the last 30 days is a
        # mistake worth stopping on: this is a file someone files away, and its
        # header must describe the period the figures really cover.
        problem = range_problem(request)
        if problem:
            messages.error(request, f'Could not export: {problem}')
            return None, redirect(_REPORT_PAGE[kind])
        start, end, active_range = range_from_request(request)

    document = exports.build_document(
        request.user, kind, start, end, active_range=active_range,
        branch_id=request.GET.get('branch'),
    )
    return document, None


def _filename(document, extension):
    # slugify: a branch name is free text, and a quote or a non-latin-1 character
    # in a Content-Disposition value breaks the header (or raises).
    scope = slugify(document.branch_label) or 'report'
    return f'gerlyn-{document.kind}-report-{scope}-{timezone.localdate()}.{extension}'


@management_report_required
def export_pdf(request, kind):
    document, problem = _document_or_redirect(request, kind)
    if problem:
        return problem
    try:
        content = exports.render_pdf(document)
    except (exports.ExportTooLarge, RuntimeError) as exc:
        messages.error(request, str(exc))
        return redirect(_REPORT_PAGE[kind])

    response = HttpResponse(content, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{_filename(document, "pdf")}"'
    return response


@management_report_required
def export_xlsx(request, kind):
    document, problem = _document_or_redirect(request, kind)
    if problem:
        return problem

    response = HttpResponse(exports.render_xlsx(document), content_type=exports.XLSX_CONTENT_TYPE)
    response['Content-Disposition'] = f'attachment; filename="{_filename(document, "xlsx")}"'
    return response
