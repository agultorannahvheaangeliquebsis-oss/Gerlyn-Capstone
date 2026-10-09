import logging

from django import forms
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template import loader

from .services import revoke_other_sessions


email_logger = logging.getLogger('accounts.email')


class PasswordResetRequestForm(PasswordResetForm):
    """Django's reset request form with GerlynPLUS UI and safe delivery errors."""

    email = forms.EmailField(
        label='Email address',
        max_length=254,
        widget=forms.EmailInput(attrs={
            'autocomplete': 'email',
            'autocapitalize': 'none',
            'placeholder': ' ',
        }),
    )

    def send_mail(
        self,
        subject_template_name,
        email_template_name,
        context,
        from_email,
        to_email,
        html_email_template_name=None,
    ):
        """Keep delivery failures private and never log the reset URL/token."""
        try:
            subject = loader.render_to_string(subject_template_name, context)
            # Email headers may never contain a newline.
            subject = ''.join(subject.splitlines())
            body = loader.render_to_string(email_template_name, context)
            message = EmailMultiAlternatives(subject, body, from_email, [to_email])
            if html_email_template_name is not None:
                html_body = loader.render_to_string(html_email_template_name, context)
                message.attach_alternative(html_body, 'text/html')
            return message.send()
        except Exception as exc:
            user = context.get('user')
            email_logger.error(
                'Password reset email delivery failed user_id=%s error_type=%s',
                getattr(user, 'pk', 'unknown'),
                type(exc).__name__,
            )
            # The public response must be identical for an unknown address and
            # an address whose provider is temporarily unavailable.
            return None


class PasswordResetSetPasswordForm(SetPasswordForm):
    """Apply configured validators and end every pre-reset login session."""

    def save(self, commit=True):
        if not commit:
            return super().save(commit=False)

        # Password and session invalidation succeed or fail as one operation.
        with transaction.atomic():
            user = super().save(commit=True)
            revoke_other_sessions(user, keep_session_key=None)
        return user
