from django.conf import settings
from django.core.cache import cache
from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base: adds created/updated timestamps. Use on NEW models."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditStampedModel(TimeStampedModel):
    """
    Abstract base giving a digital audit trail (a documented objective:
    every write is attributable). Adds created_by / updated_by on top of
    timestamps. Apply to NEW models; existing models that already carry
    their own audit fields (StockMovement, StockTransfer, Procurement,
    BillingRecord) are left as-is to avoid churn this week.
    """

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        abstract = True


class StoreSetting(TimeStampedModel):
    """Store-wide content the Owner controls — one row for the whole system.

    The Owner edits this on the internal Settings page (see
    core.views.store_settings_update); everything else reads it through
    ``StoreSetting.load()`` or the ``store_settings`` template variable
    (core.context_processors.store_settings).

    Not a per-branch record. The branches all trade under one registered
    entity, so what lives here is the legal identity for printed documents,
    the editable copy for the public About page, and the layout of the
    printed purchase order / receipt. The rotating storefront hero is its
    own model, HomeHeroSlide, since the Owner adds and removes those.

    Singleton: the row's PK is pinned to SINGLETON_ID, save() forces it, and
    delete() is a no-op, so there can only ever be one. load() is cached for
    a few minutes the same way core.refdata caches branches/categories —
    this is read on nearly every request via the context processor, and it
    only changes on an explicit Owner action.
    """

    SINGLETON_ID = 1
    _CACHE_KEY = "core:store_settings"
    _CACHE_SECONDS = 300

    # --- Legal identity, printed on purchase orders and receipts ----------
    # The store name / address defaults to per-branch (Branch.branch_name /
    # Branch.location) since printed documents already carry the branch they
    # belong to; the Owner can override it store-wide with
    # ``receipt_letterhead`` below. TIN and the VAT registration line are
    # entity-level — one registration covers every branch — so they are the
    # pieces that belong to the whole store.
    tin = models.CharField(
        "TIN", max_length=20, blank=True,
        help_text="Taxpayer Identification Number, printed on official receipts.",
    )
    vat_registration = models.CharField(
        max_length=80, blank=True,
        help_text="The VAT-registration line for official receipts "
                  "(e.g. 'VAT Reg. TIN 000-000-000-000').",
    )

    # --- Public "About Us" copy (templates/about.html) -------------------
    about_hero_lead = models.TextField(
        blank=True,
        help_text="The opening paragraph under the store name on the About page.",
    )
    about_tagline = models.CharField(
        max_length=120, blank=True,
        help_text="The short line under the hero (e.g. 'Sa Gerlyn, Lahat Panalo.').",
    )
    about_story = models.TextField(
        blank=True,
        help_text="The 'Our story' section. Blank lines start a new paragraph.",
    )

    # --- Printed purchase order / receipt layout ------------------------
    receipt_show_logo = models.BooleanField(default=True)
    receipt_logo = models.ImageField(
        upload_to="store/", null=True, blank=True,
        help_text="Optional logo shown at the top of the printed document.",
    )
    receipt_letterhead = models.TextField(
        blank=True,
        help_text="Store name and address printed at the very top of every "
                  "purchase order and receipt, under the logo. Leave blank to "
                  "use each branch's own name and location; when set, the "
                  "branch is still named on a small line beneath.",
    )
    receipt_header = models.TextField(
        blank=True,
        help_text="Free text printed under the letterhead, above the parties "
                  "(e.g. a tagline or an office address).",
    )
    receipt_show_tin = models.BooleanField(
        "Print TIN on documents", default=True,
    )
    receipt_show_vat = models.BooleanField(
        "Print VAT registration line on documents", default=True,
    )
    receipt_footer = models.CharField(
        max_length=200, blank=True,
        help_text="A short line printed at the foot of the document "
                  "(e.g. 'This serves as your official receipt. Thank you!').",
    )
    receipt_thank_you = models.CharField(
        max_length=120, blank=True,
        help_text="A closing line printed under the totals on a sales receipt.",
    )

    class Meta:
        verbose_name = "Store Settings"
        verbose_name_plural = "Store Settings"

    def __str__(self):
        return "Store Settings"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_ID
        super().save(*args, **kwargs)
        cache.delete(self._CACHE_KEY)

    def delete(self, *args, **kwargs):
        # Singleton — there is nothing sensible to delete to.
        pass

    @classmethod
    def load(cls):
        """The one settings row, creating it with defaults on first access.
        Cached for _CACHE_SECONDS; save() busts the cache."""
        obj = cache.get(cls._CACHE_KEY)
        if obj is None:
            obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_ID)
            cache.set(cls._CACHE_KEY, obj, cls._CACHE_SECONDS)
        return obj


