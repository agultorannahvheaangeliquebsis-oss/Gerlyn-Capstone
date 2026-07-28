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
