"""The Profitability report page.

Owner and Branch Manager only (core.permissions.management_report_required),
like every other report. All the arithmetic is in reports.profitability; this
module resolves the filters and hands the result to the template.
"""
from django.contrib import messages
from django.shortcuts import render
from django.utils.http import urlencode

from branches.models import Branch
from core.permissions import management_report_required
from core.scoping import branch_for_user, branch_ids_for_user
from core.timebounds import range_from_request, range_problem
from reports.profit_chart import build_trend_chart
from reports.profitability import build_profitability, data_notes
from reports.views import _range_carry


@management_report_required
def profitability_report(request):
    user = request.user
    user_branch = branch_for_user(user)
    show_branch_filter = branch_ids_for_user(user) is None   # Owner only
    branch_filter = request.GET.get('branch', '') if show_branch_filter else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''

    # Same behaviour as the Sales report: a range that cannot be honoured falls
    # back to the last 30 days, so say so rather than let the figures pass for
    # the period that was asked for.
    problem = range_problem(request)
    if problem:
        messages.warning(request, f'{problem} Showing the last 30 days instead.')
    range_start, range_end, active_range = range_from_request(request)

    result = build_profitability(user, range_start, range_end, branch_id=branch_filter)

    return render(request, 'reports/profitability.html', {
        'result': result,
        'totals': result.totals,
        'notes': data_notes(result),
        'chart': build_trend_chart(result.months),
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        'user_branch': user_branch,
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        'daterange_carry': [('branch', branch_filter)],
        'branch_switch_carry': _range_carry(active_range, range_start, range_end),
        # So the PDF / Excel buttons export the period and branch on screen.
        'export_query': urlencode(
            _range_carry(active_range, range_start, range_end)
            + ([('branch', branch_filter)] if branch_filter else [])
        ),
        'can_edit_products': user.role == 'OWNER',
        'active_page': 'reports',
    })
