# accounts/services.py
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse
from .models import VerificationToken, NotificationPreference


def _build_url(request, view_name, token):
    relative = reverse(view_name, kwargs={'token': str(token.token)})
    return request.build_absolute_uri(relative)


def should_notify(user, kind):
    """Gate for any outbound notification (email/push/SMS) tied to a user
    preference toggle. `kind` is one of: 'order_updates', 'promotions',
    'delivery_reminders', 'newsletter'.

    Call this before sending — e.g.:
        if should_notify(user, 'promotions'):
            send_promo_email(user)
    """
    prefs, _ = NotificationPreference.objects.get_or_create(user=user)
    return getattr(prefs, kind, True)


def revoke_other_sessions(user, keep_session_key=None):
    """Ends every active session for `user` except `keep_session_key` (if
    given — pass None to end all of them). Deletes the real Django session
    rows, not just the tracking table, so those devices are actually
    logged out on their next request, not just delisted here.

    Used by Settings > Sessions ("Log out all other sessions") and
    automatically after a password change or a Two-Factor Authentication
    toggle, since those are exactly the moments an attacker's still-valid
    session on the account should be cut loose immediately.
    """
    from django.contrib.sessions.models import Session
    from .models import UserSession

    qs = UserSession.objects.filter(user=user)
    if keep_session_key:
        qs = qs.exclude(session_key=keep_session_key)

    session_keys = list(qs.values_list('session_key', flat=True))
    if session_keys:
        Session.objects.filter(session_key__in=session_keys).delete()
        qs.delete()


def get_active_sessions(user):
    """Returns `user`'s still-live UserSession rows (joined against real,
    unexpired django_session rows), pruning any tracking rows whose session
    already expired — idle timeout or otherwise. Shared by the customer
    Sessions page and the internal Settings page's inline Sessions panel,
    so both list/revoke against the exact same data."""
    from django.contrib.sessions.models import Session
    from django.utils import timezone
    from .models import UserSession

    rows = list(user.sessions.all())
    live_keys = set(
        Session.objects.filter(
            session_key__in=[r.session_key for r in rows],
            expire_date__gt=timezone.now(),
        ).values_list('session_key', flat=True)
    )
    live_rows = [r for r in rows if r.session_key in live_keys]

    stale_ids = [r.pk for r in rows if r.session_key not in live_keys]
    if stale_ids:
        UserSession.objects.filter(pk__in=stale_ids).delete()

    return live_rows


VERIFICATION_RESEND_COOLDOWN_SECONDS = 30 * 60


def get_verification_resend_wait_seconds(user):
    """Seconds until `user` may request another verification email, or 0 if
    they can request one right now. Based on the most recent EMAIL_VERIFY
    token's created_at, checked BEFORE send_verification_email's
    delete-then-recreate — that call erases old tokens entirely, so this
    must run first or the timestamp trail is gone."""
    from django.utils import timezone

    last = VerificationToken.objects.filter(
        user=user, purpose=VerificationToken.PURPOSE_EMAIL_VERIFY,
    ).order_by('-created_at').first()
    if last is None:
        return 0
    elapsed = (timezone.now() - last.created_at).total_seconds()
    remaining = VERIFICATION_RESEND_COOLDOWN_SECONDS - elapsed
    return max(0, int(remaining))


