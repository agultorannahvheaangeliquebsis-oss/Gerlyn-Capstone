from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.contrib.auth import authenticate, login, logout, password_validation, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from .models import (
    BranchManagerProfile, OwnerProfile, StaffProfile, User,
    CustomerProfile, SupplierProfile, Notification,
)
from .rate_limit import (
    REGISTER_MAX_ATTEMPTS, REGISTER_WINDOW_SECONDS,
    clear_failed_logins, get_client_ip, is_login_locked_out,
    is_rate_limited, register_failed_login,
)
from .services import contact_number_taken, set_contact_number
from .validators import validate_ph_mobile_number
from .role_policy import USER_MANAGEMENT_ROLE_CHOICES, USER_MANAGEMENT_ROLES
from .two_factor import (
    cancel_login_otp, is_login_otp_locked_out, start_login_otp, verify_login_otp,
)
from core.module_permissions import (
    ALL_MODULE_CODES, MODULE_PERMISSION_REGISTRY, WRITE_ACTIONS, permission_rows,
)


def safe_next(request):
    """The requested post-login destination, but only if it is ours.

    `next` reaches us from a query string on the way in and from a hidden
    field on the way back out, either of which an attacker can set, and
    redirect() will happily send the browser to another origin. Anything
    off-site (or absent) returns None and the caller falls back to the
    role dashboard.
    """
    nxt = request.POST.get('next') or request.GET.get('next')
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return None


def _post_login_redirect(request, user):
    """Where a signed-in user goes: their requested `next`, else their own
    dashboard.

    The role is validated FIRST. Preferring `next` before that check would
    let an account with an unplaceable role stay logged in and browse, which
    is the exact state _reject_invalid_role exists to prevent — the bad role
    has to be caught however the user arrived.
    """
    target = _role_target(user)
    if target is None:
        return _reject_invalid_role(request)
    return redirect(safe_next(request) or target)


def login_view(request):
    # `next` is honoured here as well as after a successful POST: someone who
    # is already signed in and follows a "Log in to buy" link should land on
    # the product, not be bounced to a dashboard for having a live session.
    if request.user.is_authenticated:
        return _post_login_redirect(request, request.user)

    error = None
    username = ''

    if request.method == 'POST':
        username = request.POST.get('username', '')
        password = request.POST.get('password', '')

        if is_login_locked_out(username):
            error = 'Too many failed attempts. Try again in a few minutes.'
        else:
            user = authenticate(request, username=username, password=password)

            if user is not None:
                clear_failed_logins(username)

                if user.two_factor_enabled:
                    # The password was correct, but login() does not happen
                    # yet — only the session's pending-user pointer is set,
                    # so the account is not actually signed in until the
                    # emailed code is verified too. next survives here in
                    # the session rather than a hidden field on the code
                    # form, since there is no reason to expose it there.
                    if not start_login_otp(user, request):
                        # The cache the code itself lives in is unreachable —
                        # nothing was emailed, so sending them to a code
                        # screen that can never succeed would be worse than
                        # this. The password stays unconsumed; they can
                        # simply try again.
                        error = 'Could not start email verification right now. Please try again in a moment.'
                        return render(request, 'accounts/login.html', {
                            'error': error, 'username': username,
                            'next': safe_next(request) or '',
                        })
                    request.session['pending_2fa_user_id'] = user.pk
                    request.session['pending_2fa_next'] = safe_next(request) or ''
                    return redirect('accounts:login_verify')

                login(request, user)
                # Back to whatever they were doing. A shopper who hit "Log in
                # to buy" on a product card was one click from ordering it;
                # dropping them on the customer dashboard instead made them
                # find that product a second time. Same for a staff session
                # that timed out mid-page — @login_required already appends
                # ?next=, and until now this view threw it away.
                #
                # An internal role sent to the storefront home is not a
                # special case: home_view redirects INTERNAL_ROLES to their
                # own dashboard, so that lands correctly on its own.
                return _post_login_redirect(request, user)
            else:
                register_failed_login(username)
                error = 'Invalid username or password.'

    return render(request, 'accounts/login.html', {
        'error': error,
        'username': username,
        # Round-trips the destination through the POST so it survives a failed
        # attempt — otherwise a typo in the password quietly costs the shopper
        # their place.
        'next': safe_next(request) or '',
    })


def login_verify_view(request):
    """Step 2 of login when Two-Factor Authentication is enabled: the emailed
    6-digit code. Reached only via login_view setting pending_2fa_user_id in
    the session — there is deliberately no way to land here without having
    already passed the password check for that specific account."""
    pending_user_id = request.session.get('pending_2fa_user_id')
    if not pending_user_id:
        return redirect('accounts:login')

    user = User.objects.filter(pk=pending_user_id).first()
    if user is None or not user.two_factor_enabled:
        request.session.pop('pending_2fa_user_id', None)
        request.session.pop('pending_2fa_next', None)
        return redirect('accounts:login')

    error = None

    if request.method == 'POST':
        if 'resend' in request.POST:
            if start_login_otp(user, request):
                messages.info(request, f'A new code was sent to {user.email}.')
            else:
                messages.error(request, "Couldn't resend right now. Please try again shortly.")
        elif is_login_otp_locked_out(user):
            request.session.pop('pending_2fa_user_id', None)
            request.session.pop('pending_2fa_next', None)
            cancel_login_otp(user)
            messages.error(request, 'Too many incorrect codes. Please log in again.')
            return redirect('accounts:login')
        elif verify_login_otp(user, request.POST.get('code', '')):
            next_url = request.session.pop('pending_2fa_next', '')
            request.session.pop('pending_2fa_user_id', None)
            login(request, user)
            return redirect(next_url) if next_url else _post_login_redirect(request, user)
        else:
            error = 'Incorrect code. Please try again.'

    return render(request, 'accounts/login_verify.html', {
        'error': error,
        'email': user.email,
    })


def logout_view(request):
    logout(request)
    return redirect('/')


def _role_target(user):
    """The URL name of a user's own landing page, or None if the role is bad.

    Split out of redirect_by_role so login_view can prefer a validated `next`
    over the dashboard without duplicating the role table. Deliberately pure
    — it neither logs anyone out nor redirects — so that the invalid-role
    handling stays in one visible place at each call site instead of being a
    side effect of asking where someone belongs.
    """
    role_redirects = {
        User.Role.OWNER: 'accounts:owner_dashboard',
        User.Role.BRANCH_MANAGER: 'accounts:manager_dashboard',
        User.Role.STAFF: 'accounts:staff_dashboard',
        User.Role.SUPPLIER: 'accounts:supplier_dashboard',
        User.Role.CUSTOMER: 'accounts:customer_dashboard',
    }
    return role_redirects.get(user.role)


def _reject_invalid_role(request):
    """Log out a user whose role we cannot place, and send them to sign-in.

    An unrecognized role (bad data, or a value that no longer exists) must
    never fall through to 'accounts:login' while still authenticated — with
    login_view's own "already authenticated" redirect at the top, that is an
    infinite redirect loop instead of a login page.
    """
    logout(request)
    messages.error(request, 'Your account has an invalid role. Contact the Owner to have it fixed.')
    return redirect('accounts:login')


