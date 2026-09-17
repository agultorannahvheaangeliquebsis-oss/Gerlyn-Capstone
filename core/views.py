from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from core.permissions import INTERNAL_ROLES, OWNER, role_required

from .changelog import visible_changelog
from .version import APP_VERSION


def healthz(request):
    """Railway's deploy health check (railway.json healthcheckPath). Cheap
    and unauthenticated on purpose — this confirms the gunicorn process
    came up and can serve a request at all, not that the DB is reachable.
    A DB check here would make a MySQL hiccup look like a bad deploy and
    trigger a restart-policy loop for the wrong reason."""
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


_SETTINGS_ANCHOR = "internal_settings"


def _back_to_settings(anchor="store-settings"):
    return redirect(f"{reverse('internal_settings')}#{anchor}")


@role_required(OWNER)
@require_POST
def store_settings_update(request):
    """Save the Store Settings card — legal identity, the public About Us
    copy, and the printed purchase-order / receipt layout.

    Owner only; Staff/Branch Managers see the card read-only. Every exit
    path leaves a message — a bare redirect would look the same whether the
    save worked or silently did nothing.
    """
    from core.models import StoreSetting

    s = StoreSetting.load()

    def _text(name):
        return (request.POST.get(name) or "").strip()

    # Optional logo. Validate it's a real image before storing — an
    # unreadable file would 500 the print page, not this form.
    logo = request.FILES.get("receipt_logo")
    if logo is not None:
        try:
            from PIL import Image

            Image.open(logo).verify()
            logo.seek(0)
        except Exception:
            messages.error(request, "That logo file isn't a readable image.")
            return _back_to_settings()
        s.receipt_logo = logo
    elif request.POST.get("receipt_logo_clear") == "1":
        s.receipt_logo = None

    s.tin = _text("tin")
    s.vat_registration = _text("vat_registration")
    s.about_hero_lead = _text("about_hero_lead")
    s.about_tagline = _text("about_tagline")
    s.about_story = _text("about_story")
    s.receipt_letterhead = _text("receipt_letterhead")
    s.receipt_header = _text("receipt_header")
    s.receipt_footer = _text("receipt_footer")
    s.receipt_thank_you = _text("receipt_thank_you")
    s.receipt_show_logo = request.POST.get("receipt_show_logo") == "1"
    s.receipt_show_tin = request.POST.get("receipt_show_tin") == "1"
    s.receipt_show_vat = request.POST.get("receipt_show_vat") == "1"
    s.save()

    # extra_tags='sheet' renders the prominent success sheet (with a Done
    # button) instead of the quiet top banner, so pressing Save gives a
    # confirmation the Owner can't miss.
    messages.success(request, "Store settings saved.", extra_tags="sheet")
    return _back_to_settings()


@role_required(OWNER)
@require_POST
def hero_slide_save(request):
    """Create or update one storefront hero card as an unpublished draft.
    `slide_id` blank = create. The change reaches visitors only when the
    Owner Applies it from the home-page preview (see hero_draft_apply)."""
    from core.models import HomeHeroSlide

    slide_id = (request.POST.get("slide_id") or "").strip()
    if slide_id:
        # is_shadow rows are internal snapshots, never edited directly.
        slide = HomeHeroSlide.objects.filter(pk=slide_id, is_shadow=False).first()
        if slide is None:
            messages.error(request, "That hero slide no longer exists.")
            return _back_to_settings("home-hero")
    else:
        slide = HomeHeroSlide()

    # "Keep this slide" on a slide the Owner had staged for removal.
    if request.POST.get("undo_delete") == "1":
        if slide.pk and slide.pending_delete:
            slide.pending_delete = False
            # If the removal was the only pending change, the shadow is a
            # duplicate and the slide goes back to fully published.
            if slide.drop_identical_shadow():
                slide.is_draft = False
            slide.save()
            messages.success(request, "Slide kept — it will not be removed.")
        return _back_to_settings("home-hero")

    headline = (request.POST.get("headline") or "").strip()
    body = (request.POST.get("body") or "").strip()
    if not headline or not body:
        messages.error(request, "A hero slide needs both a headline and a body line.")
        return _back_to_settings("home-hero")

    placement = request.POST.get("placement")
    if placement not in HomeHeroSlide.Placement.values:
        placement = HomeHeroSlide.Placement.ROTATOR
    icon = request.POST.get("icon")
    if icon not in HomeHeroSlide.Icon.values:
        icon = HomeHeroSlide.Icon.TAG

    cta_url = (request.POST.get("cta_url") or "").strip() or "/shop/"
    if not cta_url.startswith("/"):
        messages.error(request, "The slide link has to be a path on this site, starting with '/'.")
        return _back_to_settings("home-hero")

    try:
        sort_order = int(request.POST.get("sort_order") or 0)
    except ValueError:
        sort_order = 0

    # Freeze the live version before its first unpublished edit, so Cancel on
    # the preview can put it back exactly as it was.
    if slide.pk and not slide.is_draft:
        slide.freeze_shadow()
    slide.is_draft = True

    slide.placement = placement
    slide.icon = icon
    slide.eyebrow = (request.POST.get("eyebrow") or "").strip()
    slide.headline = headline
    slide.body = body
    slide.cta_label = (request.POST.get("cta_label") or "").strip()
    slide.cta_url = cta_url
    slide.sort_order = max(sort_order, 0)
    slide.is_active = request.POST.get("is_active") == "1"
    slide.save()

    messages.success(
        request,
        "Slide updated — preview the home page, then Apply to publish."
        if slide_id else
        "Slide added as a draft — preview the home page, then Apply to publish.",
    )
    return _back_to_settings("home-hero")


