# accounts/email_backends.py
"""
Email delivery over HTTPS (port 443) for hosts that block outbound SMTP.

Railway disables ports 25/465/587 on its Free, Trial and Hobby plans, so
Django's SMTP backend can never reach Gmail/Brevo-SMTP from there. Brevo's
transactional API is plain HTTPS, needs only a verified sender address (no
owned domain), and is called with the standard library -- no new dependency.

Failures are logged to 'accounts.email' with the HTTP status and the
provider's error code only. The API key and the request/response bodies are
never logged or placed in the raised exception text.
"""
import json
import logging
import socket
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

email_logger = logging.getLogger('accounts.email')

BREVO_SEND_URL = 'https://api.brevo.com/v3/smtp/email'


class EmailDeliveryError(RuntimeError):
    """Delivery failed. The message is safe to log; it holds no secrets."""


class BrevoApiEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.api_key = getattr(settings, 'BREVO_API_KEY', '')
        self.timeout = getattr(settings, 'EMAIL_TIMEOUT', 10)

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        sent = 0
        for message in email_messages:
            try:
                self._send(message)
            except EmailDeliveryError:
                if not self.fail_silently:
                    raise
            else:
                sent += 1
        return sent

    def _send(self, message):
        if not self.api_key:
            email_logger.error('Brevo API email not sent: BREVO_API_KEY is not set.')
            raise EmailDeliveryError('BREVO_API_KEY is not set.')

        recipients = [{'email': addr} for addr in message.recipients()]
        if not recipients:
            raise EmailDeliveryError('Message has no recipients.')

        from_name, from_addr = parseaddr(message.from_email or settings.DEFAULT_FROM_EMAIL)
        if not from_addr:
            email_logger.error('Brevo API email not sent: no valid sender address.')
            raise EmailDeliveryError('No valid sender address.')
        sender = {'email': from_addr}
        if from_name:
            sender['name'] = from_name

        payload = {
            'sender': sender,
            'to': recipients,
            'subject': message.subject,
        }
        html_body = next(
            (content for content, mimetype in getattr(message, 'alternatives', [])
             if mimetype == 'text/html'),
            None,
        )
        payload['textContent'] = message.body
        if html_body:
            payload['htmlContent'] = html_body

        request = urllib.request.Request(
            BREVO_SEND_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'api-key': self.api_key,
                'accept': 'application/json',
                'content-type': 'application/json',
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            provider_code = ''
            try:
                provider_code = str(json.loads(exc.read().decode('utf-8')).get('code', ''))[:60]
            except Exception:
                pass
            email_logger.error(
                'Brevo API rejected email status=%s provider_code=%s',
                exc.code, provider_code or 'unknown',
            )
            raise EmailDeliveryError(f'Brevo API returned HTTP {exc.code}.') from None
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            email_logger.error(
                'Brevo API unreachable error_type=%s', type(exc).__name__,
            )
            raise EmailDeliveryError('Brevo API unreachable.') from None

        if status not in (200, 201, 202):
            email_logger.error('Brevo API unexpected status=%s', status)
            raise EmailDeliveryError(f'Brevo API returned HTTP {status}.')
