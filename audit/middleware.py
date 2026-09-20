from . import context


class AuditContextMiddleware:
    """Makes the current request available to the audit signal handlers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tokens = context.bind(request)
        try:
            return self.get_response(request)
        finally:
            context.unbind(tokens)
