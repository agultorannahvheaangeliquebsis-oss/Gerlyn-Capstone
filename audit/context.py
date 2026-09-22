"""Who is making the current change.

The audit signal handlers run deep inside a model save, far from the request
that caused it, so the middleware parks the request in a context variable and
the handlers read the actor back out. The user is resolved at write time, not
when the request starts: some views log the user in part-way through.
"""
import contextvars
import uuid
from contextlib import ContextDecorator

_request = contextvars.ContextVar('audit_request', default=None)
_request_id = contextvars.ContextVar('audit_request_id', default='')
_suppressed = contextvars.ContextVar('audit_suppressed', default=False)
_action_label = contextvars.ContextVar('audit_action_label', default='')


def bind(request):
    return _request.set(request), _request_id.set(uuid.uuid4().hex), _action_label.set('')


def unbind(tokens):
    request_token, id_token, label_token = tokens
    _request.reset(request_token)
    _request_id.reset(id_token)
    _action_label.reset(label_token)


def set_action_label(label):
    """Name the human-facing action about to happen, before the view/service
    performs the writes that will produce AuditLog rows for it — e.g.
    'Deleted a user account'. Every row capture.py's _write() creates for
    the rest of this request carries it, so the Audit Log's grouped list
    can show this instead of the first affected row's raw field-diff
    summary. Always generic/non-personal, same masking philosophy as
    registry.py's mask= fields — never say who, only what kind of action.
    """
    _action_label.set(label)


class suppressed(ContextDecorator):
    """Switch capture off for a block or a whole function — bulk seeding and
    the like, where thousands of rows written by a script are noise, not
    history. Usable as `with suppressed():` or `@suppressed()`."""

    def __enter__(self):
        self._token = _suppressed.set(True)
        return self

    def __exit__(self, *exc):
        _suppressed.reset(self._token)
        return False


def is_suppressed():
    return _suppressed.get()


def _client_ip(request):
    # Same best-effort real-IP rule the login rate limiter uses behind Railway.
    from accounts.rate_limit import get_client_ip
    return get_client_ip(request) or None


def current():
    """{actor_id, actor_role, source, ip_address, request_id, action_label}
    for this moment."""
    request = _request.get()
    if request is None:
        return {
            'actor_id': None, 'actor_role': '', 'source': 'system',
            'ip_address': None, 'request_id': '',
            'action_label': _action_label.get(),
        }
    user = getattr(request, 'user', None)
    authenticated = bool(user is not None and user.is_authenticated)
    return {
        'actor_id': user.pk if authenticated else None,
        'actor_role': getattr(user, 'role', '') if authenticated else '',
        'source': 'request',
        'ip_address': _client_ip(request),
        'request_id': _request_id.get(),
        'action_label': _action_label.get(),
    }
