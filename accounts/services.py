# accounts/services.py
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse
from .models import VerificationToken


def _build_url(request, view_name, token):
    relative = reverse(view_name, kwargs={'token': str(token.token)})
    return request.build_absolute_uri(relative)


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
    url = _build_url(request, 'verify_email', token)

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
    url = _build_url(request, 'verify_email_change', token)

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