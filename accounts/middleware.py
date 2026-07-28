from datetime import timedelta

from django.utils import timezone


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

    Tracked both before AND after the view: pre-view so a view like
    Settings > Sessions sees its OWN session already recorded (an
    already-authenticated request has a session_key from the moment
    SessionMiddleware loads the cookie, before this even runs); post-view
    to catch the login request itself, where request.session.session_key
    only becomes real partway through the view once login() runs.
    """

    TOUCH_INTERVAL = timedelta(minutes=1)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.session.session_key:
            self._track(request)
        response = self.get_response(request)
        if request.user.is_authenticated and request.session.session_key:
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
