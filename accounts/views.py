from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout, password_validation, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from .models import (
    BranchManagerProfile, OwnerProfile, StaffProfile, User,
    CustomerProfile, SupplierProfile, Notification,
)


def login_view(request):
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    error = None
    username = ''

    if request.method == 'POST':
        username = request.POST.get('username', '')
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect_by_role(user)
        else:
            error = 'Invalid username or password.'

    return render(request, 'accounts/login.html', {'error': error, 'username': username})


def logout_view(request):
    logout(request)
    return redirect('/')


def redirect_by_role(user):
    role_redirects = {
        User.Role.OWNER: 'accounts:owner_dashboard',
        User.Role.BRANCH_MANAGER: 'accounts:manager_dashboard',
        User.Role.STAFF: 'accounts:staff_dashboard',
        User.Role.SUPPLIER: 'accounts:supplier_dashboard',
        User.Role.CUSTOMER: 'accounts:customer_dashboard',
    }
    return redirect(role_redirects.get(user.role, 'accounts:login'))


@login_required(login_url='accounts:login')
def owner_dashboard(request):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:login')

    from reports.views import operational_dashboard
    return operational_dashboard(request)


@login_required(login_url='accounts:login')
def manager_dashboard(request):
    if request.user.role != User.Role.BRANCH_MANAGER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:login')
    # Same Dashboard as the Owner — the view scopes it to the manager's branch.
    from reports.views import operational_dashboard
    return operational_dashboard(request)


@login_required(login_url='accounts:login')
def staff_dashboard(request):
    """
    Staff's own dashboard — deliberately separate from the Owner/Manager
    operational_dashboard (reports.views): staff get task-oriented shortcuts
    and their single branch's stock, not cross-branch sales/revenue figures
    or purchase-order authoring, which are outside their role.
    """
    if request.user.role != User.Role.STAFF:
        messages.error(request, 'Access denied.')
        return redirect('accounts:login')

    from django.utils import timezone
    from core.scoping import branch_ids_for_user
    from inventory.models import Inventory, StockTransfer
    from inventory.alerts import low_stock_alerts
    from procurement.models import Procurement

    profile = getattr(request.user, 'staff_profile', None)
    branch = profile.branch if profile else None

    if not branch:
        messages.error(
            request,
            'Your account is not assigned to a branch yet. Contact your manager or the Owner.'
        )
        return render(request, 'accounts/staff_dashboard.html', {
            'branch': None,
            'active_page': 'dashboard',
        })

    branch_ids = branch_ids_for_user(request.user)

    pending_deliveries = Procurement.objects.select_related(
        'supplier__supplier_profile'
    ).filter(branch=branch, status=Procurement.Status.ORDERED).order_by('delivery_date')

    pending_receipts = StockTransfer.objects.select_related('source_branch').filter(
        destination_branch=branch, status=StockTransfer.Status.RELEASED
    ).order_by('-created_at')

    restock = low_stock_alerts(branch_ids)

    stock = Inventory.objects.select_related(
        'product', 'product__category'
    ).filter(branch=branch)

    return render(request, 'accounts/staff_dashboard.html', {
        'branch': branch,
        'pending_deliveries': pending_deliveries[:5],
        'pending_deliveries_count': pending_deliveries.count(),
        'pending_receipts': pending_receipts[:5],
        'pending_receipts_count': pending_receipts.count(),
        'restock': restock[:6],
        'low_stock_count': len(restock),
        'task_count': pending_deliveries.count() + pending_receipts.count() + len(restock),
        'total_products': stock.count(),
        'stock_preview': stock.order_by('quantity_on_hand')[:8],
        'today': timezone.localtime().date(),
        'active_page': 'dashboard',
    })


@login_required(login_url='accounts:login')
def supplier_dashboard(request):
    if request.user.role != User.Role.SUPPLIER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:login')
    # The supplier's real home is their order portal.
    return redirect('supplier_portal')


@login_required(login_url='accounts:login')
def customer_dashboard(request):
    if request.user.role != User.Role.CUSTOMER:
        messages.error(request, 'Access denied.')
        return redirect('accounts:login')
    
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