def redirect_by_role(request, user):
    target = _role_target(user)
    if target is None:
        return _reject_invalid_role(request)
    return redirect(target)


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
    from procurement.access import (
        STAFF_PENDING_DELIVERY_STATUSES, procurements_visible_to,
    )
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

    pending_deliveries = procurements_visible_to(
        request.user,
        Procurement.objects.select_related('supplier__supplier_profile'),
    ).filter(
        status__in=STAFF_PENDING_DELIVERY_STATUSES,
    ).order_by('delivery_date')

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

def _profile_for(user):
    """The role-specific profile carrying the name/branch, if any."""
    return (
        getattr(user, 'manager_profile', None)
        or getattr(user, 'staff_profile', None)
        or getattr(user, 'owner_profile', None)
    )


@login_required(login_url='accounts:login')
def _user_list_context(request):
    users = User.objects.filter(role__in=USER_MANAGEMENT_ROLES).select_related(
        'manager_profile__branch', 'staff_profile__branch', 'owner_profile',
        'supplier_profile',
    ).order_by('role', 'username')

    search_query = request.GET.get('q', '')
    role_filter = request.GET.get('role', '')
    if search_query:
        users = users.filter(username__icontains=search_query)
    if role_filter:
        users = users.filter(role=role_filter)

    # Branch lives on a different profile depending on role — a Manager's,
    # a Staff's, or a Supplier's — so narrowing "assigned to this branch"
    # has to check all three rather than a single FK on User itself.
    branch_filter = request.GET.get('branch', '')
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''
    if branch_filter:
        users = users.filter(
            Q(manager_profile__branch_id=branch_filter) |
            Q(staff_profile__branch_id=branch_filter) |
            Q(supplier_profile__branch_id=branch_filter)
        )

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

    branches = Branch.objects.all()
    return {
        'rows': rows,
        'search_query': search_query,
        'role_filter': role_filter,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Product Master List, Stock Adjustments, etc.
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        # Every other active filter, for the topbar branch switcher
        # (partials/_branch_switch.html) to carry along — same idea as
        # _daterange.html's daterange_carry.
        'branch_switch_carry': [('q', search_query), ('role', role_filter)],
        # The toolbar filter and Add/Edit forms share the same policy-bound
        # list. Customer accounts are handled by the dedicated module.
        'role_choices': USER_MANAGEMENT_ROLE_CHOICES,
        'form_role_choices': USER_MANAGEMENT_ROLE_CHOICES,
        'branches': branches,
        'active_count': users.filter(is_active=True).count(),
        # The detailed matrix is target-specific and is attached when the
        # Edit modal is opened.
    }


@login_required(login_url='accounts:login')
def user_list(request):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('accounts:home')

    context = _user_list_context(request)
    return render(request, 'accounts/user_list.html', context)


def _user_edit_context(request, target, *, branch_id=None, modules=None,
                        actions=None, errors=None, form_data=None):
    """Context for the dedicated Edit User page — GET (current values) and a
    rejected POST (posted values + errors) both render the same template
    through this."""
    profile = _profile_for(target)
    if modules is None:
        modules = target.allowed_modules or []
    return {
        'target': target,
        'branch_id': branch_id if branch_id is not None else (
            getattr(profile, 'branch_id', None) if profile else None
        ),
        'permission_rows': permission_rows(modules, actions or target.module_actions),
        'errors': errors or [],
        'form_data': form_data,
        'branches': Branch.objects.all(),
        'form_role_choices': USER_MANAGEMENT_ROLE_CHOICES,
    }


@login_required(login_url='accounts:login')
def user_form(request, pk=None):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('accounts:home')

    target = get_object_or_404(
        User.objects.filter(role__in=USER_MANAGEMENT_ROLES), pk=pk
    ) if pk else None

    if target is None:
        if request.method != 'POST':
            return redirect(f"{reverse('accounts:user_list')}?add=1")
        return _user_invite_submit(request)

    if request.method != 'POST':
        return render(request, 'accounts/user_edit.html', _user_edit_context(request, target))

    role = request.POST.get('role')
    username = request.POST.get('username', '').strip()
    branch_id = request.POST.get('branch') or None
    first_name = request.POST.get('first_name', '').strip()
    last_name = request.POST.get('last_name', '').strip()
    password = request.POST.get('password', '').strip()
    new_contact_number = request.POST.get('contact_number', '').strip()
    # Filtered against the real registry so a tampered POST can't smuggle an
    # unknown code into the field — it'd be harmless (nothing gates on an
    # unrecognized code) but there's no reason to store it either.
    submitted_modules = [
        c for c in request.POST.getlist('allowed_modules') if c in ALL_MODULE_CODES
    ]
    submitted_actions = {code: [] for code in ALL_MODULE_CODES}
    for token in request.POST.getlist('module_actions'):
        try:
            code, action = token.split(':', 1)
        except ValueError:
            continue
        if (
            code in submitted_modules
            and action in WRITE_ACTIONS
            and MODULE_PERMISSION_REGISTRY[code][action]
        ):
            submitted_actions[code].append(action)

    # The same checks, collected rather than redirected — a redirect
    # discarded the whole form, and this one has several fields to retype.
    errors = []
    if not username or role not in USER_MANAGEMENT_ROLES:
        errors.append('A username and a valid role are required.')

    if password:
        try:
            password_validation.validate_password(password, user=target)
        except ValidationError as e:
            errors.extend(e.messages)

    # Checked upfront (format + cross-profile uniqueness) rather than at
    # save time below, so a bad number is caught before any other field on
    # `target` is written — same collected-not-redirected reasoning as the
    # rest of this block. Blank is valid here (unlike the required
    # username/role above): the Owner may be clearing a number, not just
    # leaving it untouched — see _user_modal.html's contact_number field,
    # which is always prefilled from the current value.
    if new_contact_number:
        try:
            validate_ph_mobile_number(new_contact_number)
        except ValidationError as e:
            errors.append(e.messages[0])
        else:
            if contact_number_taken(new_contact_number, exclude_user=target):
                errors.append('That contact number is already in use.')

    if target.pk == request.user.pk and role != target.role:
        errors.append("You can't change your own role.")

    if not errors:
        role_changed_from = None
        if target.role != role:
            role_changed_from = target.role
        target.username = username
        target.email = request.POST.get('email', '')
        target.first_name = first_name
        target.last_name = last_name
        target.role = role
        if password:
            target.set_password(password)
        target.save()

        # Role changed (or this is a new account) — drop any stale profile
        # left over from a previous role. get_role_profile() resolves in a
        # fixed order independent of the live `role` field, so a leftover
        # profile from before a reassignment would otherwise keep winning
        # over the new one.
        current_role_attr = {
            User.Role.OWNER: 'owner_profile',
            User.Role.BRANCH_MANAGER: 'manager_profile',
            User.Role.STAFF: 'staff_profile',
            User.Role.SUPPLIER: 'supplier_profile',
        }
        for other_role, attr in current_role_attr.items():
            if other_role != role:
                stale = getattr(target, attr, None)
                if stale is not None:
                    stale.delete()

        # Keep the role profile's branch in step with the assignment.
        full_name = f'{first_name} {last_name}'.strip() or username
        if role == User.Role.BRANCH_MANAGER:
            profile, _ = BranchManagerProfile.objects.get_or_create(user=target)
            profile.manager_name = full_name
            profile.branch_id = branch_id
            profile.contact_number = new_contact_number
            profile.save()
        elif role == User.Role.STAFF:
            profile, _ = StaffProfile.objects.get_or_create(user=target)
            profile.staff_name = full_name
            profile.branch_id = branch_id
            profile.contact_number = new_contact_number
            profile.save()
        elif role == User.Role.OWNER:
            profile, _ = OwnerProfile.objects.get_or_create(user=target)
            profile.owner_name = full_name
            profile.contact_number = new_contact_number
            profile.save()
        elif role == User.Role.SUPPLIER:
            profile, _ = SupplierProfile.objects.get_or_create(user=target)
            profile.supplier_name = full_name
            profile.contact_number = new_contact_number
            profile.save()

        # Module access only ever means anything for Staff/Manager (Owner is
        # exempt, Supplier/Customer have their own separate portals) — left
        # untouched for any other role rather than overwritten with an
        # empty list that would just be ignored anyway.
        if role in (User.Role.BRANCH_MANAGER, User.Role.STAFF):
            target.allowed_modules = submitted_modules
            target.module_actions = submitted_actions
            target.save(update_fields=['allowed_modules', 'module_actions'])

        if role_changed_from is not None:
            from core.notifications import CATEGORY_SYSTEM, PRIORITY_HIGH, notify

            # No required_module: User Management is Owner-only, full stop
            # (see core/module_permissions.py) — this reaches the Owner only,
            # not even the target's own Branch Manager, regardless of any
            # module grant.
            notify(
                CATEGORY_SYSTEM, PRIORITY_HIGH,
                f'Role changed — {username}',
                f'{username} was moved from {role_changed_from} to {role} '
                f'by {request.user.username}.',
                branch=(
                    getattr(getattr(target, 'manager_profile', None), 'branch', None)
                    or getattr(getattr(target, 'staff_profile', None), 'branch', None)
                ),
                action_url=reverse('accounts:user_list'),
                action_label='View Users',
            )

        messages.success(request, f'User "{username}" saved.')
        return redirect('accounts:user_list')

    context = _user_edit_context(
        request, target, branch_id=branch_id, modules=submitted_modules,
        actions=submitted_actions, errors=errors, form_data=request.POST,
    )
    return render(request, 'accounts/user_edit.html', context)


