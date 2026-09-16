"""Login-time Two-Factor Authentication: a random 6-digit code, emailed and
entered back within a short window, required only when the account has
Two-Factor Authentication turned on. Shared by the template login flow
(accounts.views.login_view/login_verify_view) and the DRF API
(accounts.api_views.LoginView/VerifyTwoFactorView) so both close the same
gap the same way instead of drifting apart — before this module existed,
turning Two-Factor Authentication on in Settings didn't change login at
all; it only gated password-change confirmation.

Cache-backed, the same pattern as rate_limit.py's login lockout: nothing
here outlives its TTL, and a server restart just means the user re-enters
their password — an acceptable cost for state this short-lived.

Unlike rate_limit.py's lockout, a cache backend failure here can't just
fail open — the code itself lives in the cache, so "cache unreachable"
must read as "verification temporarily unavailable" (refuse the login
attempt), never as "let them in anyway". start_login_otp returns False
when it can't actually stash a checkable code, specifically so callers
don't email one and then strand the user on a code screen that can never
succeed; verify_login_otp already fails closed on a cache error on its
own (a missing code just doesn't match), so it needs no separate signal.
"""
import random

from django.core.cache import cache

OTP_LENGTH = 6
OTP_TTL_SECONDS = 10 * 60  # 10 minutes to receive and enter the email
OTP_MAX_ATTEMPTS = 5


def _code_key(user_id):
    return f'login_otp:{user_id}'


def _attempts_key(user_id):
    return f'login_otp_attempts:{user_id}'


def start_login_otp(user, request):
    """Generates a fresh code, emails it, and resets the attempt counter for
    a new window. Called once per successful password check, so a repeat
    login attempt always gets a brand new code rather than reusing
    whatever is still sitting in the cache from an earlier one.

    Returns True if a checkable code now exists and the email was sent;
    False if the cache is unavailable, in which case nothing was emailed
    and the caller must not proceed into the code-entry step.
    """
    from .services import send_login_otp

    code = f'{random.randint(0, 10 ** OTP_LENGTH - 1):0{OTP_LENGTH}d}'
    try:
        cache.set(_code_key(user.pk), code, OTP_TTL_SECONDS)
        cache.delete(_attempts_key(user.pk))
    except Exception:
        return False
    send_login_otp(user, code, request)
    return True


def is_login_otp_locked_out(user):
    try:
        return cache.get(_attempts_key(user.pk), 0) >= OTP_MAX_ATTEMPTS
    except Exception:
        return False  # verify_login_otp fails closed on its own either way


def verify_login_otp(user, submitted_code):
    """True if submitted_code matches the live code for this user and the
    attempt budget for this window isn't already spent. The code is
    single-use — a correct guess deletes it immediately, so the same value
    can never be replayed to log in twice. A 6-digit code is only a million
    possibilities, so this attempt cap is what actually makes it a second
    factor rather than a slightly-annoying formality.

    A cache read/write failure here must never be mistaken for a match —
    real_code stays None on error, which the check below already treats
    as "no code to match against", the same as an expired one.
    """
    if is_login_otp_locked_out(user):
        return False

    try:
        real_code = cache.get(_code_key(user.pk))
    except Exception:
        real_code = None
    submitted = (submitted_code or '').strip()
    if not real_code or not submitted or submitted != real_code:
        try:
            attempts_key = _attempts_key(user.pk)
            cache.set(attempts_key, cache.get(attempts_key, 0) + 1, OTP_TTL_SECONDS)
        except Exception:
            pass
        return False

    try:
        cache.delete(_code_key(user.pk))
        cache.delete(_attempts_key(user.pk))
    except Exception:
        pass
    return True


def cancel_login_otp(user):
    """Called when a pending 2FA login is abandoned (back to the login form,
    or the session's pending-user reference is otherwise invalidated) so a
    still-valid emailed code can't be used later."""
    try:
        cache.delete(_code_key(user.pk))
        cache.delete(_attempts_key(user.pk))
    except Exception:
        pass