@login_required(login_url='accounts:login')
def user_list(request):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('accounts:home')

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


@login_required(login_url='accounts:login')
def user_form(request, pk=None):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('accounts:home')

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
            return redirect('accounts:user_add')

        if target is None:
            if not password:
                messages.error(request, 'A password is required for a new user.')
                return redirect('accounts:user_add')
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
        return redirect('accounts:user_list')

    profile = _profile_for(target) if target else None
    return render(request, 'accounts/user_form.html', {
        'target': target,
        'profile': profile,
        'current_branch_id': getattr(profile, 'branch_id', None) if profile else None,
        'branches': Branch.objects.all(),
        'role_choices': INTERNAL_ROLE_CHOICES,
    })


@login_required(login_url='accounts:login')
def user_toggle_active(request, pk):
    """Deactivate/reactivate an account without deleting its audit history."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('accounts:home')

    target = get_object_or_404(User, pk=pk)

    if target.pk == request.user.pk:
        messages.error(request, "You can't deactivate your own account.")
        return redirect('accounts:user_list')

    if request.method == 'POST':
        target.is_active = not target.is_active
        target.save()
        state = 'reactivated' if target.is_active else 'deactivated'
        messages.success(request, f'{target.username} {state}.')

    return redirect('accounts:user_list')


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
        address = request.POST.get('address', '').strip()

        if password1 != password2:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'accounts/register.html')

        try:
            password_validation.validate_password(password1)
        except ValidationError as e:
            for error in e.messages:
                messages.error(request, error)
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

        if address:
            picker_fields = _address_fields_from_post(request)
            picker_fields['text'] = address
            Address.objects.create(user=user, is_default=True, **picker_fields)

        login(request, user)
        messages.success(request, 'Account created successfully. Welcome!')
        return redirect('accounts:customer_dashboard')

    return render(request, 'accounts/register.html')

# ============================================================
# PROFILE SYSTEM — add to bottom of accounts/views.py
# ============================================================
from django.conf import settings
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import VerificationToken, Address, NotificationPreference, UserSession
from .services import (
    send_verification_email, send_email_change_verification,
    send_password_change_verification, revoke_other_sessions,
    send_account_deactivation_verification, deactivate_account,
)


def _validate_avatar(file):
    """Validates file size and real image type using Pillow."""
    from PIL import Image
    max_bytes = settings.AVATAR_MAX_MB * 1024 * 1024
    if file.size > max_bytes:
        raise ValidationError(f"Image must be under {settings.AVATAR_MAX_MB} MB.")
    try:
        file.seek(0)
        img = Image.open(file)
        img.verify()          # Confirms it is a real image, not a renamed file
        file.seek(0)
        if img.format.lower() not in ('jpeg', 'png', 'webp'):
            raise ValidationError("Only JPEG, PNG, and WEBP images are allowed.")
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("The uploaded file is not a valid image.")
    finally:
        file.seek(0)


def _profile_context(user):
    """Builds the context dict the profile template expects."""
    role_profile = user.get_role_profile()
    contact_number = getattr(role_profile, 'contact_number', '')

    return {
        'username'          : user.username,
        'first_name'        : user.first_name,
        'last_name'         : user.last_name,
        'full_name'         : user.get_full_name(),
        'initials'          : user.get_initials(),
        'email'             : user.email,
        'is_email_verified' : user.is_email_verified,
        'pending_email'     : user.pending_email,
        'avatar_url'        : user.avatar.url if user.avatar else None,
        'contact_number'    : contact_number,
        'role'              : user.get_role_display(),
        'two_factor_enabled': user.two_factor_enabled,
        'default_address'   : user.addresses.filter(is_default=True).first(),
        'active_page'       : 'profile',
    }


@login_required(login_url='accounts:login')
def profile_view(request):
    return render(request, 'accounts/profile.html', _profile_context(request.user))


@login_required(login_url='accounts:login')
@require_POST
def update_profile(request):
    user   = request.user
    errors = {}

    new_username   = request.POST.get('username', '').strip()
    new_first_name = request.POST.get('first_name', '').strip()
    new_last_name  = request.POST.get('last_name', '').strip()
    new_email      = request.POST.get('email', '').strip().lower()
    new_phone      = request.POST.get('contact_number', '').strip()

    # --- Username uniqueness (exclude self) ---
    if new_username and new_username != user.username:
        if User.objects.filter(username__iexact=new_username).exclude(pk=user.pk).exists():
            errors['username'] = "That username is already taken."
        else:
            user.username = new_username

    # --- Email change flow ---
    if new_email and new_email != user.email:
        email_taken = (
            User.objects.filter(email=new_email).exclude(pk=user.pk).exists()
            or User.objects.filter(pending_email=new_email).exclude(pk=user.pk).exists()
        )
        if email_taken:
            errors['email'] = "That email is already associated with another account."
        else:
            try:
                send_email_change_verification(user, new_email, request)
                messages.info(
                    request,
                    f"A verification link has been sent to {new_email}. "
                    f"Your email won't change until you click it."
                )
            except Exception:
                errors['email'] = "Failed to send verification email. Please try again."

    # --- Name fields (no uniqueness constraint) ---
    if new_first_name:
        user.first_name = new_first_name
    if new_last_name:
        user.last_name = new_last_name

    # --- Contact number (update on role profile) ---
    if new_phone:
        role_profile = user.get_role_profile()
        if role_profile and hasattr(role_profile, 'contact_number'):
            # Check uniqueness across all profile tables
            phone_taken = any([
                BranchManagerProfile.objects.filter(contact_number=new_phone)
                    .exclude(user=user).exists(),
                StaffProfile.objects.filter(contact_number=new_phone)
                    .exclude(user=user).exists(),
                CustomerProfile.objects.filter(contact_number=new_phone)
                    .exclude(user=user).exists(),
                OwnerProfile.objects.filter(contact_number=new_phone)
                    .exclude(user=user).exists(),
                SupplierProfile.objects.filter(contact_number=new_phone)
                    .exclude(user=user).exists(),
            ])
            if phone_taken:
                errors['contact_number'] = "That contact number is already in use."
            else:
                role_profile.contact_number = new_phone
                role_profile.save(update_fields=['contact_number'])

    if errors:
        context = {**_profile_context(user), 'errors': errors}
        return render(request, 'accounts/profile.html', context)

    user.save()
    messages.success(request, "Profile updated.")
    return redirect('accounts:profile')


@login_required(login_url='accounts:login')
@require_POST
def update_avatar(request):
    user = request.user
    file = request.FILES.get('avatar')

    if not file:
        messages.error(request, "No file was uploaded.")
        return redirect('accounts:update_profile')

    try:
        _validate_avatar(file)
    except ValidationError as e:
        messages.error(request, e.message)
        return redirect('accounts:update_profile')

    # Delete old file from disk before saving new one
    if user.avatar:
        user.avatar.delete(save=False)

    user.avatar = file
    user.save(update_fields=['avatar'])
    messages.success(request, "Profile picture updated.")
    return redirect('accounts:profile')


@login_required(login_url='accounts:login')
def verify_pending(request):
    """Page shown after registration asking user to check their email."""
    return render(request, 'accounts/verify_pending.html')


def verify_email(request, token):
    """Handles the initial registration verification link."""
    try:
        token_obj = VerificationToken.objects.select_related('user').get(
            token=token, purpose=VerificationToken.PURPOSE_EMAIL_VERIFY
        )
    except VerificationToken.DoesNotExist:
        messages.error(request, "Invalid verification link.")
        return redirect('accounts:login')

    if not token_obj.is_valid:
        messages.error(request, "This link has expired. Request a new one below.")
        return redirect('accounts:profile')

    token_obj.consume()
    user = token_obj.user
    user.is_email_verified = True
    user.save(update_fields=['is_email_verified'])

    messages.success(request, "Email verified! You can now log in.")
    return redirect('accounts:login')


def verify_email_change(request, token):
    """Handles the email-change verification link. Moves pending_email → email."""
    try:
        token_obj = VerificationToken.objects.select_related('user').get(
            token=token, purpose=VerificationToken.PURPOSE_EMAIL_CHANGE
        )
    except VerificationToken.DoesNotExist:
        messages.error(request, "Invalid verification link.")
        return redirect('accounts:profile')

    if not token_obj.is_valid:
        messages.error(request, "This link has expired. Please update your email again.")
        return redirect('accounts:profile')

    token_obj.consume()
    user = token_obj.user

    if not user.pending_email:
        messages.error(request, "No pending email change found.")
        return redirect('accounts:profile')

    user.email         = user.pending_email
    user.pending_email = None
    user.save(update_fields=['email', 'pending_email'])

    messages.success(request, f"Email updated to {user.email}.")
    return redirect('accounts:profile')


@login_required(login_url='accounts:login')
def resend_verification(request):
    user = request.user
    if user.is_email_verified:
        messages.info(request, "Your email is already verified.")
        return redirect('accounts:profile')
    try:
        send_verification_email(user, request)
        messages.success(request, "Verification email sent. Check your inbox.")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Email send failed: {e}")
        messages.error(request, f"Failed to send email: {e}")
    return redirect('accounts:profile')

@login_required(login_url='accounts:login')
@require_POST
def change_password(request):
    user = request.user

    # With Two-Factor Authentication on, password changes must go through
    # the emailed confirmation link instead of this direct form.
    if user.two_factor_enabled:
        messages.error(request, "Two-factor authentication is enabled. Use \"Change\" to request a secure email link instead.")
        return redirect('accounts:profile')

    current  = request.POST.get('current_password', '')
    new_pwd  = request.POST.get('new_password', '')
    confirm  = request.POST.get('confirm_password', '')

    if not user.check_password(current):
        messages.error(request, "Current password is incorrect.")
        return redirect('accounts:profile')

    if new_pwd != confirm:
        messages.error(request, "New passwords do not match.")
        return redirect('accounts:profile')

    try:
        password_validation.validate_password(new_pwd, user)
    except ValidationError as e:
        for error in e.messages:
            messages.error(request, error)
        return redirect('accounts:profile')

    user.set_password(new_pwd)
    user.save()
    # Re-authenticate so the session stays valid
    update_session_auth_hash(request, user)
    # A changed password should actually lock out anyone else still holding
    # a valid session on this account, not just leave them until it expires.
    revoke_other_sessions(user, keep_session_key=request.session.session_key)
    messages.success(request, "Password updated successfully.")
    return redirect('accounts:profile')


@login_required(login_url='accounts:login')
@require_POST
def toggle_two_factor(request):
    """Flips Two-Factor Authentication on/off. Requires the current password
    as proof before changing this security-sensitive setting."""
    user = request.user
    password = request.POST.get('password', '')

    if not user.check_password(password):
        messages.error(request, "Incorrect password. Two-factor authentication was not changed.")
        return redirect('accounts:profile')

    user.two_factor_enabled = not user.two_factor_enabled
    user.save(update_fields=['two_factor_enabled'])
    revoke_other_sessions(user, keep_session_key=request.session.session_key)

    state = "enabled" if user.two_factor_enabled else "disabled"
    messages.success(request, f"Two-factor authentication {state}.")
    return redirect('accounts:profile')


@login_required(login_url='accounts:login')
@require_POST
def request_password_change_email(request):
    """Entry point for changing password when Two-Factor Authentication is
    enabled — sends a confirmation link instead of changing it directly."""
    user = request.user
    if not user.two_factor_enabled:
        return redirect('accounts:profile')

    try:
        send_password_change_verification(user, request)
        messages.success(request, "We've emailed you a secure link to confirm your password change.")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Email send failed: {e}")
        messages.error(request, f"Failed to send email: {e}")
    return redirect('accounts:profile')


def confirm_password_change(request, token):
    """Landing page for the emailed password-change confirmation link.
    Not login-gated — the token identifies the account, and the current
    password (re-entered here) proves it's really the account owner."""
    try:
        token_obj = VerificationToken.objects.select_related('user').get(
            token=token, purpose=VerificationToken.PURPOSE_PASSWORD_CHANGE
        )
    except VerificationToken.DoesNotExist:
        messages.error(request, "Invalid password change link.")
        return redirect('accounts:login')

    if not token_obj.is_valid:
        messages.error(request, "This link has expired or was already used. Request a new one from your profile.")
        return redirect('accounts:login')

    user = token_obj.user

    if request.method == 'POST':
        current = request.POST.get('current_password', '')
        new_pwd = request.POST.get('new_password', '')
        confirm = request.POST.get('confirm_password', '')

        if not user.check_password(current):
            blocked = token_obj.register_failed_attempt()
            if blocked:
                messages.error(request, "Too many incorrect attempts. Please request a new password change email.")
                return redirect('accounts:login')
            remaining = VerificationToken.MAX_ATTEMPTS - token_obj.attempts
            messages.error(request, f"Current password is incorrect. {remaining} attempt(s) left.")
            return render(request, 'accounts/confirm_password_change.html', {'token': token})

        if new_pwd != confirm:
            messages.error(request, "New passwords do not match.")
            return render(request, 'accounts/confirm_password_change.html', {'token': token})

        try:
            password_validation.validate_password(new_pwd, user)
        except ValidationError as e:
            for error in e.messages:
                messages.error(request, error)
            return render(request, 'accounts/confirm_password_change.html', {'token': token})

        user.set_password(new_pwd)
        user.save(update_fields=['password'])
        token_obj.consume()

        if request.user.is_authenticated and request.user.pk == user.pk:
            update_session_auth_hash(request, user)
            revoke_other_sessions(user, keep_session_key=request.session.session_key)
        else:
            # Confirmed from a device that wasn't logged in as this user —
            # nothing here to "keep", so every existing session gets cut.
            revoke_other_sessions(user, keep_session_key=None)

        messages.success(request, "Password updated successfully.")
        return redirect('accounts:profile')

    return render(request, 'accounts/confirm_password_change.html', {'token': token})