def _user_invite_submit(request):
    """The Add User submit target when there is no existing target — invites
    a new internal account by email/role/branch only. Kept as its own
    function rather than folded into user_form's single body: validation,
    creation and the re-render-on-error shape are different enough (no
    username/password at all) that sharing the body just added branching
    without actually sharing much logic."""
    email = request.POST.get('email', '').strip()
    role = request.POST.get('role')
    branch_id = request.POST.get('branch') or None

    errors = []
    if role not in USER_MANAGEMENT_ROLES:
        errors.append('A valid role is required.')
    if not email:
        errors.append('An email address is required.')
    else:
        try:
            validate_email(email)
        except ValidationError:
            errors.append('Enter a valid email address.')
        else:
            if User.objects.filter(email=email).exists():
                errors.append(f'A user with email "{email}" already exists.')
    if not errors and role in (User.Role.BRANCH_MANAGER, User.Role.STAFF) and not branch_id:
        errors.append('Assign a branch for this role.')

    if not errors:
        import uuid

        from .services import send_staff_invite

        # A real username doesn't exist yet — the invited person picks their
        # own on the completion page. This placeholder is never shown
        # anywhere and gets overwritten the moment they do; it only exists
        # because the column itself is required and unique.
        target = User.objects.create(
            username=f'invited-{uuid.uuid4().hex[:24]}', email=email, role=role,
            is_active=False,
        )
        target.set_unusable_password()
        target.save()

        # accounts.signals.create_user_profile already created the matching
        # role profile (named from the placeholder username) the moment
        # target was saved above — only the branch needs setting here.
        if role == User.Role.BRANCH_MANAGER:
            target.manager_profile.branch_id = branch_id
            target.manager_profile.save()
        elif role == User.Role.STAFF:
            target.staff_profile.branch_id = branch_id
            target.staff_profile.save()

        send_staff_invite(target, request)
        messages.success(request, f'Invite sent to {email}.')
        return redirect('accounts:user_list')

    context = _user_list_context(request)
    context.update({
        'show_add_modal': True,
        'add_branch_id': branch_id,
        'add_errors': errors,
        'add_form_data': request.POST,
    })
    return render(request, 'accounts/user_list.html', context)


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
        state = 'reactivated' if target.is_active else 'deactivated'

        from audit.context import set_action_label
        set_action_label(f'{state.capitalize()} a user account')

        target.save()
        messages.success(request, f'{target.username} {state}.')

    return redirect('accounts:customer_list' if target.role == User.Role.CUSTOMER else 'accounts:user_list')


@login_required(login_url='accounts:login')
def user_hard_delete(request, pk):
    """Owner-only, in response to an actual deletion request received
    outside the system (by email) — erases this account's identity fields
    (accounts.services.hard_delete_personal_data) without touching anything
    it's linked to. Works for any role; a customer's own request is the
    expected case, but nothing here is customer-specific."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('accounts:home')

    target = get_object_or_404(User, pk=pk)
    redirect_to = 'accounts:customer_list' if target.role == User.Role.CUSTOMER else 'accounts:user_list'

    if target.pk == request.user.pk:
        messages.error(request, "You can't delete your own personal data.")
        return redirect(redirect_to)

    if request.method != 'POST':
        return redirect(redirect_to)

    if target.deleted_at:
        messages.info(request, f"{target.username}'s personal data was already deleted.")
        return redirect(redirect_to)

    if not target.has_usable_password():
        messages.error(request, "That invite was never completed — cancel it instead.")
        return redirect(redirect_to)

    from audit.context import set_action_label
    set_action_label('Deleted a user account')

    from .services import hard_delete_personal_data
    hard_delete_personal_data(target, performed_by=request.user)
    messages.success(request, "That account's personal data has been deleted.")
    return redirect(redirect_to)


@login_required(login_url='accounts:login')
def user_cancel_invite(request, pk):
    """Owner-only. Only for an account that has never been completed (Add
    User was used, but has_usable_password() is still False) — nothing has
    been created through it yet, so unlike user_hard_delete this actually
    deletes the row (accounts.services.cancel_pending_invite) instead of
    scrubbing and keeping it. There's no audit log yet to record who did
    this, so for now that's just an accepted gap."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('accounts:home')

    target = get_object_or_404(
        User.objects.filter(role__in=USER_MANAGEMENT_ROLES), pk=pk
    )

    if target.has_usable_password():
        messages.error(request, "That account has already been set up and can't be cancelled this way.")
        return redirect('accounts:user_list')

    if request.method != 'POST':
        return redirect('accounts:user_list')

    from .services import cancel_pending_invite
    email = target.email
    cancel_pending_invite(target)
    messages.success(request, f'Invite to {email} cancelled.')
    return redirect('accounts:user_list')


@login_required(login_url='accounts:login')
def user_resend_invite(request, pk):
    """Sends a fresh invite link for a still-pending account (Add User was
    used, but the invited person hasn't finished setting up their username
    and password yet) — the old link, if unused, is invalidated the same
    way accounts.services.send_staff_invite already does for a repeat call."""
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('accounts:home')

    target = get_object_or_404(
        User.objects.filter(role__in=USER_MANAGEMENT_ROLES), pk=pk
    )

    if target.has_usable_password():
        messages.info(request, f"{target.username} has already set up their account.")
        return redirect('accounts:user_list')

    if request.method == 'POST':
        from .services import send_staff_invite
        send_staff_invite(target, request)
        messages.success(request, f'Invite resent to {target.email}.')

    return redirect('accounts:user_list')


