# accounts/services.py
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse
from .models import VerificationToken, NotificationPreference


def _build_url(request, view_name, token):
    relative = reverse(view_name, kwargs={'token': str(token.token)})
    return request.build_absolute_uri(relative)


def send_staff_invite(user, request):
    """
    Sends the invite link for a just-created internal account (Owner clicked
    Add User with only email/role/branch) — user is already saved, inactive,
    with an unusable password. The link leads to a register-style page
    where the invited person picks their own username and password; email
    stays locked to what the Owner entered. Deletes any earlier unused
    invite for this account first, same pattern as the other verification
    emails, so an old link can't be used once a fresh one is sent.
    """
    VerificationToken.objects.filter(
        user=user, purpose=VerificationToken.PURPOSE_STAFF_INVITE, is_used=False
    ).delete()

    token = VerificationToken.objects.create(
        user=user, purpose=VerificationToken.PURPOSE_STAFF_INVITE
    )
    url = _build_url(request, 'accounts:staff_invite_complete', token)

    send_mail(
        subject="You've been added to Gerlyn Variety Store",
        message=(
            f"Hi,\n\n"
            f"An account has been created for you at Gerlyn Variety Store. "
            f"Click the link below to set up your username and password:\n\n"
            f"{url}\n\n"
            f"This link expires in {VerificationToken.EXPIRY_HOURS} hours. "
            f"If you weren't expecting this, you can ignore this email.\n\n"
            f"— Gerlyn Variety Store"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


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
    Self-service deactivation: is_active=False blocks login immediately,
    and every other session is revoked, but nothing is erased. Reversible —
    the Owner can flip is_active back on (User Management, or a customer
    accounts view) and the account is exactly as it was, same email and
    username. This is deliberately NOT where personal data is removed; that
    is hard_delete_personal_data below, a distinct, Owner-triggered action
    for an actual privacy request, not something that happens to everyone
    who deactivates for an ordinary reason.
    """
    user.is_active = False
    user.save(update_fields=['is_active'])
    revoke_other_sessions(user, keep_session_key=None)


def hard_delete_personal_data(user, performed_by):
    """
    Erases this account's identity — email, username, name, contact number,
    profile address, avatar, saved Address rows — in response to an actual
    deletion request (received outside the system, by email, per how this
    store handles them). Deliberately NOT a row delete: everything the
    account ever DID (sales, stock movements, billing confirmations) stays
    exactly where it is, still linked by this row's immutable id — only the
    fields that identify a person go blank. deleted_at/deleted_by on the
    row itself are the entire accountability record this needs; there is no
    in-system "request" to log since it never arrives through the app.

    Always also deactivates the account (a hard-deleted identity has no
    business being logged into), on top of whatever deactivate_account()
    may or may not have already done to it.

    Also used as "cancel this invite" for a still-pending account (Add User
    was used, but the link was never completed) — in that case there is
    barely any personal data on the row yet besides the email, but the
    outstanding VerificationToken matters a lot: without deleting it here,
    the emailed link keeps working against a row whose email has just been
    overwritten with a placeholder, landing the invited person on a setup
    page for an account that no longer identifies anyone. Any token type
    (email verify, password change, a pending invite) is equally wrong to
    leave alive once the identity it was emailed to has been erased.
    """
    import uuid
    from django.utils import timezone
    from .models import Address, VerificationToken

    placeholder = uuid.uuid4().hex

    user.is_active = False
    user.email = f"deleted-{placeholder}@deleted.invalid"
    user.username = f"deleted-{placeholder[:20]}"
    user.first_name = ''
    user.last_name = ''
    user.pending_email = None
    user.is_email_verified = False
    if user.avatar:
        user.avatar.delete(save=False)
    user.deleted_at = timezone.now()
    user.deleted_by = performed_by
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
    VerificationToken.objects.filter(user=user).delete()
    revoke_other_sessions(user, keep_session_key=None)


def cancel_pending_invite(user):
    """
    Cancels an invite that was never completed (Add User was used, but the
    invited person never set up a username/password — has_usable_password()
    is still False). Unlike hard_delete_personal_data, this actually deletes
    the row: there is no accountability reason to keep a husk around for an
    account nothing was ever done through (no sales, no logs — the row's
    OneToOne role profile and any VerificationToken cascade-delete with it,
    same as they always would for any User.delete()). No audit trail exists
    yet to record who cancelled it, so for now this is just a delete; the
    caller is trusted to only call this for a still-pending, Owner-only
    action anyway.
    """
    if user.avatar:
        user.avatar.delete(save=False)
    user.delete()


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


def contact_number_taken(number, exclude_user=None):
    """True if `number` is already saved on another role profile — checked
    across all five profile tables since a contact number isn't scoped to
    one role. `exclude_user` leaves the caller's own profile out (self-edit
    and admin-edit), left as None for a brand-new registration where no
    profile owns it yet."""
    from .models import (
        BranchManagerProfile, CustomerProfile, OwnerProfile,
        StaffProfile, SupplierProfile,
    )
    querysets = [
        BranchManagerProfile.objects.filter(contact_number=number),
        StaffProfile.objects.filter(contact_number=number),
        CustomerProfile.objects.filter(contact_number=number),
        OwnerProfile.objects.filter(contact_number=number),
        SupplierProfile.objects.filter(contact_number=number),
    ]
    if exclude_user is not None:
        querysets = [qs.exclude(user=exclude_user) for qs in querysets]
    return any(qs.exists() for qs in querysets)


def set_contact_number(role_profile, new_number, owner):
    """Validates (PH mobile format + cross-profile uniqueness) and saves
    `new_number` onto `role_profile.contact_number`. Raises
    django.core.exceptions.ValidationError with a user-facing message on
    either failure, otherwise saves and returns None. `owner` is the User
    the uniqueness check excludes (their own existing number isn't a
    conflict with itself)."""
    from django.core.exceptions import ValidationError
    from .validators import validate_ph_mobile_number

    validate_ph_mobile_number(new_number)
    if contact_number_taken(new_number, exclude_user=owner):
        raise ValidationError("That contact number is already in use.")

    role_profile.contact_number = new_number
    role_profile.save(update_fields=['contact_number'])