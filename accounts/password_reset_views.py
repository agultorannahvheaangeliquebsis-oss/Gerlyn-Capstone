import hashlib
import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from .forms import PasswordResetRequestForm, PasswordResetSetPasswordForm
from .rate_limit import (
    PASSWORD_RESET_EMAIL_MAX_ATTEMPTS,
    PASSWORD_RESET_IP_MAX_ATTEMPTS,
    PASSWORD_RESET_WINDOW_SECONDS,
    get_client_ip,
    is_rate_limited,
)


email_logger = logging.getLogger('accounts.email')


def _reset_email_origin(request):
    """Return the configured public domain and whether reset links use HTTPS."""
    if settings.PUBLIC_BASE_URL:
        configured = urlsplit(settings.PUBLIC_BASE_URL)
        return configured.netloc, configured.scheme == 'https'

    # In production, links must still be HTTPS when PUBLIC_BASE_URL is left
    # blank. SECURE_PROXY_SSL_HEADER makes request.is_secure() accurate behind
    # Railway and other supported reverse proxies.
    return request.get_host(), request.is_secure() or not settings.DEBUG


def _rate_limit_identifier(value):
    """Avoid retaining an email address or IP address in a cache key."""
    return hashlib.sha256(value.strip().casefold().encode('utf-8')).hexdigest()


def _password_reset_rate_limited(request, email):
    email_limited = is_rate_limited(
        'password-reset-email',
        _rate_limit_identifier(email),
        PASSWORD_RESET_EMAIL_MAX_ATTEMPTS,
        PASSWORD_RESET_WINDOW_SECONDS,
    )
    ip_limited = is_rate_limited(
        'password-reset-ip',
        _rate_limit_identifier(get_client_ip(request) or 'unknown'),
        PASSWORD_RESET_IP_MAX_ATTEMPTS,
        PASSWORD_RESET_WINDOW_SECONDS,
    )
    return email_limited or ip_limited


@sensitive_post_parameters('email')
@never_cache
@require_http_methods(['GET', 'POST'])
def forgot_password_view(request):
    """Request a reset without disclosing whether the address is registered."""
    request_sent = request.GET.get('sent') == '1'

    if request.method == 'POST':
        form = PasswordResetRequestForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            if not _password_reset_rate_limited(request, email):
                domain, use_https = _reset_email_origin(request)
                try:
                    form.save(
                        domain_override=domain,
                        use_https=use_https,
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        subject_template_name='accounts/emails/password_reset_subject.txt',
                        email_template_name='accounts/emails/password_reset_email.txt',
                        html_email_template_name='accounts/emails/password_reset_email.html',
                        request=request,
                        extra_email_context={'site_name': 'GerlynPLUS'},
                    )
                except Exception as exc:
                    # PasswordResetRequestForm already handles provider errors.
                    # This final boundary protects the non-enumerating response
                    # from a template/configuration failure without logging PII
                    # or a token-bearing URL.
                    email_logger.error(
                        'Password reset request failed error_type=%s',
                        type(exc).__name__,
                    )
            return redirect(f"{reverse('accounts:forgot_password')}?sent=1")
    else:
        form = PasswordResetRequestForm()

    return render(request, 'accounts/forgot_password.html', {
        'form': form,
        'request_sent': request_sent,
    })


class GerlynPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = 'accounts/reset_password.html'
    form_class = PasswordResetSetPasswordForm
    success_url = reverse_lazy('accounts:password_reset_complete')
    post_reset_login = False


class GerlynPasswordResetCompleteView(TemplateView):
    template_name = 'accounts/password_reset_complete.html'