@login_required(login_url='accounts:login')
def customer_list(request):
    """Owner-only view of customer accounts — User Management's own list
    always excluded Customer, since nothing there (branch, modules, an
    internal role) applies to one. This exists only so the Owner can find
    an account to act on when an actual deletion request comes in by email;
    it's read-focused, with just the same deactivate/reactivate and hard-
    delete personal data actions User Management offers for internal roles.
    """
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage users.')
        return redirect('accounts:home')

    customers = User.objects.filter(role=User.Role.CUSTOMER).select_related(
        'customer_profile'
    ).order_by('-date_joined')

    search_query = request.GET.get('q', '')
    if search_query:
        customers = customers.filter(
            Q(username__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(customer_profile__first_name__icontains=search_query) |
            Q(customer_profile__last_name__icontains=search_query)
        )

    rows = []
    for u in customers:
        profile = getattr(u, 'customer_profile', None)
        rows.append({
            'user': u,
            'full_name': u.get_full_name() or (
                f'{profile.first_name} {profile.last_name}'.strip() if profile else ''
            ) or u.username,
        })

    return render(request, 'accounts/customer_list.html', {
        'rows': rows,
        'search_query': search_query,
        'active_count': customers.filter(is_active=True).count(),
    })


def home_view(request):
    # "/" is the app's start_url (PWA relaunch, a fresh tab/window, or just
    # typing the domain) — for a logged-in staff/branch-manager/owner it
    # must not render the customer storefront, since that's not their
    # landing experience. CUSTOMER and anonymous visitors are unaffected:
    # this page already *is* their correct landing page. SUPPLIER is sent to
    # their portal too: the storefront is a customer experience, and a supplier
    # who lands on it (reopening the site, the PWA start_url) can't use any of
    # its links — their role is confined to the portal — so they could not even
    # reach Logout from there.
    # ?preview=1 is the one exception: the Owner edits the storefront hero on
    # the internal Settings page and needs to see the result with the staged
    # changes applied. Without this the "preview" link there would just bounce
    # them back to their dashboard. Only internal users get the preview — an
    # anonymous visitor with ?preview=1 sees the ordinary live page.
    from core.permissions import INTERNAL_ROLES
    is_internal = (
        request.user.is_authenticated and request.user.role in INTERNAL_ROLES
    )
    previewing = is_internal and request.GET.get('preview') == '1'
    if is_internal and not previewing:
        return redirect_by_role(request, request.user)
    if request.user.is_authenticated and request.user.role == User.Role.SUPPLIER:
        return redirect_by_role(request, request.user)

    from core.models import HomeHeroSlide
    from inventory.stats import (
        attach_storefront_counts, best_sellers, in_stock_product_ids,
    )

    # 12, not 8: the discover grid runs four across on a desktop, and eight
    # left the second row half-empty.
    #
    # Filtered to in-stock in the QUERY rather than after the slice, or the
    # grid would quietly shrink below 12 whenever the first twelve products
    # happened to include an empty one.
    products = attach_storefront_counts(
        list(
            Product.objects.select_related('category')
            .filter(id__in=in_stock_product_ids())
            .prefetch_related('images')[:12]
        ),
    )
    branches = Branch.objects.filter(is_active=True)
    categories = ProductCategory.objects.all()
    return render(request, 'home.html', {
        'products': products,
        'branches': branches,
        'categories': categories,
        # The discover grid's Add-to-cart button needs to know whether a
        # branch has been chosen, same as the catalog does — without it every
        # card would render the "Choose a branch" branch forever.
        'selected_branch_id': request.session.get('branch_id'),
        # Empty on a store with no sales yet — the rail hides itself rather
        # than showing a "top products" list nobody has bought.
        'best_sellers': best_sellers(limit=8),
        # Owner-editable hero. Empty lists → the template's built-in copy.
        # In preview mode the Owner sees the staged (unpublished) slides.
        'hero_slides': HomeHeroSlide.rotator_slides(preview=previewing),
        'hero_side_cards': HomeHeroSlide.side_cards(preview=previewing),
        # Drives the Apply changes / Cancel bar on the preview.
        'hero_preview': previewing,
        'hero_has_pending_changes': (
            previewing and HomeHeroSlide.has_pending_changes()
        ),
    })

def _register_form_values(request):
    """What the visitor typed, so a rejected POST can re-render it filled in.

    register_view rejects on several separate conditions and every one of
    them used to `render(...)` with no context at all — the form came back
    blank and the shopper retyped every field to fix one of them. The
    address keys are here too: they are hidden inputs the picker fills from
    the Region/Province/City/Barangay dropdowns.

    The two password fields are deliberately absent. A password re-rendered
    into HTML is a password in the browser's view-source and in whatever
    proxy cached the response.
    """
    keep = (
        'username', 'email', 'first_name', 'last_name', 'contact_number',
        'address', 'type', 'region', 'province', 'city', 'barangay',
        'street_detail', 'latitude', 'longitude', 'accept_terms',
    )
    return {k: request.POST.get(k, '') for k in keep}


def register_view(request):
    if request.user.is_authenticated:
        return redirect_by_role(request, request.user)

    if request.method == 'POST':
        # A scripted flood of sign-ups (or of anything below that's cheap to
        # trigger repeatedly, like the username/email uniqueness checks) has
        # no cost to an attacker otherwise — sign-up had no rate limit at
        # all before this. Keyed by IP, not username, since the whole point
        # is to catch it before any of the per-field checks below even run.
        if is_rate_limited('register', get_client_ip(request), REGISTER_MAX_ATTEMPTS, REGISTER_WINDOW_SECONDS):
            messages.error(
                request,
                'Too many sign-up attempts from this connection. Please try again in a while.'
            )
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        username = (request.POST.get('username') or '').strip()
        email = (request.POST.get('email') or '').strip()
        password1 = request.POST.get('password1')
        password2 = request.POST.get('password2')
        first_name = request.POST.get('first_name')
        last_name = request.POST.get('last_name')
        contact_number = request.POST.get('contact_number')
        address = request.POST.get('address', '').strip()

        # Neither field was checked before create_user() reached the DB
        # layer raw — an empty username hit a NOT NULL constraint, and one
        # over the column's 150-char limit hit MySQL's own length error.
        # Both surfaced as a bare 500 instead of a message on the form.
        if not username or len(username) > 150:
            messages.error(request, 'Username must be between 1 and 150 characters.')
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        try:
            User.username_validator(username)
        except ValidationError as e:
            for error in e.messages:
                messages.error(request, error)
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        if not email:
            messages.error(request, 'Enter your email address.')
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, 'Enter a valid email address.')
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        # The delivery address is required — a typed line, optionally filled
        # in from the Region/Province/City/Barangay dropdowns. This is the
        # only place (with Settings -> Addresses) an address is captured:
        # checkout shows it read-only and ships to it, so an account needs
        # one from the start. It can be changed later in Settings.
        try:
            picker_fields = _address_fields_from_post(request)
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})
        if not address:
            messages.error(
                request,
                'Enter your delivery address before creating your account. '
                'It is where every order is delivered, and you can change it '
                'later in Settings.'
            )
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        # Contact number is optional at sign-up (unlike the self-edit and
        # admin-edit paths, which only ever see it once a profile already
        # exists) — validated only when the visitor actually typed one.
        if contact_number:
            try:
                validate_ph_mobile_number(contact_number)
            except ValidationError as e:
                messages.error(request, e.messages[0])
                return render(request, 'accounts/register.html', {'form': _register_form_values(request)})
            if contact_number_taken(contact_number):
                messages.error(request, 'That contact number is already in use.')
                return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        if password1 != password2:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        try:
            password_validation.validate_password(password1)
        except ValidationError as e:
            for error in e.messages:
                messages.error(request, error)
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already taken.')
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        if User.objects.filter(email=email).exists():
            messages.error(request, 'Email already registered.')
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

        # The checkbox's `required` is only a browser nicety — a POST built
        # by hand skips it — so this is the actual gate. Exactly '1', not
        # just "present", so a scripted accept_terms=false doesn't count.
        if request.POST.get('accept_terms') != '1':
            messages.error(
                request,
                'Please tick the box to agree to the Terms & Conditions and '
                'Privacy Notice before creating your account.'
            )
            return render(request, 'accounts/register.html', {'form': _register_form_values(request)})

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
        profile.terms_accepted_at = timezone.now()
        profile.terms_version = CustomerProfile.TERMS_VERSION
        profile.save()

        # Guaranteed present (checked above) — every new account gets its one
        # default Address, which is what checkout ships to.
        picker_fields['text'] = address
        Address.objects.create(user=user, is_default=True, **picker_fields)

        # Verification is optional here (login isn't gated on it — only
        # turning on Two-Factor Authentication is), so a send failure must
        # not block account creation. The user can always retry from the
        # "Resend" banner on their Profile/Settings page.
        verification_sent = True
        try:
            send_verification_email(user, request)
        except VerificationEmailDeliveryError:
            verification_sent = False

        login(request, user)
        messages.success(request, "Account created successfully. Welcome!")
        if verification_sent:
            messages.info(
                request,
                "A verification link was sent to your email. You can also "
                "request another from Settings after the cooldown."
            )
        else:
            messages.warning(
                request,
                "Your account is ready, but we couldn't send the verification "
                "email right now. Please try again from Settings."
            )
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
    get_active_sessions, get_verification_resend_wait_seconds,
    VerificationEmailDeliveryError, VerificationEmailRateLimited,
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
        'verification_resend_wait_seconds': (
            0 if user.is_email_verified else get_verification_resend_wait_seconds(user)
        ),
        'pending_email'     : user.pending_email,
        'avatar_url'        : user.avatar.url if user.avatar else None,
        'contact_number'    : contact_number,
        'role'              : user.get_role_display(),
        'two_factor_enabled': user.two_factor_enabled,
        'addresses'         : user.addresses.all(),
        'active_page'       : 'profile',
    }


