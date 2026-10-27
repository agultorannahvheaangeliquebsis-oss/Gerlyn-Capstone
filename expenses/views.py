"""Expenses: list, add, edit, delete, and the receipt photo.

Owner and Branch Manager only (core.permissions.role_required, then the
module/action layer in accounts.middleware). All the rules -- which branch, what
is valid, the audit trail -- are in expenses.services.
"""
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_GET, require_POST

from branches.models import Branch
from core.pagination import paginate
from core.permissions import BRANCH_MANAGER, OWNER, role_required
from core.scoping import branch_for_user
from core.timebounds import local_range_bounds, range_from_request
from reports.views import _range_carry

from . import services
from .models import Expense
from .receipts import MAX_RECEIPT_MB, content_type_for
from .services import ExpenseError

ZERO = Decimal('0.00')


def _filtered(request):
    """(queryset, range_start, range_end, active_range, branch_filter, category)
    for the list page and nothing else -- one place so the rows, the totals and
    the pager cannot disagree."""
    user = request.user
    branch_filter = request.GET.get('branch', '') if user.role == OWNER else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''
    category = request.GET.get('category', '')
    if category not in Expense.Category.values:
        category = ''

    range_start, range_end, active_range = range_from_request(request)
    qs = services.visible_expenses(user, branch_id=branch_filter)
    qs = qs.filter(date__lte=range_end)
    if range_start is not None:
        qs = qs.filter(date__gte=range_start)
    if category:
        qs = qs.filter(category=category)
    return qs, range_start, range_end, active_range, branch_filter, category


@role_required(OWNER, BRANCH_MANAGER)
def expense_list(request):
    user = request.user
    qs, range_start, range_end, active_range, branch_filter, category = _filtered(request)

    totals = qs.aggregate(total=Coalesce(Sum('amount'), ZERO), n=Count('pk'))
    by_category = list(
        qs.order_by().values('category').annotate(total=Sum('amount'), n=Count('pk')).order_by('-total')
    )
    top_category = by_category[0] if by_category else None

    page = paginate(request, qs)
    show_branch_filter = user.role == OWNER

    carry = [('branch', branch_filter), ('category', category)]
    return render(request, 'expenses/expense_list.html', {
        'expenses': page.object_list,
        'page_obj': page,
        'total_amount': totals['total'],
        'total_count': totals['n'],
        'top_category': top_category,
        'by_category': by_category,
        'categories': Expense.Category.choices,
        'category_filter': category,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'branch_filter': branch_filter,
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        'user_branch': branch_for_user(user),
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        'daterange_carry': carry,
        'branch_switch_carry': _range_carry(active_range, range_start, range_end)
        + ([('category', category)] if category else []),
        'active_page': 'expenses',
    })


def _form_context(request, *, mode, values, errors=(), expense=None):
    return {
        'mode': mode,
        'expense': expense,
        'values': values,
        'errors': list(errors),
        'categories': Expense.Category.choices,
        'branches': services.managed_branches(request.user),
        'own_branch': branch_for_user(request.user) if request.user.role == BRANCH_MANAGER else None,
        'today': timezone.localdate(),
        'max_receipt_mb': MAX_RECEIPT_MB,
        'active_page': 'expenses',
    }


def _posted_values(post):
    return {
        'branch': post.get('branch', ''), 'category': post.get('category', ''),
        'amount': post.get('amount', ''), 'date': post.get('date', ''),
        'description': post.get('description', ''),
    }


@role_required(OWNER, BRANCH_MANAGER)
def expense_add(request):
    if request.method == 'POST':
        cleaned, errors = services.clean_expense(request.user, request.POST, request.FILES)
        if not errors:
            expense = services.create_expense(request.user, cleaned)
            messages.success(
                request,
                f'Recorded {expense.category} expense of ₱{expense.amount:,.2f} for '
                f'{expense.branch.branch_name}.',
            )
            return redirect('expense_list')
        return render(request, 'expenses/expense_form.html', _form_context(
            request, mode='add', values=_posted_values(request.POST), errors=errors,
        ))

    own = branch_for_user(request.user) if request.user.role == BRANCH_MANAGER else None
    values = {
        'branch': own.pk if own else '', 'category': '', 'amount': '',
        'date': timezone.localdate().isoformat(), 'description': '',
    }
    return render(request, 'expenses/expense_form.html', _form_context(request, mode='add', values=values))


@role_required(OWNER, BRANCH_MANAGER)
def expense_edit(request, pk):
    expense = services.get_manageable_expense(request.user, pk)
    if expense is None:
        raise Http404

    if request.method == 'POST':
        cleaned, errors = services.clean_expense(
            request.user, request.POST, request.FILES, instance=expense,
        )
        if not errors:
            updated = services.update_expense(request.user, pk, cleaned)
            if updated is None:
                raise Http404
            messages.success(request, 'Expense updated.')
            return redirect('expense_list')
        return render(request, 'expenses/expense_form.html', _form_context(
            request, mode='edit', values=_posted_values(request.POST), errors=errors, expense=expense,
        ))

    values = {
        'branch': expense.branch_id, 'category': expense.category,
        'amount': expense.amount, 'date': expense.date.isoformat(),
        'description': expense.description,
    }
    return render(request, 'expenses/expense_form.html', _form_context(
        request, mode='edit', values=values, expense=expense,
    ))


@role_required(OWNER, BRANCH_MANAGER)
@require_POST
def expense_delete(request, pk):
    if not services.delete_expense(request.user, pk):
        raise Http404
    messages.success(request, 'Expense deleted.')
    return redirect('expense_list')


@role_required(OWNER, BRANCH_MANAGER)
@require_GET
def expense_receipt(request, pk):
    """Stream the receipt photo. 404 for a missing one, another branch's
    expense, or anything stored that is not a JPG/PNG."""
    expense = services.get_manageable_expense(request.user, pk)
    if expense is None or not expense.receipt:
        raise Http404
    content_type = content_type_for(expense.receipt.name)
    if not content_type:
        raise Http404
    try:
        handle = expense.receipt.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    response = FileResponse(handle, content_type=content_type)
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response