@login_required(login_url='accounts:login')
def notifications_view(request):
    """
    Displays all notifications for the logged-in user ordered by latest.
    """
    notifications = request.user.notifications.all().order_by('-created_at')
    return render(request, 'accounts/notifications.html', {
        'notifications': notifications,
        'active_page': 'notifications',
    })


@login_required(login_url='accounts:login')
def mark_notification_read(request, pk):
    """
    Marks a specific notification as read and redirects back.
    """
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    notification.is_read = True
    notification.save()
    return redirect('accounts:notifications')


# ============================================================
# ADDRESSES
# ============================================================

@login_required(login_url='accounts:login')
def addresses_view(request):
    return render(request, 'accounts/addresses.html', {
        'addresses': request.user.addresses.all(),
        'active_page': 'addresses',
    })


def _address_fields_from_post(request):
    """Pulls the map picker's fields off a POST — structured PSGC codes plus
    the pin's coordinates and whatever text the user confirmed/edited."""
    from decimal import Decimal, InvalidOperation

    addr_type = request.POST.get('type', Address.Type.HOME)
    if addr_type not in Address.Type.values:
        addr_type = Address.Type.HOME

    def _decimal(name):
        raw = request.POST.get(name, '').strip()
        if not raw:
            return None
        try:
            return Decimal(raw)
        except InvalidOperation:
            return None

    return {
        'type': addr_type,
        'region': request.POST.get('region', '').strip(),
        'province': request.POST.get('province', '').strip(),
        'city': request.POST.get('city', '').strip(),
        'barangay': request.POST.get('barangay', '').strip(),
        'street_detail': request.POST.get('street_detail', '').strip(),
        'text': request.POST.get('text', '').strip(),
        'latitude': _decimal('latitude'),
        'longitude': _decimal('longitude'),
    }