def _internal_settings_context(request):
    """Context for internal_settings.html — shared by
    core.views.internal_settings_view and update_profile's error path,
    so re-rendering this template after a validation failure doesn't drop
    the internal chrome or any of its account-security data."""
    from accounts.services import get_active_sessions
    from core.models import HomeHeroSlide
    from core.permissions import OWNER
    is_owner = getattr(request.user, 'role', None) == OWNER
    context = {
        **_profile_context(request.user),
        'active_page': 'internal_settings',
        'sessions': get_active_sessions(request.user),
        'current_session_key': request.session.session_key,
        # Store Settings card: Owner edits, everyone else reads. The row
        # itself (`store_settings`) comes from the context processor.
        'can_edit_store_settings': is_owner,
        # The hero-slide manager list — only the Owner sees (and edits) it.
        # Shadow rows are internal snapshots for the draft workflow, never
        # shown in the editor.
        'hero_slides': (
            list(HomeHeroSlide.objects.filter(is_shadow=False)) if is_owner else []
        ),
        'hero_icon_choices': HomeHeroSlide.Icon.choices,
        'hero_placement_choices': HomeHeroSlide.Placement.choices,
        # Shows the "unpublished changes" banner + Apply / Discard controls.
        'hero_has_pending_changes': (
            is_owner and HomeHeroSlide.has_pending_changes()
        ),
    }
    return context


@login_required(login_url='accounts:login')
def profile_view(request):
    # Enforced here, not just by removing the link that used to point here:
    # this template (public_base.html) and its Personal Information form
    # assume a first/last name and an Address book neither exists for a
    # Supplier, and a Supplier could always reach this URL directly
    # (bookmark, typed address) regardless of what the UI links to.
    if request.user.role == User.Role.SUPPLIER:
        return redirect('supplier_account')
    return render(request, 'accounts/profile.html', _profile_context(request.user))


@login_required(login_url='accounts:login')
@require_POST
def update_profile(request):
    user = request.user
    if user.role == User.Role.SUPPLIER:
        # supplier_account_update is the one path now — it also saves
        # `address`, which this view never touches (Customer's Address book
        # is a different model entirely from SupplierProfile.address).
        return redirect('supplier_account')
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
        role_profile = user.get_or_create_role_profile()
        if role_profile and hasattr(role_profile, 'contact_number'):
            try:
                set_contact_number(role_profile, new_phone, user)
            except ValidationError as e:
                errors['contact_number'] = e.messages[0]

    if errors:
        from core.permissions import INTERNAL_ROLES
        if user.role in INTERNAL_ROLES:
            context = {**_internal_settings_context(request), 'errors': errors}
            return render(request, 'internal_settings.html', context)
        context = {**_profile_context(user), 'errors': errors}
        return render(request, 'accounts/profile.html', context)

    user.save()
    messages.success(request, "Profile updated.")
    return _profile_redirect(user)


@login_required(login_url='accounts:login')
@require_POST
def update_avatar(request):
    user = request.user
    file = request.FILES.get('avatar')

    if not file:
        messages.error(request, "No file was uploaded.")
        return _profile_redirect(user)

    try:
        _validate_avatar(file)
    except ValidationError as e:
        messages.error(request, e.message)
        return _profile_redirect(user)

    # Delete old file from disk before saving new one
    if user.avatar:
        user.avatar.delete(save=False)

    user.avatar = file
    user.save(update_fields=['avatar'])
    messages.success(request, "Profile picture updated.")
    return _profile_redirect(user)


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


