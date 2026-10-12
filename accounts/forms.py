import logging

from django import forms
from django.contrib.auth.forms import AdminUserCreationForm, PasswordResetForm, SetPasswordForm, UserChangeForm
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template import loader

from .services import revoke_other_sessions
from .models import User


email_logger = logging.getLogger('accounts.email')


class AccountAdminInputMixin:
    """Give Django admin the same browser limits as the role-specific UI."""

    class Media:
        js = ('js/input-limits.js',)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        customer = (self.data.get('role') or self.instance.role) == User.Role.CUSTOMER
        for name in ('first_name', 'last_name'):
            if name in self.fields:
                self.fields[name].widget.attrs.update({
                    'maxlength': 50 if customer else 100,
                    'data-account-input': name,
                    'pattern': r"(?=.*\p{L})[\p{L}\p{M} .'\u2019\-]+",
                })
                self.fields[name].required = customer
        if 'email' in self.fields:
            self.fields['email'].required = customer
        if 'role' in self.fields:
            self.fields['role'].widget.attrs['data-account-role'] = '1'
        for name in ('password1', 'password2'):
            if name in self.fields:
                self.fields[name].widget.attrs.update(minlength=8, maxlength=128)
        if 'password1' in self.fields:
            self.fields['password1'].widget.attrs['data-account-password'] = '1'


class AccountAdminCreationForm(AccountAdminInputMixin, AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'role')


class AccountAdminChangeForm(AccountAdminInputMixin, UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ('new_password1', 'new_password2'):
            self.fields[name].widget.attrs.update(minlength=8, maxlength=128)
        self.fields['new_password1'].widget.attrs['data-account-password'] = '1'

    def save(self, commit=True):
        if not commit:
            return super().save(commit=False)

        # Password and session invalidation succeed or fail as one operation.
        with transaction.atomic():
            user = super().save(commit=True)
            revoke_other_sessions(user, keep_session_key=None)
        return user
