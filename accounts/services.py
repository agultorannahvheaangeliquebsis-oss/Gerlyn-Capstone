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
    Stores pending_email and sends verification to the NEW address.
    User.email is NOT changed yet — only after the link is clicked.
    """
    user.pending_email = new_email
    user.save(update_fields=['pending_email'])

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