def staff_invite_complete(request, token):
    """The link an invited internal user (Staff/Manager/Supplier/Owner)
    receives from Add User. They pick their own username and password here
    — email stays locked to whatever the Owner entered, since that's the
    address the invite itself proved they control, and there is no address
    field since internal accounts don't ship anything."""
    try:
        token_obj = VerificationToken.objects.select_related('user').get(
            token=token, purpose=VerificationToken.PURPOSE_STAFF_INVITE
        )
    except VerificationToken.DoesNotExist:
        messages.error(request, "Invalid or already-used invite link.")
        return redirect('accounts:login')

    if not token_obj.is_valid:
        messages.error(request, "This invite link has expired. Ask the Owner to resend it.")
        return redirect('accounts:login')

    user = token_obj.user

    if user.role not in USER_MANAGEMENT_ROLES:
        token_obj.consume()
        messages.error(request, "This invite is not valid for a customer account.")
        return redirect('accounts:login')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        password1 = request.POST.get('password1', '')
        password2 = request.POST.get('password2', '')

        errors = []
        if not username or len(username) > 150:
            errors.append('Username must be between 1 and 150 characters.')
        else:
            try:
                User.username_validator(username)
            except ValidationError as e:
                errors.extend(e.messages)
            else:
                if User.objects.exclude(pk=user.pk).filter(username=username).exists():
                    errors.append(f'Username "{username}" is already taken.')

        if not first_name or not last_name:
            errors.append('Enter your first and last name.')

        if password1 != password2:
            errors.append('Passwords do not match.')
        else:
            try:
                password_validation.validate_password(password1, user=user)
            except ValidationError as e:
                errors.extend(e.messages)

        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'accounts/staff_invite_complete.html', {
                'token': token, 'email': user.email,
                'form_data': {
                    'username': username, 'first_name': first_name, 'last_name': last_name,
                },
            })

        token_obj.consume()
        user.username = username
        user.first_name = first_name
        user.last_name = last_name
        user.set_password(password1)
        user.is_active = True
        user.save()

        # The role profile's own name field was left blank at invite time —
        # fill it in now that a real name exists.
        full_name = f'{first_name} {last_name}'.strip()
        role_profile = user.get_role_profile()
        for attr in ('manager_name', 'staff_name', 'supplier_name', 'owner_name'):
            if hasattr(role_profile, attr):
                setattr(role_profile, attr, full_name)
                role_profile.save()
                break

        login(request, user)
        messages.success(request, 'Your account is ready.')
        return redirect_by_role(request, user)

    return render(request, 'accounts/staff_invite_complete.html', {
        'token': token, 'email': user.email,
    })


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
@require_POST
def resend_verification(request):
    user = request.user
    if user.is_email_verified:
        messages.info(request, "Your email is already verified.")
        return _profile_redirect(user)

    # The service repeats the cooldown check while holding a per-user DB lock,
    # so rapid or concurrent requests cannot race past this server-side limit.
    try:
        send_verification_email(user, request, enforce_cooldown=True)
        messages.success(request, "Verification email sent. Check your inbox.")
    except VerificationEmailRateLimited as exc:
        wait_minutes = max(1, -(-exc.wait_seconds // 60))  # ceil to whole minutes
        messages.error(
            request,
            f"You can request another verification email in {wait_minutes} minute"
            f"{'s' if wait_minutes != 1 else ''}."
        )
    except VerificationEmailDeliveryError:
        messages.error(
            request,
            "We couldn't send the verification email right now. "
            "Please try again shortly."
        )
    return _profile_redirect(user)

def _profile_redirect(user):
    """Where to land after an account-security action (password, 2FA)
    completes — the internal Settings page for staff/branch managers/
    owners, the Supplier Portal's own account page for suppliers, the
    customer Settings hub for everyone else. Without this, a Supplier (or
    an internal user) doing this from their own account page gets bounced
    into the customer-chromed profile page regardless of where they started
    — change_password/toggle_two_factor/request_password_change_email never
    render a template directly on error, only ever redirect through here, so
    this one branch is enough to keep all three correctly on-shell for every
    role."""
    from core.permissions import INTERNAL_ROLES
    if user.role in INTERNAL_ROLES:
        return redirect('internal_settings')
    if user.role == User.Role.SUPPLIER:
        return redirect('supplier_account')
    return redirect('accounts:profile')


@login_required(login_url='accounts:login')
@require_POST
def change_password(request):
    user = request.user

    # With Two-Factor Authentication on, password changes must go through
    # the emailed confirmation link instead of this direct form.
    if user.two_factor_enabled:
        messages.error(request, "Two-factor authentication is enabled. Use \"Change\" to request a secure email link instead.")
        return _profile_redirect(user)

    current  = request.POST.get('current_password', '')
    new_pwd  = request.POST.get('new_password', '')
    confirm  = request.POST.get('confirm_password', '')

    if not user.check_password(current):
        messages.error(request, "Current password is incorrect.")
        return _profile_redirect(user)

    if new_pwd != confirm:
        messages.error(request, "New passwords do not match.")
        return _profile_redirect(user)

    try:
        password_validation.validate_password(new_pwd, user)
    except ValidationError as e:
        for error in e.messages:
            messages.error(request, error)
        return _profile_redirect(user)

    user.set_password(new_pwd)
    user.save()
    # Re-authenticate so the session stays valid
    update_session_auth_hash(request, user)
    # A changed password should actually lock out anyone else still holding
    # a valid session on this account, not just leave them until it expires.
    revoke_other_sessions(user, keep_session_key=request.session.session_key)
    messages.success(request, "Password updated successfully.")
    return _profile_redirect(user)


@login_required(login_url='accounts:login')
@require_POST
def toggle_two_factor(request):
    """Flips Two-Factor Authentication on/off. Requires the current password
    as proof before changing this security-sensitive setting."""
    user = request.user
    password = request.POST.get('password', '')

    if not user.check_password(password):
        messages.error(request, "Incorrect password. Two-factor authentication was not changed.")
        return _profile_redirect(user)

    turning_on = not user.two_factor_enabled
    # 2FA leans on email delivery (password-change confirmation links) —
    # turning it on with an unverified address would lock the user out of
    # ever completing that flow. Turning OFF is always allowed regardless,
    # so an unverified user is never stuck unable to manage their own
    # security settings.
    if turning_on and not user.is_email_verified:
        messages.error(request, "Verify your email address before turning on two-factor authentication — it's how we confirm password changes.")
        return _profile_redirect(user)

    user.two_factor_enabled = turning_on
    user.save(update_fields=['two_factor_enabled'])
    revoke_other_sessions(user, keep_session_key=request.session.session_key)

    state = "enabled" if user.two_factor_enabled else "disabled"
    messages.success(request, f"Two-factor authentication {state}.")
    return _profile_redirect(user)


@login_required(login_url='accounts:login')
@require_POST
def request_password_change_email(request):
    """Entry point for changing password when Two-Factor Authentication is
    enabled — sends a confirmation link instead of changing it directly."""
    user = request.user
    if not user.two_factor_enabled:
        return _profile_redirect(user)

    try:
        send_password_change_verification(user, request)
        messages.success(request, "We've emailed you a secure link to confirm your password change.")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Email send failed: {e}")
        messages.error(request, f"Failed to send email: {e}")
    return _profile_redirect(user)


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
        return _profile_redirect(user)

    return render(request, 'accounts/confirm_password_change.html', {'token': token})

def _is_xhr(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


@login_required(login_url='accounts:login')
def notifications_view(request):
    """
    The notifications page: what the store has sent, and what it is allowed
    to send.

    These were two pages until now — an inbox at /notifications/ and a
    preferences screen at /profile/notification-preferences/ — sitting as two
    rows in the account rail under names that differ by one word. Reading a
    notification and deciding whether to receive that kind of notification are
    the same errand, and splitting them meant the answer to "why am I getting
    these?" was never on the page showing them.

    XHR (X-Requested-With) hits this same URL for the storefront header's
    floating tray — static/js/notification-tray.js, shared verbatim with the
    internal Owner tray (see core/views.py:notification_center_view) — and
    gets back a JSON fragment (list_html + needs_action_count) instead of the
    full page, same convention as that view. The href itself stays the
    no-JS/fetch-failure fallback straight to this page.
    """
    notifications = request.user.notifications.all().order_by('-created_at')
    if _is_xhr(request):
        unread_count = notifications.filter(is_read=False).count()
        list_html = render_to_string('accounts/_notification_tray_list.html', {
            'notifications': notifications[:8],
        }, request=request)
        return JsonResponse({'list_html': list_html, 'needs_action_count': unread_count})

    # The full-page render below is customer-shelled with customer-only
    # preference toggles (order_updates/promotions/delivery_reminders/
    # newsletter) — a Supplier has their own inbox at supplier_notifications
    # already (no XHR tray of its own to hit this branch from, so only the
    # direct-visit case needs the redirect).
    if request.user.role == User.Role.SUPPLIER:
        return redirect('supplier_notifications')

    prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)
    return render(request, 'accounts/notifications.html', {
        'notifications': notifications,
        'unread_count': sum(1 for n in notifications if not n.is_read),
        'pref_rows': [
            {'field': f, 'label': label, 'sub': sub, 'on': getattr(prefs, f)}
            for f, label, sub in NOTIFICATION_PREFERENCE_ROWS
        ],
        'active_page': 'notifications',
    })


@require_POST
@login_required(login_url='accounts:login')
def mark_notification_read(request, pk):
    """
    Marks a specific notification as read.

    POST, not the GET link this used to be: marking something read changes
    stored state, and a plain <a href> hands that change to anything that
    follows links on the page — a prefetcher, a crawler, an accessibility
    tool walking the DOM — which is how an inbox empties itself without
    anyone having clicked.
    """
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if notification.is_read:
        if not _is_xhr(request):
            messages.info(request, "That notification was already marked as read.")
    else:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=['is_read', 'read_at'])
        if not _is_xhr(request):
            messages.success(request, "Marked as read.")
    if _is_xhr(request):
        return JsonResponse({
            'needs_action_count': request.user.notifications.filter(is_read=False).count(),
        })
    return redirect('accounts:notifications')


@require_POST
@login_required(login_url='accounts:login')
def mark_all_notifications_read(request):
    """
    Clears the unread badge in one action.

    Without this the only way to empty a header badge showing, say, 14 is to
    press "Mark as read" fourteen times — and the badge is on every page of
    the storefront, so an inbox nobody clears is a number that follows the
    shopper around the shop forever.
    """
    updated = request.user.notifications.filter(is_read=False).update(
        is_read=True, read_at=timezone.now()
    )
    if _is_xhr(request):
        return JsonResponse({
            'needs_action_count': request.user.notifications.filter(is_read=False).count(),
        })
    if updated:
        messages.success(
            request,
            f"Marked {updated} notification{'' if updated == 1 else 's'} as read.",
        )
    else:
        messages.info(request, "You have no unread notifications.")
    return redirect('accounts:notifications')


# ============================================================
# ADDRESSES
# ============================================================

def _addresses_anchor():
    """Where the Saved Addresses card sits on the merged profile page, so a
    save/delete/set-default returns the reader to their addresses instead of
    the top of the page."""
    return reverse('accounts:profile') + '#addresses'


def _address_changed(request):
    """Force the cart to resolve again after a saved-address mutation."""
    request.session.pop('fulfillment_location', None)
    request.session.pop('fulfillment_assignment', None)


@login_required(login_url='accounts:login')
def addresses_view(request):
    """
    Permanently redirects to the merged profile page.

    Addresses used to be their own page; they are now the Saved Addresses
    card on My Profile. The route stays, same as notification_preferences_view
    below, so old bookmarks and links land somewhere real instead of 404ing.
    """
    return redirect(_addresses_anchor(), permanent=True)


def _address_fields_from_post(request):
    """Pulls the address picker's fields off a POST — the Region/Province/
    City/Barangay codes off the dropdowns, an optional street line, and
    whatever address text the user typed or confirmed."""
    addr_type = request.POST.get('type', Address.Type.HOME)
    if addr_type not in Address.Type.values:
        addr_type = Address.Type.HOME

    from .geocoding import parse_coordinates

    latitude, longitude = parse_coordinates(
        request.POST.get('latitude'), request.POST.get('longitude')
    )
    return {
        'type': addr_type,
        'region': request.POST.get('region', '').strip(),
        'province': request.POST.get('province', '').strip(),
        'city': request.POST.get('city', '').strip(),
        'barangay': request.POST.get('barangay', '').strip(),
        'street_detail': request.POST.get('street_detail', '').strip(),
        'text': request.POST.get('text', '').strip(),
        'latitude': latitude,
        'longitude': longitude,
    }


@login_required(login_url='accounts:login')
@require_POST
def address_create(request):
    try:
        fields = _address_fields_from_post(request)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(_addresses_anchor())

    if not fields['text']:
        fields['text'] = Address(**{k: v for k, v in fields.items() if k != 'text'}).compose_text()

    if not fields['text']:
        messages.error(request, "Please enter an address.")
        return redirect(_addresses_anchor())

    is_first = not request.user.addresses.exists()
    Address.objects.create(user=request.user, is_default=is_first, **fields)
    _address_changed(request)
    messages.success(request, "Address added.")
    return redirect(_addresses_anchor())


@login_required(login_url='accounts:login')
@require_POST
def address_edit(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)

    try:
        fields = _address_fields_from_post(request)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(_addresses_anchor())

    if not fields['text']:
        fields['text'] = Address(**{k: v for k, v in fields.items() if k != 'text'}).compose_text()

    if not fields['text']:
        messages.error(request, "Please enter an address.")
        return redirect(_addresses_anchor())

    for key, value in fields.items():
        setattr(address, key, value)
    address.save()
    _address_changed(request)
    messages.success(request, "Address updated.")
    return redirect(_addresses_anchor())


@login_required(login_url='accounts:login')
@require_POST
def address_delete(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    if address.is_default:
        messages.error(request, "You can't delete your default address. Set a different one as default first.")
        return redirect(_addresses_anchor())
    address.delete()
    _address_changed(request)
    messages.success(request, "Address deleted.")
    return redirect(_addresses_anchor())


@login_required(login_url='accounts:login')
@require_POST
def address_set_default(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    address.set_as_default()
    _address_changed(request)
    messages.success(request, "Default address updated.")
    return redirect(_addresses_anchor())


# ============================================================
# NOTIFICATION PREFERENCES
# ============================================================

# The four switches, in the order they are drawn: field name, label,
# description. One table rather than four hand-copied blocks of markup — the
# template repeated the same nine lines per switch, so adding a fifth meant
# copying it a fifth time and remembering to change three strings inside.
#
# The English strings live here, not only in the template, for two reasons:
# the confirmation message after a save has to name the switch that moved
# ("Delivery reminders turned off", never "delivery_reminders"), and the same
# list has to reject an unknown ?field=. They stay verbatim so {% t %} still
# finds them in accounts/translations.py, which carries a Filipino line for
# every one.
NOTIFICATION_PREFERENCE_ROWS = [
    ('order_updates', 'Order updates',
     'Status changes and delivery confirmations'),
    ('promotions', 'Promotions & deals',
     'Discounts and flash sales'),
    ('delivery_reminders', 'Delivery reminders',
     'Reminders before your order arrives'),
    ('newsletter', 'Newsletter',
     'Monthly store news and updates'),
]
NOTIFICATION_PREFERENCE_LABELS = {f: label for f, label, _ in NOTIFICATION_PREFERENCE_ROWS}


def _preferences_anchor():
    """Where the preferences card sits on the merged page, so a save returns
    the reader to the switch they just flipped instead of the top of their
    inbox. A function rather than a module constant: reverse() at import time
    runs before the URLconf is loaded."""
    return reverse('accounts:notifications') + '#preferences'


@login_required(login_url='accounts:login')
def notification_preferences_view(request):
    """
    Permanently redirects to the merged notifications page.

    The route stays rather than being deleted: it has been the preferences
    URL for the life of the settings section, so it is in browser histories
    and autocompletes, and a 404 there teaches nothing. A 301 sends those
    straight to the card that replaced it.
    """
    return redirect(_preferences_anchor(), permanent=True)


@login_required(login_url='accounts:login')
@require_POST
def notification_preferences_update(request):
    prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)

    field = request.POST.get('field')
    if field not in NOTIFICATION_PREFERENCE_LABELS:
        # Previously this fell through to a bare redirect: an unknown field
        # saved nothing and said nothing, so a typo'd switch looked exactly
        # like a working one that had simply been left alone.
        messages.error(request, "That notification setting doesn't exist.")
        return redirect(_preferences_anchor())

    on = request.POST.get('value') == 'on'
    setattr(prefs, field, on)
    prefs.save(update_fields=[field])
    messages.success(
        request,
        f"{NOTIFICATION_PREFERENCE_LABELS[field]} turned {'on' if on else 'off'}.",
    )
    return redirect(_preferences_anchor())


# ============================================================
# SETTINGS — Sessions / Privacy & Data / Language
# (placeholders: each needs real backend work — session listing/revocation,
# data export/account deletion, and an actual i18n setup respectively —
# that hasn't been built yet. Wired up now so the Settings nav is complete.)
# ============================================================

@login_required(login_url='accounts:login')
def help_center_view(request):
    # Content, not just chrome, differs by role: the customer FAQ answers
    # questions about delivery addresses and checkout that don't apply to a
    # Supplier account, so this isn't just a shell swap the way language/
    # about are — procurement/supplier_help_center.html carries its own,
    # supplier-relevant questions instead of reusing this copy.
    if request.user.role == User.Role.SUPPLIER:
        return render(request, 'procurement/supplier_help_center.html', {'active_page': 'help_center'})
    return render(request, 'accounts/help_center.html', {'active_page': 'help_center'})


@login_required(login_url='accounts:login')
def about_view(request):
    # Same store-general content either way (branches, delivery, payment
    # methods, help link) — accounts/_about_card.html and
    # procurement/supplier_about.html both include the shared
    # accounts/_about_card.html partial rather than duplicating it, only
    # the surrounding shell (and the Help Center link's target) differs.
    if request.user.role == User.Role.SUPPLIER:
        return render(request, 'procurement/supplier_about.html', {'active_page': 'about'})
    return render(request, 'accounts/about.html', {'active_page': 'about'})


def terms_privacy_view(request):
    """Terms & Conditions and Privacy Notice, public on purpose: the sign-up
    form's consent box links here, and the person reading it has no account
    yet. The Settings "Privacy & Data" page is the logged-in control panel,
    not the policy itself."""
    return render(request, 'accounts/terms_privacy.html', {
        'terms_version': CustomerProfile.TERMS_VERSION,
    })


@login_required(login_url='accounts:login')
def language_view(request):
    if request.method == 'POST':
        language = request.POST.get('language')
        if language in User.Language.values:
            request.user.language = language
            request.user.save(update_fields=['language'])
            request.session['language'] = language
            messages.success(request, "Language updated.")
        else:
            # Silent before this: an unrecognised code fell straight past the
            # branch to the redirect, so the page came back in the language it
            # started in with nothing said — which is exactly what a save that
            # failed to stick looks like.
            messages.error(request, "That language isn't available. Your language is unchanged.")
        return redirect('accounts:language')

    # request.user.language is a base User field — the save above already
    # works for any role — so only the template (which shell wraps the same
    # English/Filipino toggle) needs to branch here.
    if request.user.role == User.Role.SUPPLIER:
        return render(request, 'procurement/supplier_language.html', {'active_page': 'language'})
    return render(request, 'accounts/language.html', {'active_page': 'language'})


# ============================================================
# SESSIONS
# ============================================================

def _sessions_redirect(user):
    """Mirrors _profile_redirect for the Sessions actions — internal users
    land back on their own Settings page, a Supplier back on their own
    Account page (Sessions is a #sessions card there, not its own page —
    see supplier_account.html), not the customer Sessions page."""
    from core.permissions import INTERNAL_ROLES
    if user.role in INTERNAL_ROLES:
        return redirect('internal_settings')
    if user.role == User.Role.SUPPLIER:
        return redirect('supplier_account')
    return redirect('accounts:sessions')


@login_required(login_url='accounts:login')
def sessions_view(request):
    # Supplier Sessions lives inline on their own Account page (see
    # _sessions_redirect above) — this page is customer-shelled
    # (public_base.html) and was never built to be reached by any other
    # role, so a Supplier landing here directly (bookmark, typed URL) is
    # sent to the equivalent card instead of the wrong chrome.
    if request.user.role == User.Role.SUPPLIER:
        return redirect('supplier_account')
    return render(request, 'accounts/sessions.html', {
        'sessions': get_active_sessions(request.user),
        'current_session_key': request.session.session_key,
        'active_page': 'sessions',
    })


@login_required(login_url='accounts:login')
@require_POST
def session_revoke(request, pk):
    from django.contrib.sessions.models import Session as DjangoSession

    target = get_object_or_404(UserSession, pk=pk, user=request.user)
    if target.session_key == request.session.session_key:
        messages.error(request, "That's your current session — use Logout for that instead.")
        return _sessions_redirect(request.user)

    DjangoSession.objects.filter(session_key=target.session_key).delete()
    target.delete()
    messages.success(request, "Session logged out.")
    return _sessions_redirect(request.user)


@login_required(login_url='accounts:login')
@require_POST
def session_logout_all_others(request):
    revoke_other_sessions(request.user, keep_session_key=request.session.session_key)
    messages.success(request, "Logged out of all other sessions.")
    return _sessions_redirect(request.user)


# ============================================================
# PRIVACY & DATA — Account deactivation
# ============================================================

@login_required(login_url='accounts:login')
def privacy_view(request):
    # No self-deactivate/delete for a Supplier per the permission matrix —
    # that's an Owner action on the account, not self-service (same as
    # internal roles, which never get a Privacy & Data page at all).
    if request.user.role == User.Role.SUPPLIER:
        return redirect('supplier_account')
    return render(request, 'accounts/privacy.html', {'active_page': 'privacy'})


@login_required(login_url='accounts:login')
@require_POST
def deactivate_account_request(request):
    """Entry point when Two-Factor Authentication is on — sends a
    confirmation link instead of deactivating right away."""
    user = request.user
    # Guarded here too, not just on the GET page it's posted from — a
    # Supplier POSTing this URL directly would otherwise still trigger a
    # real deactivation email even with privacy_view itself blocked.
    if user.role == User.Role.SUPPLIER:
        return redirect('supplier_account')
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
    # Same backend guard as deactivate_account_request above — a Supplier
    # is never allowed to self-deactivate regardless of which of the two
    # POST endpoints is hit directly.
    if user.role == User.Role.SUPPLIER:
        return redirect('supplier_account')
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
