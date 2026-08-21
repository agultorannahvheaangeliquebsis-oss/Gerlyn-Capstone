"""Branch Management — Owner-only administration of store branches."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.models import BranchManagerProfile, User
from core.refdata import bust_branches_cache
from inventory.models import Inventory, Warehouse

from .models import Branch


def _branch_list_context():
    branches = Branch.objects.annotate(
        staff_count=Count('staff', distinct=True),
        product_count=Count('inventory', distinct=True),
    ).order_by('branch_name')

    # One query for every branch's manager instead of one query per branch.
    manager_by_branch_id = {}
    for m in BranchManagerProfile.objects.filter(branch__in=branches):
        manager_by_branch_id.setdefault(m.branch_id, m.manager_name)

    rows = []
    for b in branches:
        rows.append({
            'branch': b,
            'manager': manager_by_branch_id.get(b.pk),
            'staff_count': b.staff_count,
            'product_count': b.product_count,
        })

    return {'rows': rows}


@login_required(login_url='accounts:login')
def branch_list(request):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage branches.')
        return redirect('accounts:home')

    context = _branch_list_context()

    edit_pk = request.GET.get('edit')
    if edit_pk:
        branch = Branch.objects.filter(pk=edit_pk).first()
        if branch:
            context.update({'show_edit_modal': True, 'edit_branch': branch})

    return render(request, 'branches/branch_list.html', context)


@login_required(login_url='accounts:login')
def branch_form(request, pk=None):
    """Add or edit a branch (Owner only). Both live in a modal on the branch
    list, so this is the POST target and renders no page of its own. Only the
    GET branch and the error path changed — the create/update below, and the
    warehouse that comes with a new branch, are untouched."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage branches.')
        return redirect('accounts:home')

    branch = get_object_or_404(Branch, pk=pk) if pk else None

    if request.method != 'POST':
        flag = f'edit={pk}' if branch else 'add=1'
        return redirect(f"{reverse('branch_list')}?{flag}")

    name = request.POST.get('branch_name', '').strip()
    location = request.POST.get('location', '').strip()

    errors = []
    if not name:
        errors.append('Branch name is required.')

    if not errors:
        if branch is None:
            branch = Branch.objects.create(
                branch_name=name, location=location, owner=request.user,
            )
            # Every branch needs a warehouse to receive/withdraw stock.
            Warehouse.objects.get_or_create(
                branch=branch, defaults={'name': f'{name} Warehouse'},
            )
            messages.success(request, f'Branch "{name}" created with its warehouse.')
        else:
            branch.branch_name = name
            branch.location = location
            branch.save()
            messages.success(request, f'Branch "{name}" updated.')
        bust_branches_cache()
        return redirect('branch_list')

    context = _branch_list_context()
    if pk:
        context.update({
            'show_edit_modal': True,
            'edit_branch': branch,
            'edit_errors': errors,
            'edit_form_data': request.POST,
        })
    else:
        context.update({
            'show_add_modal': True,
            'add_errors': errors,
            'add_form_data': request.POST,
        })
    return render(request, 'branches/branch_list.html', context)


@login_required(login_url='accounts:login')
def branch_delete(request, pk):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can delete branches.')
        return redirect('accounts:home')

    branch = get_object_or_404(Branch, pk=pk)
    stock_on_hand = Inventory.objects.filter(branch=branch).aggregate(
        total=Sum('quantity_on_hand')
    )['total'] or 0
    staff_count = User.objects.filter(staff_profile__branch=branch).count()

    if request.method == 'POST':
        if stock_on_hand or staff_count:
            messages.error(
                request,
                'Cannot delete a branch that still holds stock or has staff assigned.'
            )
            return redirect('branch_list')
        name = branch.branch_name
        branch.delete()
        bust_branches_cache()
        messages.success(request, f'Branch "{name}" deleted.')
        return redirect('branch_list')

    return render(request, 'branches/branch_confirm_delete.html', {
        'branch': branch,
        'stock_on_hand': stock_on_hand,
        'staff_count': staff_count,
    })
