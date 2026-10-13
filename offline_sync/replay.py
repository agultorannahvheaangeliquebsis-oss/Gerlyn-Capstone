"""Replay one queued form submission through the real view, as its user.

Why replay the view instead of re-implementing it: the walk-in sale, the
refill request, the count correction, the purchase order and the delivery
receipt each carry validation, pricing, branch scoping and stock rules that
took a long time to get right. A second copy of them for "offline" would
drift. Replaying the view guarantees a queued change is held to exactly the
rules an online submission is -- including the per-user module permissions,
which live in middleware and are therefore re-applied here explicitly.

The view is called directly (no HTTP round trip) with a request built from the
sync request: same user, same session, same client address. Anything the view
says through django.contrib.messages is collected instead of flashed, and the
final answer is decided by `interpret()`.
"""
from dataclasses import dataclass, field
from io import BytesIO

from django.contrib.messages import constants as message_levels
from django.core.exceptions import PermissionDenied
from django.core.handlers.wsgi import WSGIRequest
from django.http import Http404
from django.urls import Resolver404, resolve, reverse

# Views that re-render a form on a validation error (instead of flashing the
# errors and redirecting) leave their reasons here so the sync endpoint can
# report them. Setting the attribute is harmless on an ordinary request.
REJECTION_ATTR = 'offline_rejection_errors'

_REDIRECT_CODES = (301, 302, 303, 307, 308)


class _CollectedMessages:
    """Quacks like a messages storage; keeps what the view tried to say."""

    level = message_levels.INFO

    def __init__(self):
        self.items = []

    def add(self, level, message, extra_tags=''):
        self.items.append((level, str(message)))

    def __iter__(self):
        return iter(())

    def __len__(self):
        return len(self.items)


@dataclass
class Outcome:
    status: str                      # 'APPLIED' | 'REJECTED' | 'CONFLICT'
    message: str = ''
    result: dict = field(default_factory=dict)
    current: dict = None


def build_inner_request(outer, path, post, files):
    environ = dict(outer.META)
    environ.update({
        'PATH_INFO': path,
        'REQUEST_METHOD': 'POST',
        'QUERY_STRING': '',
        'CONTENT_LENGTH': '0',
        'CONTENT_TYPE': 'application/x-www-form-urlencoded',
        'wsgi.input': BytesIO(b''),
    })
    inner = WSGIRequest(environ)
    inner.POST = post
    inner._files = files
    inner.user = outer.user
    inner.session = outer.session
    inner.LANGUAGE = getattr(outer, 'LANGUAGE', 'en')
    inner._messages = _CollectedMessages()
    return inner


def _first(texts):
    seen = []
    for text in texts:
        if text and text not in seen:
            seen.append(text)
    return seen


def run_view(outer, spec, post, files, target_pk, op_id, started_at):
    """Run the view for `spec` and decide what happened. Never raises for a
    *business* refusal -- only for a genuine server fault, which the caller
    turns into a retryable error (the ledger row stays pending)."""
    from accounts.middleware import ModulePermissionMiddleware

    kwargs = {'pk': target_pk} if spec.needs_target else {}
    try:
        path = reverse(spec.url_name, kwargs=kwargs)
        match = resolve(path)
    except Resolver404:  # pragma: no cover - registry/url drift
        return Outcome('REJECTED', 'That action is no longer available.')

    inner = build_inner_request(outer, path, post, files)
    inner.resolver_match = match
    store = inner._messages
    baseline = None  # taken once permissions pass, just before the view writes

    # Per-user View/Add/Edit/Delete grants are enforced in this middleware,
    # not in the views. Reuse it so queued changes cannot slip past them.
    try:
        denied = ModulePermissionMiddleware(lambda request: None).process_view(
            inner, match.func, match.args, match.kwargs,
        )
    except PermissionDenied as exc:
        # The middleware refuses a missing grant by raising; that is a clean
        # "no", not a server fault that should leave the change queued forever.
        return Outcome(
            'REJECTED',
            str(exc) or 'Your account no longer has permission for this action.',
        )
    if denied is not None:
        reasons = [text for level, text in store.items if level >= message_levels.ERROR]
        return Outcome(
            'REJECTED',
            ' '.join(reasons) or 'Your account no longer has permission for this action.',
        )

    if spec.baseline:
        baseline = spec.baseline(inner.user, post, target_pk)

    try:
        response = match.func(inner, *match.args, **match.kwargs)
    except Http404:
        return Outcome(
            'REJECTED',
            'The record this change refers to no longer exists or is not '
            'available to your account.',
        )
    except PermissionDenied as exc:
        return Outcome('REJECTED', str(exc) or 'You do not have permission for this action.')

    return interpret(response, inner, spec, op_id, started_at, post, target_pk, baseline)


def interpret(response, inner, spec, op_id, started_at, post, target_pk, baseline=None):
    store = inner._messages
    errors = [text for level, text in store.items if level >= message_levels.ERROR]
    errors += list(getattr(inner, REJECTION_ATTR, None) or [])
    errors = _first(errors)
    if errors:
        return Outcome('REJECTED', ' '.join(errors))

    status = getattr(response, 'status_code', 0)
    if status not in _REDIRECT_CODES:
        # The form was handed back instead of being saved.
        return Outcome(
            'REJECTED',
            'The server did not accept this change. Open it online and check the details.',
        )

    location = response.get('Location', '')
    login_path = reverse('accounts:login')
    if location.split('?')[0].startswith(login_path):
        return Outcome(
            'REJECTED',
            'Your session is no longer valid for this action. Sign in again.',
        )

    said = _first(text for level, text in store.items if level < message_levels.ERROR)
    if not said:
        # A redirect with nothing said is not a confirmation of anything.
        return Outcome('REJECTED', 'The server did not confirm that this change was saved.')

    verified = None
    if spec.verify:
        extra = {'baseline': baseline} if spec.baseline else {}
        verified = spec.verify(
            inner.user, op_id, location, started_at, post, target_pk, **extra,
        )
        if verified is None:
            # The view answered kindly ("already received", "already created")
            # but none of OUR entry's effects exist. That is the server state
            # having moved on, not a success -- never report it as synced.
            return Outcome(
                'CONFLICT',
                f"{' '.join(said)} This queued change was not applied.",
            )
    return Outcome('APPLIED', ' '.join(said), verified or {})
