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
    return redirect('login')


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
    return render(request, 'accounts/owner_dashboard.html')


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
    return render(request, 'accounts/customer_dashboard.html')