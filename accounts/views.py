from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import (
    BranchManagerProfile, OwnerProfile, StaffProfile, User,
)


def login_view(request):
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect_by_role(user)
        else:
            messages.error(request, 'Invalid username or password.')

    return render(request, 'accounts/login.html')


def logout_view(request):
    logout(request)
    return redirect('/')


def redirect_by_role(user):
    role_redirects = {
        User.Role.OWNER: 'owner_dashboard',
        User.Role.BRANCH_MANAGER: 'manager_dashboard',
        User.Role.STAFF: 'staff_dashboard',
        User.Role.SUPPLIER: 'supplier_dashboard',
        User.Role.CUSTOMER: 'customer_dashboard',
    }
    return redirect(role_redirects.get(user.role, 'login'))


@login_required(login_url='login')
def owner_dashboard(request):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Access denied.')
        return redirect('login')

    from reports.views import operational_dashboard
    return operational_dashboard(request)


@login_required(login_url='login')
def manager_dashboard(request):
    if request.user.role != User.Role.BRANCH_MANAGER:
        messages.error(request, 'Access denied.')
        return redirect('login')
    # Same Dashboard as the Owner — the view scopes it to the manager's branch.
    from reports.views import operational_dashboard
    return operational_dashboard(request)


@login_required(login_url='login')
def staff_dashboard(request):
    if request.user.role != User.Role.STAFF:
        messages.error(request, 'Access denied.')
        return redirect('login')
    from reports.views import operational_dashboard
    return operational_dashboard(request)


@login_required(login_url='login')
def supplier_dashboard(request):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('login')
    # The supplier's real home is their order portal.
    return redirect('supplier_portal')


@login_required(login_url='login')
def customer_dashboard(request):
    if request.user.role != User.Role.CUSTOMER:
        messages.error(request, 'Access denied.')
        return redirect('login')
    
    from inventory.models import Product, ProductCategory
    from branches.models import Branch
    products = Product.objects.prefetch_related('images')[:8]
    branches = Branch.objects.all()
    categories = ProductCategory.objects.all()
    return render(request, 'accounts/customer_dashboard.html', {
        'products': products,
        'branches': branches,
        'categories': categories
    })

from inventory.models import Product, ProductCategory
from branches.models import Branch


# =============================================================================
# USER MANAGEMENT — RBAC administration (Owner only).
# Supports the documented information-governance objective: roles are assigned
# explicitly, and every account is traceable to a branch.
# =============================================================================

INTERNAL_ROLE_CHOICES = [
    (User.Role.OWNER, 'Owner'),
    (User.Role.BRANCH_MANAGER, 'Branch Manager'),
    (User.Role.STAFF, 'Store Staff'),
]


def _profile_for(user):
    """The role-specific profile carrying the name/branch, if any."""
    return (
        getattr(user, 'manager_profile', None)
        or getattr(user, 'staff_profile', None)
        or getattr(user, 'owner_profile', None)
    )


@login_required(login_url='login')
def user_list(request):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('home')

    users = User.objects.exclude(role=User.Role.CUSTOMER).select_related(
        'manager_profile__branch', 'staff_profile__branch', 'owner_profile',
        'supplier_profile',
    ).order_by('role', 'username')

    search_query = request.GET.get('q', '')
    role_filter = request.GET.get('role', '')
    if search_query:
        users = users.filter(username__icontains=search_query)
    if role_filter:
        users = users.filter(role=role_filter)

    rows = []
    for u in users:
        profile = _profile_for(u)
        branch = getattr(profile, 'branch', None) if profile else None
        rows.append({
            'user': u,
            'full_name': u.get_full_name() or getattr(profile, 'manager_name', '')
                         or getattr(profile, 'staff_name', '') or u.username,
            'branch': branch.branch_name if branch else (
                'All Branches' if u.role == User.Role.OWNER else '—'
            ),
        })

    return render(request, 'accounts/user_list.html', {
        'rows': rows,
        'search_query': search_query,
        'role_filter': role_filter,
        'role_choices': User.Role.choices,
        'active_count': users.filter(is_active=True).count(),
    })


