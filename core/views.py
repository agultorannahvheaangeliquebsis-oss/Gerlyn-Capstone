import time

from django.contrib import messages
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.permissions import INTERNAL_ROLES, OWNER, role_required

from .changelog import visible_changelog
from .version import APP_VERSION


def health_check(request):
    """Unauthenticated liveness probe for Railway's healthcheckPath
    (railway.json). Deliberately does not touch the database: migrate
    already ran before gunicorn starts (see railway.json startCommand), so a
    DB check here would only add a way for a transient MySQL blip to fail a
    deploy's health check and trigger an unnecessary restart. It exists to
    confirm the WSGI process itself is up and serving.
    """
    return JsonResponse({'status': 'ok'})


def service_worker_view(request):
    """Serves the service worker at /sw.js (root path, not /static/js/) so
    its default scope covers the whole site instead of just /static/js/."""
    return render(request, 'sw.js', content_type='application/javascript; charset=utf-8')


def about_view(request):
    """The storefront's public About Us page, sitting in the shop nav beside
    Products.

    Distinct from accounts.views.about_view, which is the same store facts
    rendered inside the logged-in Settings shell (account sidebar, Settings
    breadcrumb) and reached from there. This one is the page a shopper who
    has never signed in lands on, so it carries the storefront chrome and the
    branch list rather than a settings sidebar.

    Branches come from the database rather than a hardcoded list — the footer
    used to name three and would have gone stale the moment a fourth opened.
    """
    from branches.models import Branch

    return render(request, 'about.html', {'branches': Branch.objects.all()})


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


# =============================================================================
# OWNER NOTIFICATION CENTER
# =============================================================================
# Cross-department feed of everything core.notifications.notify() has raised
# for this user — see that module for what raises these and
# templates/core/notification_center.html for the panel/filter UI. Reuses
# accounts.models.Notification (the same model the customer-facing inbox at
# accounts:notifications already uses) rather than a second table; `category`
# blank is what keeps that plain customer inbox from showing up here.

@role_required(*INTERNAL_ROLES)
def notification_center_view(request):
    from accounts.models import Notification

    notifications = request.user.notifications.exclude(category='').select_related('branch')

    counts = {
        'staff': notifications.filter(category=Notification.Category.STAFF).count(),
        'procurement': notifications.filter(category=Notification.Category.PROCUREMENT).count(),
        'sales': notifications.filter(category=Notification.Category.SALES).count(),
        'system': notifications.filter(category=Notification.Category.SYSTEM).count(),
    }

    return render(request, 'core/notification_center.html', {
        # A feed, not a table — capped rather than paginated, matching the
        # rest of the app (see feedback-gerlyn-conventions: no pagination
        # anywhere yet at current data volume).
        'notifications': notifications[:200],
        'total_count': notifications.count(),
        'unread_count': notifications.filter(is_read=False).count(),
        'needs_action_count': notifications.filter(
            requires_action=True, resolved_at__isnull=True
        ).count(),
        'counts': counts,
        'active_page': 'notifications',
    })


@require_POST
@role_required(*INTERNAL_ROLES)
def notification_mark_read(request, pk):
    notification = get_object_or_404(request.user.notifications, pk=pk)
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=['is_read'])
    return redirect('notification_center')


@require_POST
@role_required(*INTERNAL_ROLES)
def notification_resolve(request, pk):
    """Marks the alert acted-on — the action button, distinct from just
    opening/reading it. The topbar badge
    (core.context_processors.internal_notifications) only counts unresolved
    High/Critical items, not raw unread count, so opening a card doesn't
    silently clear it — only this does."""
    notification = get_object_or_404(request.user.notifications, pk=pk)
    if not notification.resolved_at:
        notification.resolve()
        messages.success(request, 'Marked as resolved.')
    return redirect('notification_center')


@require_POST
@role_required(*INTERNAL_ROLES)
def notification_mark_all_read(request):
    updated = request.user.notifications.exclude(category='').filter(
        is_read=False
    ).update(is_read=True)
    if updated:
        messages.success(
            request,
            f'Marked {updated} notification{"" if updated == 1 else "s"} as read.',
        )
    else:
        messages.info(request, 'You have no unread notifications.')
    return redirect('notification_center')
