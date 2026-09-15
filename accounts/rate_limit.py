"""Simple cache-backed login lockout — shared by the template login view and
the DRF LoginView so both close the same gap the same way: unlimited
password guessing against a known username (including the Owner account),
with nothing to slow an attacker down.

Keyed by username rather than IP: an attacker rotating IPs is out of scope
here, but capping attempts against the account itself directly closes the
brute-force path either way.
"""
from django.core.cache import cache

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes

REGISTER_MAX_ATTEMPTS = 5
REGISTER_WINDOW_SECONDS = 60 * 60  # 1 hour


def _key(identifier):
    return f'login_attempts:{identifier.strip().lower()}'


def is_login_locked_out(identifier):
    if not identifier:
        return False
    return cache.get(_key(identifier), 0) >= LOGIN_MAX_ATTEMPTS


def register_failed_login(identifier):
    if not identifier:
        return
    key = _key(identifier)
    attempts = cache.get(key, 0) + 1
    cache.set(key, attempts, LOGIN_LOCKOUT_SECONDS)

    # Owner Notification Center trigger — fires exactly once, on the attempt
    # that crosses the threshold, not on every subsequent guess while the
    # account is already locked out.
    if attempts == LOGIN_MAX_ATTEMPTS:
        _notify_lockout(identifier)


def _notify_lockout(identifier):
    from accounts.models import User
    from core.permissions import INTERNAL_ROLES
    from core.notifications import CATEGORY_SYSTEM, PRIORITY_MEDIUM, notify

    user = User.objects.filter(username__iexact=identifier.strip()).first()
    if not user or user.role not in INTERNAL_ROLES:
        return  # only internal accounts (Owner/Manager/Staff) are this center's concern

    # No required_module: a lockout is a security event about an account,
    # not a section of the app — reaches the Owner only, not even that
    # account's own Branch Manager, regardless of what modules the
    # locked-out account has.
    notify(
        CATEGORY_SYSTEM, PRIORITY_MEDIUM,
        f'Login lockout — {user.username}',
        f'{LOGIN_MAX_ATTEMPTS} failed login attempts locked out the '
        f'"{user.username}" account for {LOGIN_LOCKOUT_SECONDS // 60} minutes.',
        branch=(
            getattr(getattr(user, 'manager_profile', None), 'branch', None)
            or getattr(getattr(user, 'staff_profile', None), 'branch', None)
        ),
    )


def clear_failed_logins(identifier):
    if not identifier:
        return
    cache.delete(_key(identifier))


def get_client_ip(request):
    """Best-effort real client IP behind Railway's proxy. REMOTE_ADDR alone
    would always read as the proxy's own address in production, making any
    limit keyed on it useless — X-Forwarded-For's first entry is the
    original client. Local dev has no proxy in front of it, so this falls
    straight back to REMOTE_ADDR there."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def is_rate_limited(bucket, identifier, max_attempts, window_seconds):
    """Generic cache-backed limit: at most max_attempts calls per identifier
    within window_seconds, shared by any caller that needs one (sign-up,
    the address lookup). Returns True if this call should be refused.

    Once blocked, further calls do NOT push the window back out — only
    calls that were actually allowed count towards it — so a script that
    keeps hammering past the limit still has the block expire on schedule
    instead of resetting the clock on every attempt.
    """
    if not identifier:
        return False
    key = f'ratelimit:{bucket}:{identifier}'
    attempts = cache.get(key, 0)
    if attempts >= max_attempts:
        return True
    cache.set(key, attempts + 1, window_seconds)
    return False