class HomeHeroSlide(TimeStampedModel):
    """One card in the storefront home page's hero area (see
    templates/partials/home_content.html and the mobile block in
    templates/home.html).

    `placement` decides where it goes:
      * ROTATOR — a slide in the big rotating banner on the left.
      * SIDE    — one of the two small cards stacked on the right. The
                  layout is a 1fr 1fr grid, so at most two are drawn.

    The Owner adds, reorders and retires these on the internal Settings
    page. When nothing is active for a placement the templates fall back to
    their built-in copy, so the home page is never left with an empty hero.

    `icon` is a key into a fixed set of inline SVGs the template already
    draws — a slide can't carry arbitrary artwork, so it picks from the
    icons the storefront's visual language already uses.
    """

    class Placement(models.TextChoices):
        ROTATOR = "rotator", "Rotating banner"
        SIDE = "side", "Side card"

    class Icon(models.TextChoices):
        DELIVERY = "delivery", "Delivery van"
        BRANCH = "branch", "Storefront"
        PAYMENT = "payment", "Card"
        PIN = "pin", "Location pin"
        TAG = "tag", "Price tag"
        BASKET = "basket", "Basket"

    placement = models.CharField(
        max_length=8, choices=Placement.choices, default=Placement.ROTATOR,
    )
    eyebrow = models.CharField(
        max_length=40, blank=True,
        help_text="The small label above the headline (e.g. 'Free delivery'). "
                  "Rotating slides only.",
    )
    icon = models.CharField(
        max_length=12, choices=Icon.choices, default=Icon.TAG,
    )
    headline = models.CharField(max_length=60)
    body = models.CharField(max_length=160)
    cta_label = models.CharField(
        max_length=30, blank=True, default="Shop now",
        help_text="Link text at the bottom of the card. Rotating slides only.",
    )
    cta_url = models.CharField(
        max_length=200, default="/shop/",
        help_text="Where the card links to. A path like /shop/ or /shop/branch/.",
    )
    sort_order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers show first.",
    )
    is_active = models.BooleanField(
        default=True, help_text="Uncheck to hide this without deleting it.",
    )

    # ── Draft workflow ───────────────────────────────────────────────────
    # The Owner edits slides in place on the Settings page, previews the
    # storefront at /?preview=1, then Applies or Cancels. Until Applied the
    # public page keeps rendering the last-published state:
    #   * is_draft      — this row has edits (or is a new / removed slide)
    #                     that are not live yet.
    #   * is_shadow     — a frozen copy of a slide's last-published values,
    #                     kept only while its twin is being edited so Cancel
    #                     can restore it. Never rendered on the storefront and
    #                     never listed in the editor.
    #   * pending_delete — a draft the Owner removed; still live (through its
    #                     shadow) until Apply.
    is_draft = models.BooleanField(default=False)
    is_shadow = models.BooleanField(default=False)
    pending_delete = models.BooleanField(default=False)
    shadow_of = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.CASCADE,
        related_name="shadow",
    )

    class Meta:
        ordering = ["placement", "sort_order", "id"]
        verbose_name = "Home hero slide"

    def __str__(self):
        return f"[{self.get_placement_display()}] {self.headline}"

    # Fields copied into a shadow — everything the storefront renders, but
    # none of the draft-workflow bookkeeping.
    SHADOW_FIELDS = (
        "placement", "eyebrow", "icon", "headline", "body",
        "cta_label", "cta_url", "sort_order", "is_active",
    )

    @classmethod
    def _live_qs(cls):
        """What visitors see: clean published rows, plus the frozen shadows
        standing in for slides that are currently being edited or removed."""
        return cls.objects.filter(
            models.Q(is_shadow=True) | models.Q(is_shadow=False, is_draft=False),
            is_active=True,
        )

    @classmethod
    def _preview_qs(cls):
        """What the Owner sees at /?preview=1: drafts applied, pending
        removals gone, shadows hidden."""
        return cls.objects.filter(
            is_shadow=False, pending_delete=False, is_active=True,
        )

    @classmethod
    def rotator_slides(cls, *, preview=False):
        qs = cls._preview_qs() if preview else cls._live_qs()
        return list(qs.filter(placement=cls.Placement.ROTATOR))

    @classmethod
    def side_cards(cls, *, preview=False):
        qs = cls._preview_qs() if preview else cls._live_qs()
        return list(qs.filter(placement=cls.Placement.SIDE)[:2])

    @classmethod
    def has_pending_changes(cls):
        """True when staged hero changes are waiting to be applied."""
        return cls.objects.filter(is_draft=True).exists()

    def freeze_shadow(self):
        """Snapshot this slide's live values into a shadow twin — once, just
        before its first unpublished edit — so Cancel can restore it."""
        if self.pk is None or HomeHeroSlide.objects.filter(shadow_of=self).exists():
            return
        HomeHeroSlide.objects.create(
            is_shadow=True, shadow_of=self,
            **{f: getattr(self, f) for f in self.SHADOW_FIELDS},
        )

    def drop_identical_shadow(self):
        """If a slide's only 'draft' was a removal that has now been undone,
        its shadow is a byte-for-byte duplicate — clear it so the slide reads
        as fully published again."""
        shadow = HomeHeroSlide.objects.filter(shadow_of=self).first()
        if shadow and all(
            getattr(shadow, f) == getattr(self, f) for f in self.SHADOW_FIELDS
        ):
            shadow.delete()
            return True
        return False
