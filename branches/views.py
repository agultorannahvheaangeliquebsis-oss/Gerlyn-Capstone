"""Branch Management — Owner-only administration of store branches."""
from django.contrib import messages
from django.contrib.auth import password_validation
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.models import BranchManagerProfile, User
from core.module_permissions import ALL_MODULE_CODES
from core.refdata import bust_branches_cache
from inventory.models import Warehouse

from .models import Branch
from .services import BranchDeletionBlocked, delete_branch_if_safe


def _branch_list_context():
    branches = Branch.objects.annotate(
        staff_count=Count('staff', distinct=True),
        product_count=Count('inventory', distinct=True),
    ).order_by('branch_name')

    # One query for every branch's manager instead of one query per branch.
    manager_by_branch_id = {}
    for m in BranchManagerProfile.objects.filter(branch__in=branches):
        manager_by_branch_id.setdefault(m.branch_id, m)

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

    assign_pk = request.GET.get('assign')
    if assign_pk:
        branch = Branch.objects.filter(pk=assign_pk).first()
        if branch:
            context.update(_assign_manager_context(branch))

    return render(request, 'branches/branch_list.html', context)


def _assign_manager_context(branch, form_data=None):
    current = BranchManagerProfile.objects.filter(branch=branch).first()
    available = BranchManagerProfile.objects.filter(branch__isnull=True).exclude(
        pk=current.pk if current else None
    ).order_by('manager_name')
    # What the <select> should show pre-picked: the just-submitted (and
    # rejected) choice, else today's actual manager, else "— Unassign —".
    # Computed here rather than in the template so a branch with no current
    # manager doesn't stringify None into the literal text "None".
    if form_data is not None:
        default_choice = form_data.get('manager_choice', '')
    elif current:
        default_choice = str(current.user_id)
    else:
        default_choice = ''
    return {
        'show_assign_modal': True,
        'assign_branch': branch,
        'assign_current_manager': current,
        'assign_available_managers': available,
        'assign_default_choice': default_choice,
    }


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

    from accounts.geocoding import parse_coordinates
    try:
        latitude, longitude = parse_coordinates(
            request.POST.get('latitude'), request.POST.get('longitude')
        )
    except ValueError as exc:
        latitude = longitude = None
        coordinate_error = str(exc)
    else:
        coordinate_error = None

    location_fields = {
        'region': request.POST.get('region', '').strip(),
        'province': request.POST.get('province', '').strip(),
        'city': request.POST.get('city', '').strip(),
        'barangay': request.POST.get('barangay', '').strip(),
        'street_detail': request.POST.get('street_detail', '').strip(),
        'latitude': latitude,
        'longitude': longitude,
        'is_active': (
            request.POST.get('is_active') == 'on'
            if branch is not None or 'is_active' in request.POST else True
        ),
    }

    errors = []
    if not name:
        errors.append('Branch name is required.')
    if coordinate_error:
        errors.append(coordinate_error)

    if not errors:
        if branch is None:
            branch = Branch.objects.create(
                branch_name=name, location=location, owner=request.user,
                **location_fields,
            )
            # Every branch needs a warehouse to receive/withdraw stock.
            Warehouse.objects.get_or_create(
                branch=branch, defaults={'name': f'{name} Warehouse'},
            )
            messages.success(request, f'Branch "{name}" created with its warehouse.')
        else:
            branch.branch_name = name
            branch.location = location
            for field, value in location_fields.items():
                setattr(branch, field, value)
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

    if request.method == 'POST':
        try:
            name = delete_branch_if_safe(branch.pk)
        except BranchDeletionBlocked as exc:
            messages.error(request, str(exc))
            return redirect('branch_list')
        messages.success(request, f'Branch "{name}" deleted.')
        return redirect('branch_list')

    # The confirm step is a modal on the branch list now (confirm-dialog.js off
    # the row's Delete button). A direct GET here — bookmark, back button —
    # just goes to the list.
    return redirect('branch_list')


@login_required(login_url='accounts:login')
def branch_assign_manager(request, pk):
    """Assign, change, or clear a branch's manager from a modal on the branch
    list — pick an unassigned Branch Manager account, or create a new one
    inline, without leaving this page."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage branches.')
        return redirect('accounts:home')

    branch = get_object_or_404(Branch, pk=pk)

    if request.method != 'POST':
        return redirect(f"{reverse('branch_list')}?assign={pk}")

    choice = request.POST.get('manager_choice', '')
    current = BranchManagerProfile.objects.filter(branch=branch).first()
    errors = []

    if choice == '':
        if current:
            current.branch = None
            current.save(update_fields=['branch'])
        messages.success(request, f'Manager unassigned from "{branch.branch_name}".')
        bust_branches_cache()
        return redirect('branch_list')

    elif choice == '__new__':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '').strip()
        contact_number = request.POST.get('contact_number', '').strip()

        if not username:
            errors.append('Username is required for a new manager.')
        elif User.objects.filter(username=username).exists():
            errors.append(f'Username "{username}" is already taken.')
        if not password:
            errors.append('Password is required for a new manager.')
        else:
            try:
                password_validation.validate_password(password)
            except ValidationError as e:
                errors.extend(e.messages)
        if not contact_number:
            errors.append('Contact number is required for a new manager.')

        if not errors:
            target = User.objects.create_user(
                username=username, password=password, role=User.Role.BRANCH_MANAGER,
                first_name=first_name, last_name=last_name,
            )
            # Same default as a freshly-opened Add User modal: the Owner is
            # setting this account up from scratch, so it starts with every
            # module rather than an empty grant that would lock them out.
            target.allowed_modules = ALL_MODULE_CODES
            target.save(update_fields=['allowed_modules'])

            if current:
                current.branch = None
                current.save(update_fields=['branch'])

            profile, _ = BranchManagerProfile.objects.get_or_create(user=target)
            profile.manager_name = f'{first_name} {last_name}'.strip() or username
            profile.contact_number = contact_number
            profile.branch = branch
            profile.save()

            messages.success(request, f'"{profile.manager_name}" assigned as manager of "{branch.branch_name}".')
            bust_branches_cache()
            return redirect('branch_list')

    else:
        target_profile = (
            BranchManagerProfile.objects.filter(user_id=choice).first()
            if choice.isdigit() else None
        )
        if not target_profile:
            errors.append('Selected manager could not be found.')
        elif target_profile.branch_id not in (None, branch.pk):
            errors.append(f'"{target_profile.manager_name}" is already assigned to another branch.')
        else:
            if current and current.pk != target_profile.pk:
                current.branch = None
                current.save(update_fields=['branch'])
            target_profile.branch = branch
            target_profile.save(update_fields=['branch'])
            messages.success(request, f'"{target_profile.manager_name}" assigned as manager of "{branch.branch_name}".')
            bust_branches_cache()
            return redirect('branch_list')

    context = _branch_list_context()
    context.update(_assign_manager_context(branch, form_data=request.POST))
    context.update({'assign_errors': errors, 'assign_form_data': request.POST})
    return render(request, 'branches/branch_list.html', context)