@login_required(login_url='accounts:login')
@require_POST
def address_create(request):
    fields = _address_fields_from_post(request)

    if not fields['text']:
        fields['text'] = Address(**{k: v for k, v in fields.items() if k != 'text'}).compose_text()

    if not fields['text']:
        messages.error(request, "Please enter an address.")
        return redirect('accounts:addresses')

    is_first = not request.user.addresses.exists()
    Address.objects.create(user=request.user, is_default=is_first, **fields)
    messages.success(request, "Address added.")
    return redirect('accounts:addresses')


@login_required(login_url='accounts:login')
@require_POST
def address_edit(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    if address.is_default:
        messages.error(request, "Your default address can't be edited. Add a new address and set it as default instead.")
        return redirect('accounts:addresses')

    fields = _address_fields_from_post(request)

    if not fields['text']:
        fields['text'] = Address(**{k: v for k, v in fields.items() if k != 'text'}).compose_text()

    if not fields['text']:
        messages.error(request, "Please enter an address.")
        return redirect('accounts:addresses')

    for key, value in fields.items():
        setattr(address, key, value)
    address.save()
    messages.success(request, "Address updated.")
    return redirect('accounts:addresses')


@login_required(login_url='accounts:login')
@require_POST
def address_delete(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    if address.is_default:
        messages.error(request, "You can't delete your default address. Set a different one as default first.")
        return redirect('accounts:addresses')
    address.delete()
    messages.success(request, "Address deleted.")
    return redirect('accounts:addresses')


@login_required(login_url='accounts:login')
@require_POST
def address_set_default(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    address.set_as_default()
    messages.success(request, "Default address updated.")
    return redirect('accounts:addresses')


# ============================================================
# NOTIFICATION PREFERENCES
# ============================================================

@login_required(login_url='accounts:login')
def notification_preferences_view(request):
    prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)
    return render(request, 'accounts/notification_preferences.html', {
        'prefs': prefs,
        'active_page': 'notification_preferences',
    })


@login_required(login_url='accounts:login')
@require_POST
def notification_preferences_update(request):
    prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)

    field = request.POST.get('field')
    valid_fields = {'order_updates', 'promotions', 'delivery_reminders', 'newsletter'}
    if field in valid_fields:
        setattr(prefs, field, request.POST.get('value') == 'on')
        prefs.save(update_fields=[field])

    return redirect('accounts:notification_preferences')


# ============================================================
# SETTINGS — Sessions / Privacy & Data / Language
# (placeholders: each needs real backend work — session listing/revocation,
# data export/account deletion, and an actual i18n setup respectively —
# that hasn't been built yet. Wired up now so the Settings nav is complete.)
# ============================================================

@login_required(login_url='accounts:login')
def help_center_view(request):
    return render(request, 'accounts/help_center.html', {'active_page': 'help_center'})


@login_required(login_url='accounts:login')
def about_view(request):
    return render(request, 'accounts/about.html', {'active_page': 'about'})


@login_required(login_url='accounts:login')
def language_view(request):
    if request.method == 'POST':
        language = request.POST.get('language')
        if language in User.Language.values:
            request.user.language = language
            request.user.save(update_fields=['language'])
            request.session['language'] = language
            messages.success(request, "Language updated.")
        return redirect('accounts:language')

    return render(request, 'accounts/language.html', {'active_page': 'language'})


# ============================================================
# SESSIONS
# ============================================================

@login_required(login_url='accounts:login')
def sessions_view(request):
    from django.contrib.sessions.models import Session as DjangoSession

    rows = list(request.user.sessions.all())
    live_keys = set(
        DjangoSession.objects.filter(
            session_key__in=[r.session_key for r in rows],
            expire_date__gt=timezone.now(),
        ).values_list('session_key', flat=True)
    )
    live_rows = [r for r in rows if r.session_key in live_keys]

    # Tracking rows whose real session already expired (idle timeout, natural
    # expiry) are just dead weight — drop them the next time anyone looks.
    stale_ids = [r.pk for r in rows if r.session_key not in live_keys]
    if stale_ids:
        UserSession.objects.filter(pk__in=stale_ids).delete()

    return render(request, 'accounts/sessions.html', {
        'sessions': live_rows,
        'current_session_key': request.session.session_key,
        'active_page': 'sessions',
    })


@login_required(login_url='accounts:login')
@require_POST
def session_revoke(request, pk):
    from django.contrib.sessions.models import Session as DjangoSession

    target = get_object_or_404(UserSession, pk=pk, user=request.user)
    if target.session_key == request.session.session_key:
        messages.error(request, "That's your current session — use Sign out for that instead.")
        return redirect('accounts:sessions')

    DjangoSession.objects.filter(session_key=target.session_key).delete()
    target.delete()
    messages.success(request, "Session logged out.")
    return redirect('accounts:sessions')


@login_required(login_url='accounts:login')
@require_POST
def session_logout_all_others(request):
    revoke_other_sessions(request.user, keep_session_key=request.session.session_key)
    messages.success(request, "Logged out of all other sessions.")
    return redirect('accounts:sessions')


# ============================================================
# PRIVACY & DATA — Account deactivation
# ============================================================

@login_required(login_url='accounts:login')
def privacy_view(request):
    return render(request, 'accounts/privacy.html', {'active_page': 'privacy'})


@login_required(login_url='accounts:login')
@require_POST
def deactivate_account_request(request):
    """Entry point when Two-Factor Authentication is on — sends a
    confirmation link instead of deactivating right away."""
    user = request.user
    if not user.two_factor_enabled:
        return redirect('accounts:privacy')

    try:
        send_account_deactivation_verification(user, request)
        messages.success(request, "We've emailed you a secure link to confirm account deactivation.")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Email send failed: {e}")
        messages.error(request, f"Failed to send email: {e}")
    return redirect('accounts:privacy')


@login_required(login_url='accounts:login')
@require_POST
def deactivate_account_confirm(request):
    """Direct deactivation path when Two-Factor Authentication is off —
    password confirmation happens right here on the Privacy page, gated by
    a JS confirm() pop-up before the form even submits."""
    user = request.user
    if user.two_factor_enabled:
        messages.error(request, "Two-factor authentication is enabled. Use \"Deactivate Account\" to request a secure email link instead.")
        return redirect('accounts:privacy')

    password = request.POST.get('password', '')
    if not user.check_password(password):
        messages.error(request, "Incorrect password. Your account was not deactivated.")
        return redirect('accounts:privacy')

    deactivate_account(user)
    logout(request)
    messages.success(request, "Your account has been deactivated. We're sorry to see you go.")
    return redirect('accounts:login')


def confirm_account_deactivation(request, token):
    """Landing page for the emailed account-deactivation link. Not
    login-gated — the token identifies the account, and the password
    re-entered here proves it's really the account owner. Same 3-attempt
    lockout as the password-change confirmation flow."""
    try:
        token_obj = VerificationToken.objects.select_related('user').get(
            token=token, purpose=VerificationToken.PURPOSE_ACCOUNT_DEACTIVATE
        )
    except VerificationToken.DoesNotExist:
        messages.error(request, "Invalid account deactivation link.")
        return redirect('accounts:login')

    if not token_obj.is_valid:
        messages.error(request, "This link has expired or was already used. Request a new one from Settings.")
        return redirect('accounts:login')

    user = token_obj.user

    if request.method == 'POST':
        password = request.POST.get('password', '')

        if not user.check_password(password):
            blocked = token_obj.register_failed_attempt()
            if blocked:
                messages.error(request, "Too many incorrect attempts. Please request a new deactivation email.")
                return redirect('accounts:login')
            remaining = VerificationToken.MAX_ATTEMPTS - token_obj.attempts
            messages.error(request, f"Incorrect password. {remaining} attempt(s) left.")
            return render(request, 'accounts/confirm_account_deactivation.html', {'token': token})

        token_obj.consume()
        deactivate_account(user)

        if request.user.is_authenticated and request.user.pk == user.pk:
            logout(request)

        messages.success(request, "Your account has been deactivated. We're sorry to see you go.")
        return redirect('accounts:login')

    return render(request, 'accounts/confirm_account_deactivation.html', {'token': token})