@role_required(OWNER)
@require_POST
def hero_slide_delete(request):
    """Stage one storefront hero card for removal. It stays live (through its
    frozen shadow) until the Owner Applies from the home-page preview. POST
    only (data-confirm form on the settings page); a GET is just the page."""
    from core.models import HomeHeroSlide

    slide = HomeHeroSlide.objects.filter(
        pk=request.POST.get("slide_id") or "", is_shadow=False,
    ).first()
    if slide is None:
        messages.error(request, "That hero slide no longer exists.")
        return _back_to_settings("home-hero")

    never_published = slide.is_draft and not HomeHeroSlide.objects.filter(
        shadow_of=slide,
    ).exists()
    if never_published:
        # Added and removed before it ever went live — nothing to stage.
        slide.delete()
        messages.success(request, "Draft slide discarded.")
    else:
        if not slide.is_draft:
            slide.freeze_shadow()
        slide.is_draft = True
        slide.pending_delete = True
        slide.save()
        messages.success(
            request,
            "Slide staged for removal — preview the home page, then Apply to confirm.",
        )
    return _back_to_settings("home-hero")


def _hero_return(request):
    """Apply / Cancel are triggered from two places — the preview bar on the
    storefront and the Settings page. A hidden `return_to` says which."""
    if request.POST.get("return_to") == "preview":
        return redirect("/?preview=1")
    return _back_to_settings("home-hero")


@role_required(OWNER)
@require_POST
def hero_draft_apply(request):
    """Publish every staged hero-slide change at once."""
    from core.models import HomeHeroSlide

    count = HomeHeroSlide.objects.filter(is_draft=True).count()
    if not count:
        messages.info(request, "No unpublished home-page changes to apply.")
        return _hero_return(request)

    # Confirmed removals: drop the working row (its shadow cascades away).
    HomeHeroSlide.objects.filter(is_draft=True, pending_delete=True).delete()
    # Edits and additions go live.
    HomeHeroSlide.objects.filter(is_draft=True).update(
        is_draft=False, pending_delete=False,
    )
    # Frozen snapshots are now stale.
    HomeHeroSlide.objects.filter(is_shadow=True).delete()

    messages.success(
        request,
        f"Published {count} change{'' if count == 1 else 's'} to the home page.",
    )
    return _hero_return(request)


@role_required(OWNER)
@require_POST
def hero_draft_discard(request):
    """Throw away every staged hero-slide change and keep the live page."""
    from core.models import HomeHeroSlide

    if not HomeHeroSlide.objects.filter(is_draft=True).exists():
        messages.info(request, "No unpublished home-page changes to discard.")
        return _hero_return(request)

    # Restore each edited / removed slide from its frozen shadow: drop the
    # working copy, promote the shadow back to a normal published row.
    for shadow in HomeHeroSlide.objects.filter(is_shadow=True).select_related("shadow_of"):
        working = shadow.shadow_of
        if working:
            working.delete()
        shadow.is_shadow = False
        shadow.shadow_of = None
        shadow.is_draft = False
        shadow.pending_delete = False
        shadow.save()
    # Whatever is still a draft was a brand-new addition — it never had a
    # published version to fall back to.
    HomeHeroSlide.objects.filter(is_draft=True).delete()

    messages.success(request, "Discarded the unpublished home-page changes.")
    return _hero_return(request)


# =============================================================================
# OWNER NOTIFICATION CENTER
# =============================================================================
# Cross-department feed of everything core.notifications.notify() has raised
# for this user — see that module for what raises these. Reuses
# accounts.models.Notification (the same model the customer-facing inbox at
# accounts:notifications already uses) rather than a second table; `category`
# blank is what keeps that plain customer inbox from showing up here.
#
# Two surfaces share one partial (templates/core/_notification_tray_list.html):
# a floating tray popover off the topbar bell (internal_base.html + static/js/
# notification-tray.js), fetched as a JSON fragment, and the plain full page
# below — the tray's no-JS fallback and its "see everything" link target, not
# a second implementation. `_is_xhr` matches the X-Requested-With convention
# static/js/toast.js and orders/customer_views.py already use for this.

