from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import User


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

    from reports.views import reports_dashboard
    return reports_dashboard(request)


@login_required(login_url='login')
def manager_dashboard(request):
    if request.user.role != User.Role.BRANCH_MANAGER:
        messages.error(request, 'Access denied.')
        return redirect('login')
    return render(request, 'accounts/manager_dashboard.html')


@login_required(login_url='login')
def staff_dashboard(request):
    if request.user.role != User.Role.STAFF:
        messages.error(request, 'Access denied.')
        return redirect('login')
    return render(request, 'accounts/staff_dashboard.html')


@login_required(login_url='login')
def supplier_dashboard(request):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('login')
    return render(request, 'accounts/supplier_dashboard.html')


@login_required(login_url='login')
def customer_dashboard(request):
    if request.user.role != User.Role.CUSTOMER:
        messages.error(request, 'Access denied.')
        return redirect('login')
    
    from inventory.models import Product, ProductCategory
    from branches.models import Branch
    products = Product.objects.all()[:8]
    branches = Branch.objects.all()
    categories = ProductCategory.objects.all()
    return render(request, 'accounts/customer_dashboard.html', {
        'products': products,
        'branches': branches,
        'categories': categories
    })

from inventory.models import Product, ProductCategory
from branches.models import Branch


def home_view(request):
    products = Product.objects.all()[:8]
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

        from .models import CustomerProfile
        CustomerProfile.objects.create(
            user=user,
            first_name=first_name,
            last_name=last_name,
            contact_number=contact_number,
            address=address,
        )

        login(request, user)
        messages.success(request, 'Account created successfully. Welcome!')
        return redirect('customer_dashboard')

    return render(request, 'accounts/register.html')