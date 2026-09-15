import time

from django.contrib import messages
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.permissions import INTERNAL_ROLES, OWNER, role_required

from .changelog import visible_changelog
from .version import APP_VERSION


def healthz(request):
    """Railway's deploy health check (railway.json healthcheckPath). Cheap
    and unauthenticated on purpose — this confirms the gunicorn process
    came up and can serve a request at all, not that the DB is reachable
    (that's db_latency_check, which is Owner-gated and does real queries).
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