def _is_xhr(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _notification_counts(notifications_qs):
    return {
        'total_count': notifications_qs.count(),
        'unread_count': notifications_qs.filter(is_read=False).count(),
        'needs_action_count': notifications_qs.filter(
            requires_action=True, resolved_at__isnull=True
        ).count(),
    }


def _tray_context(user, branch_filter=''):
    """Shared by the tray fragment and the full page: everything non-Low
    priority renders as its own card (escalations stay individually visible —
    see the v2 design spec's "group what would repeat" principle); Low
    priority items collapse into one expandable group row when there's more
    than one, so a run of low-stakes events (e.g. several new-staff-account
    notices) can't push a Critical item below the fold.

    `branch_filter` (a Branch pk as a string, '' for none) is Owner-only —
    the tray fragment never passes one, only the full page's topbar
    switcher does. See notification_center_view."""
    from accounts.models import Notification

    notifications = user.notifications.exclude(category='').select_related('branch')
    if branch_filter:
        notifications = notifications.filter(branch_id=branch_filter)
    low_qs = notifications.filter(priority=Notification.Priority.LOW)
    low_count = low_qs.count()

    if low_count > 1:
        main_notifications = list(notifications.exclude(priority=Notification.Priority.LOW)[:200])
        low_group_items = list(low_qs[:12])
        low_group_more = max(low_count - len(low_group_items), 0)
    else:
        # 0 or 1 Low item: no group needed, it shows as an ordinary card in
        # its natural chronological position among the rest.
        main_notifications = list(notifications[:200])
        low_group_items = None
        low_group_more = 0

    return {
        'main_notifications': main_notifications,
        'low_group_items': low_group_items,
        'low_group_count': low_count,
        'low_group_more': low_group_more,
    }, notifications


@role_required(*INTERNAL_ROLES)
def notification_center_view(request):
    if _is_xhr(request):
        # The tray popover always shows everything — it has no branch chip
        # of its own, so it never reads ?branch=.
        tray_ctx, notifications = _tray_context(request.user)
        counts = _notification_counts(notifications)
        list_html = render_to_string(
            'core/_notification_tray_list.html', tray_ctx, request=request
        )
        return JsonResponse({'list_html': list_html, **counts})

    from accounts.models import Notification
    from branches.models import Branch
    from core.scoping import branch_for_user

    # Owner-only branch filter — Manager/Staff only ever receive
    # notifications for their own branch (see core/notifications.py's fan-out
    # rules), so a selector would do nothing for them. Same pattern as
    # procurement's delivery_list / Floor Stock / Warehouse Stock.
    user_branch = branch_for_user(request.user)
    branch_filter = request.GET.get('branch', '') if not user_branch else ''
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''

    tray_ctx, notifications = _tray_context(request.user, branch_filter)
    counts = _notification_counts(notifications)

    def _cat_counts(category):
        # total + needs_action per category, so the full page's tabs can tint
        # a count danger the same way the KPI cards do (see
        # feedback-kpi-card-status-tint) instead of every tab reading neutral
        # regardless of what's actually sitting in it.
        cat_qs = notifications.filter(category=category)
        return {
            'total': cat_qs.count(),
            'needs_action': cat_qs.filter(
                requires_action=True, resolved_at__isnull=True
            ).count(),
        }

    branches = Branch.objects.all() if not user_branch else []

    return render(request, 'core/notification_center.html', {
        **tray_ctx,
        **counts,
        # Table rows for the full page — unlike main_notifications (from
        # tray_ctx), Low priority items aren't split into a collapsed group
        # here: a table row is already compact enough that the grouping
        # tray cards needed to avoid pushing a Critical item below the fold
        # doesn't buy anything on this page.
        'all_notifications': list(notifications[:200]),
        'counts': {
            'staff': _cat_counts(Notification.Category.STAFF),
            'procurement': _cat_counts(Notification.Category.PROCUREMENT),
            'sales': _cat_counts(Notification.Category.SALES),
            'system': _cat_counts(Notification.Category.SYSTEM),
        },
        'branches': branches,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this (partials/_branch_switch.html).
        'filter_branch': next((b for b in branches if str(b.id) == branch_filter), None),
        'user_branch': user_branch,
        'branch_switch_carry': [],
        'active_page': 'notifications',
    })


@require_POST
@role_required(*INTERNAL_ROLES)
def notification_mark_read(request, pk):
    notification = get_object_or_404(request.user.notifications, pk=pk)
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=['is_read'])
    if _is_xhr(request):
        return JsonResponse(_notification_counts(request.user.notifications.exclude(category='')))
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
        if not _is_xhr(request):
            messages.success(request, 'Marked as resolved.')
    if _is_xhr(request):
        return JsonResponse(_notification_counts(request.user.notifications.exclude(category='')))
    return redirect('notification_center')


@require_POST
@role_required(*INTERNAL_ROLES)
def notification_mark_all_read(request):
    updated = request.user.notifications.exclude(category='').filter(
        is_read=False
    ).update(is_read=True)
    if _is_xhr(request):
        return JsonResponse(_notification_counts(request.user.notifications.exclude(category='')))
    if updated:
        messages.success(
            request,
            f'Marked {updated} notification{"" if updated == 1 else "s"} as read.',
        )
    else:
        messages.info(request, 'You have no unread notifications.')
    return redirect('notification_center')
