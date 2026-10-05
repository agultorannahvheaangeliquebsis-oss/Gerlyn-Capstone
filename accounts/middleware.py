from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

from core.module_permissions import (
    MODULE_ACTION_LABELS,
    REPORT_ROLES,
    REPORTS,
    URL_NAME_TO_MODULE,
    required_action_for_request,
    user_can_perform_module_action,
)


class NoCacheForAuthenticatedMiddleware:
    """Stops the browser from caching pages rendered for a logged-in user.

    Without this, pressing Back after logout (or after an idle timeout)
    can redisplay a cached copy of a protected page straight from the
    browser's bfcache/disk cache — no request ever reaches Django, so
    login_required never gets a chance to reject it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(request, 'user', None) and request.user.is_authenticated:
            response['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'
        return response


class ModulePermissionMiddleware:
    """Centrally enforce the module/action registry for resolved views.

    The URL name identifies the module and action; shared REST routes also
    use the HTTP method to distinguish View/Add/Edit/Delete. Dashboards,
    account settings, and Owner-only administration stay outside this
    module system. Only Staff and Branch Managers are restricted here.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return None
        if user.role not in ('STAFF', 'BRANCH_MANAGER'):
            return None

        url_name = request.resolver_match.url_name if request.resolver_match else None
        module_code = URL_NAME_TO_MODULE.get(url_name)
        if module_code is None:
            return None

        # Management reports are never a Staff module, whatever grants the
        # account carries: answer with the styled 403 page (not a flash
        # redirect that would imply the Owner could unlock it).
        if (
            module_code == REPORTS
            and user.role not in REPORT_ROLES
            and not request.path.startswith('/api/')
        ):
            raise PermissionDenied('Reports are limited to the Owner and Branch Managers.')

        action = required_action_for_request(url_name, request.method)
        if not user_can_perform_module_action(user, module_code, action):
            if request.path.startswith('/api/'):
                return JsonResponse(
                    {'detail': (
                        f'You do not have {MODULE_ACTION_LABELS[action]} '
                        'permission for this module.'
                    )},
                    status=403,
                )
            messages.error(
                request,
                f"You don't have {MODULE_ACTION_LABELS[action]} permission "
                "for that section. Ask your Owner to grant it if you need it."
            )
            from .views import redirect_by_role
            return redirect_by_role(request, user)
        return None


class LanguageMiddleware:
    """Sets request.LANGUAGE for the {% t %} template tag.

    Signed-in users: their saved Settings > Language preference, so it
    follows them across devices. Anonymous visitors: whatever they last
    picked this session, if anything. Either way, the fallback is always
    English — there's no way to end up in a language nobody chose.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and getattr(request.user, 'language', None):
            request.LANGUAGE = request.user.language
        else:
            request.LANGUAGE = request.session.get('language', 'en')
        return self.get_response(request)


class SessionTrackingMiddleware:
    """Keeps accounts.UserSession in sync with real Django sessions, so
    Settings > Sessions can list them without scanning django_session.

    Checked both before AND after the view: pre-view so a view like
    Settings > Sessions sees its OWN session already recorded (an
    already-authenticated request has a session_key from the moment
    SessionMiddleware loads the cookie, before this even runs); post-view
    to catch the login request itself, where request.session.session_key
    only becomes real partway through the view once login() runs.

    Only actually hits the database once for an ordinary already-
    authenticated request, though: the post-view check is skipped when the
    session key hasn't changed since pre-view, since that means pre-view
    already tracked it. Without this, every single authenticated page view
    on the site paid for two get_or_create() round trips against MySQL
    instead of one — a fixed tax on every click, independent of whatever
    the view itself needed to do.
    """

    TOUCH_INTERVAL = timedelta(minutes=1)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session_key_before = None
        if request.user.is_authenticated and request.session.session_key:
            session_key_before = request.session.session_key
            self._track(request)
        response = self.get_response(request)
        if request.user.is_authenticated and request.session.session_key:
            if request.session.session_key != session_key_before:
                self._track(request)
        return response

    def _track(self, request):
        from .models import UserSession

        session_key = request.session.session_key
        ip_address = request.META.get('REMOTE_ADDR')
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:255]
        now = timezone.now()

        obj, created = UserSession.objects.get_or_create(
            session_key=session_key,
            defaults={
                'user': request.user,
                'ip_address': ip_address,
                'user_agent': user_agent,
                'last_active': now,
            },
        )
        if not created and now - obj.last_active > self.TOUCH_INTERVAL:
            obj.last_active = now
            obj.ip_address = ip_address
            obj.save(update_fields=['last_active', 'ip_address'])
