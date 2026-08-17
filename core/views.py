import time

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render

from core.permissions import INTERNAL_ROLES, OWNER, role_required

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


@role_required(OWNER)
def db_latency_check(request):
    """TEMPORARY diagnostic — remove once the Railway slow-page-load
    investigation is closed. Isolates raw MySQL round-trip time from
    everything else a normal page load pays for (gunicorn queueing,
    template rendering, session/alert queries), by timing N bare
    `SELECT 1` round trips on a connection Django already holds open
    (CONN_MAX_AGE=60 in settings.py) — the same connection reuse a real
    request gets. If each round trip below is fast (a few ms) but real
    pages are still slow, the bottleneck is elsewhere. If each one is
    itself hundreds of ms, that confirms it's the DB connection/network
    path, not query complexity or app code.
    """
    connect_start = time.perf_counter()
    with connection.cursor():
        pass  # forces a connection open if one wasn't already
    connect_ms = (time.perf_counter() - connect_start) * 1000

    round_trip_ms = []
    for _ in range(5):
        start = time.perf_counter()
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        round_trip_ms.append(round((time.perf_counter() - start) * 1000, 1))

    return JsonResponse({
        'first_connection_ms': round(connect_ms, 1),
        'select_1_round_trips_ms': round_trip_ms,
        'db_host': connection.settings_dict.get('HOST'),
        'db_port': connection.settings_dict.get('PORT'),
        'conn_max_age': connection.settings_dict.get('CONN_MAX_AGE'),
    })


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
