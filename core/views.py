from django.shortcuts import render

from core.permissions import INTERNAL_ROLES, role_required

from .changelog import visible_changelog
from .version import APP_VERSION


def service_worker_view(request):
    """Serves the service worker at /sw.js (root path, not /static/js/) so
    its default scope covers the whole site instead of just /static/js/."""
    return render(request, 'sw.js', content_type='application/javascript; charset=utf-8')


def download_view(request):
    """One shared page (same URL, same data, same partials — see
    templates/partials/download_*.html) rendered with whichever chrome
    fits the visitor: internal admin panel for staff/branch managers/
    owners, storefront chrome for everyone else."""
    is_internal = request.user.is_authenticated and request.user.role in INTERNAL_ROLES
    context = {
        'changelog': visible_changelog(is_internal),
        'current_version': APP_VERSION,
    }
    if is_internal:
        context['active_page'] = 'download'
        return render(request, 'download_internal.html', context)
    return render(request, 'download.html', context)


@role_required(*INTERNAL_ROLES)
def internal_settings_view(request):
    """Staff/branch-manager/owner settings landing page — distinct from the
    customer Settings hub (accounts:profile and friends), which is styled
    for the storefront and not meant for internal use. Password and
    Sessions controls are inline on this page (not links to the customer
    pages) so "Manage" never takes the user out of the admin layout.
    Store-level configuration (branch defaults, thresholds, etc.) is
    intentionally left as a "coming soon" section until that scope is
    defined."""
    from accounts.views import _internal_settings_context

    return render(request, 'internal_settings.html', _internal_settings_context(request))
