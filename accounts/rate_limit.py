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


def clear_failed_logins(identifier):
    if not identifier:
        return
    cache.delete(_key(identifier))