@login_required(login_url='login')
def user_form(request, pk=None):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('home')

    target = get_object_or_404(User, pk=pk) if pk else None

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        role = request.POST.get('role')
        branch_id = request.POST.get('branch') or None
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        password = request.POST.get('password', '').strip()

        if not username or role not in dict(INTERNAL_ROLE_CHOICES):
            messages.error(request, 'A username and a valid role are required.')
            return redirect(request.path)

        if target is None and User.objects.filter(username=username).exists():
            messages.error(request, f'Username "{username}" is already taken.')
            return redirect('user_add')

        if target is None:
            if not password:
                messages.error(request, 'A password is required for a new user.')
                return redirect('user_add')
            target = User.objects.create_user(
                username=username, email=request.POST.get('email', ''),
                password=password, role=role,
                first_name=first_name, last_name=last_name,
            )
        else:
            target.username = username
            target.email = request.POST.get('email', '')
            target.first_name = first_name
            target.last_name = last_name
            target.role = role
            if password:
                target.set_password(password)
            target.save()

        # Keep the role profile's branch in step with the assignment.
        full_name = f'{first_name} {last_name}'.strip() or username
        if role == User.Role.BRANCH_MANAGER:
            profile, _ = BranchManagerProfile.objects.get_or_create(user=target)
            profile.manager_name = full_name
            profile.branch_id = branch_id
            profile.save()
        elif role == User.Role.STAFF:
            profile, _ = StaffProfile.objects.get_or_create(user=target)
            profile.staff_name = full_name
            profile.branch_id = branch_id
            profile.save()
        elif role == User.Role.OWNER:
            profile, _ = OwnerProfile.objects.get_or_create(user=target)
            profile.owner_name = full_name
            profile.save()

        messages.success(request, f'User "{username}" saved.')
        return redirect('user_list')

    profile = _profile_for(target) if target else None
    return render(request, 'accounts/user_form.html', {
        'target': target,
        'profile': profile,
        'current_branch_id': getattr(profile, 'branch_id', None) if profile else None,
        'branches': Branch.objects.all(),
        'role_choices': INTERNAL_ROLE_CHOICES,
    })


@login_required(login_url='login')
def user_toggle_active(request, pk):
    """Deactivate/reactivate an account without deleting its audit history."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('home')

    target = get_object_or_404(User, pk=pk)

    if target.pk == request.user.pk:
        messages.error(request, "You can't deactivate your own account.")
        return redirect('user_list')

    if request.method == 'POST':
        target.is_active = not target.is_active
        target.save()
        state = 'reactivated' if target.is_active else 'deactivated'
        messages.success(request, f'{target.username} {state}.')

    return redirect('user_list')


def home_view(request):
    products = Product.objects.prefetch_related('images')[:8]
    branches = Branch.objects.all()
    categories = ProductCategory.objects.all()
    return render(request, 'home.html', {'products': products, 'branches': branches, 'categories': categories})

def register_view(request):
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        password1 = request.POST.get('password1')
        password2 = request.POST.get('password2')
        first_name = request.POST.get('first_name')
        last_name = request.POST.get('last_name')
        contact_number = request.POST.get('contact_number')
        address = request.POST.get('address')

        if password1 != password2:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'accounts/register.html')

        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already taken.')
            return render(request, 'accounts/register.html')

        if User.objects.filter(email=email).exists():
            messages.error(request, 'Email already registered.')
            return render(request, 'accounts/register.html')

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password1,
            first_name=first_name,
            last_name=last_name,
            role=User.Role.CUSTOMER,
        )

        # create_user() above already triggered the post_save signal, which
        # auto-created a placeholder CustomerProfile — fill it in rather than
        # creating a second one (that collides on the OneToOne user_id).
        profile = user.customer_profile
        profile.first_name = first_name
        profile.last_name = last_name
        profile.contact_number = contact_number
        profile.address = address
        profile.save()

        login(request, user)
        messages.success(request, 'Account created successfully. Welcome!')
        return redirect('customer_dashboard')

    return render(request, 'accounts/register.html')