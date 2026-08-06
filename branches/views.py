"""Branch Management — Owner-only administration of store branches."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render

from accounts.models import BranchManagerProfile, User
from inventory.models import Inventory, Warehouse

from .models import Branch


@login_required(login_url='login')
def branch_list(request):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage branches.')
        return redirect('accounts:home')

    branches = Branch.objects.annotate(
        staff_count=Count('staff', distinct=True),
        product_count=Count('inventory', distinct=True),
    ).order_by('branch_name')

    rows = []
    for b in branches:
        manager = BranchManagerProfile.objects.filter(branch=b).first()
        rows.append({
            'branch': b,
            'manager': manager.manager_name if manager else None,
            'staff_count': b.staff_count,
            'product_count': b.product_count,
        })

    return render(request, 'branches/branch_list.html', {'rows': rows})


@login_required(login_url='login')
def branch_form(request, pk=None):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage branches.')
        return redirect('accounts:home')

    branch = get_object_or_404(Branch, pk=pk) if pk else None

    if request.method == 'POST':
        name = request.POST.get('branch_name', '').strip()
        location = request.POST.get('location', '').strip()

        if not name:
            messages.error(request, 'Branch name is required.')
            return redirect(request.path)

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
        return redirect('branch_list')

    return render(request, 'branches/branch_form.html', {'branch': branch})


@login_required(login_url='login')
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
        messages.success(request, f'Branch "{name}" deleted.')
        return redirect('branch_list')

    return render(request, 'branches/branch_confirm_delete.html', {
        'branch': branch,
        'stock_on_hand': stock_on_hand,
        'staff_count': staff_count,
    })