def send_verification_email(user, request):
    """
    Sends initial email verification link after registration.
    Deletes any existing unused tokens for this user first.
    """
    VerificationToken.objects.filter(
        user=user, purpose=VerificationToken.PURPOSE_EMAIL_VERIFY, is_used=False
    ).delete()

    token = VerificationToken.objects.create(
        user=user, purpose=VerificationToken.PURPOSE_EMAIL_VERIFY
    )
    url = _build_url(request, 'accounts:verify_email', token)

    send_mail(
        subject="Verify your Gerlyn Variety Store email",
        message=(
            f"Hi {user.get_full_name() or user.username},\n\n"
            f"Click the link below to verify your email. "
            f"It expires in {VerificationToken.EXPIRY_HOURS} hours.\n\n"
            f"{url}\n\n"
            f"If you didn't create an account, ignore this email.\n\n"
            f"— Gerlyn Variety Store"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def send_login_otp(user, code, request):
    """
    Sends the login-time one-time code — the actual second authentication
    factor. Distinct from send_password_change_verification above, which
    only confirms a password change by email and never gated login itself.
    """
    send_mail(
        subject="Your Gerlyn Variety Store login code",
        message=(
            f"Hi {user.get_full_name() or user.username},\n\n"
            f"Your login code is: {code}\n\n"
            f"Enter it on the sign-in screen to finish logging in. It expires "
            f"in 10 minutes and can only be used once.\n\n"
            f"If you didn't try to log in, you can ignore this email — your "
            f"account is still safe.\n\n"
            f"— Gerlyn Variety Store"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def send_password_change_verification(user, request):
    """
    Sends a confirmation link the user must click before their password
    change (requested while Two-Factor Authentication is enabled) takes effect.
    """
    VerificationToken.objects.filter(
        user=user, purpose=VerificationToken.PURPOSE_PASSWORD_CHANGE, is_used=False
    ).delete()

    token = VerificationToken.objects.create(
        user=user, purpose=VerificationToken.PURPOSE_PASSWORD_CHANGE
    )
    url = _build_url(request, 'accounts:confirm_password_change', token)

    send_mail(
        subject="Confirm your password change — Gerlyn Variety Store",
        message=(
            f"Hi {user.get_full_name() or user.username},\n\n"
            f"We received a request to change your account password. "
            f"Two-factor authentication is enabled on your account, so we need "
            f"you to confirm this change here:\n\n"
            f"{url}\n\n"
            f"This link expires in {VerificationToken.EXPIRY_HOURS} hours and can "
            f"only be used once.\n\n"
            f"If you didn't request this, ignore this email — your password will not change.\n\n"
            f"— Gerlyn Variety Store"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def send_account_deactivation_verification(user, request):
    """
    Sends a confirmation link the user must click before their account
    deactivation (requested while Two-Factor Authentication is enabled)
    can proceed to the password + final-confirmation step.
    """
    VerificationToken.objects.filter(
        user=user, purpose=VerificationToken.PURPOSE_ACCOUNT_DEACTIVATE, is_used=False
    ).delete()

    token = VerificationToken.objects.create(
        user=user, purpose=VerificationToken.PURPOSE_ACCOUNT_DEACTIVATE
    )
    url = _build_url(request, 'accounts:confirm_account_deactivation', token)

    send_mail(
        subject="Confirm account deactivation — Gerlyn Variety Store",
        message=(
            f"Hi {user.get_full_name() or user.username},\n\n"
            f"We received a request to deactivate your account. "
            f"Two-factor authentication is enabled, so we need you to confirm "
            f"this here:\n\n"
            f"{url}\n\n"
            f"This link expires in {VerificationToken.EXPIRY_HOURS} hours and can "
            f"only be used once. You'll still need to enter your password and "
            f"confirm again on that page before anything happens.\n\n"
            f"If you didn't request this, ignore this email — your account will stay active.\n\n"
            f"— Gerlyn Variety Store"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def deactivate_account(user):
    """
    Deactivates (never hard-deletes) the account: is_active=False blocks
    login immediately, and PII is scrubbed so the original email/username
    become free to register again. The User row itself, and everything
    that references it (orders, billing records), stays intact — nothing
    is orphaned, it's just no longer personally identifiable.
    """
    import uuid
    from .models import Address

    placeholder = uuid.uuid4().hex

    user.is_active = False
    user.email = f"deactivated-{placeholder}@deactivated.invalid"
    user.username = f"deactivated-{placeholder[:20]}"
    user.first_name = ''
    user.last_name = ''
    user.pending_email = None
    user.is_email_verified = False
    if user.avatar:
        user.avatar.delete(save=False)
    user.save()

    role_profile = user.get_role_profile()
    if role_profile is not None:
        if hasattr(role_profile, 'contact_number'):
            role_profile.contact_number = ''
        if hasattr(role_profile, 'address'):
            role_profile.address = ''
        if hasattr(role_profile, 'first_name'):
            role_profile.first_name = ''
        if hasattr(role_profile, 'last_name'):
            role_profile.last_name = ''
        role_profile.save()

    Address.objects.filter(user=user).delete()
    revoke_other_sessions(user, keep_session_key=None)


def send_email_change_verification(user, new_email, request):
    """
    Sends verification to the NEW address, then stores pending_email only
    once that send actually succeeds. User.email is NOT changed yet — only
    after the link is clicked.

    Order matters here: if send_mail() raises, we must not have already
    committed pending_email — update_profile()'s duplicate check filters on
    that field, so a single SMTP hiccup would otherwise permanently lock the
    address out (including for whoever originally requested it) until
    someone manually clears it.
    """
    VerificationToken.objects.filter(
        user=user, purpose=VerificationToken.PURPOSE_EMAIL_CHANGE, is_used=False
    ).delete()

    token = VerificationToken.objects.create(
        user=user, purpose=VerificationToken.PURPOSE_EMAIL_CHANGE
    )
    url = _build_url(request, 'accounts:verify_email_change', token)

    send_mail(
        subject="Confirm your new email — Gerlyn Variety Store",
        message=(
            f"Hi {user.get_full_name() or user.username},\n\n"
            f"Click below to confirm {new_email} as your new email.\n"
            f"This link expires in {VerificationToken.EXPIRY_HOURS} hours.\n\n"
            f"{url}\n\n"
            f"If you didn't request this, ignore this email.\n\n"
            f"— Gerlyn Variety Store"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[new_email],
        fail_silently=False,
    )

    user.pending_email = new_email
    user.save(update_fields=['pending_email'])