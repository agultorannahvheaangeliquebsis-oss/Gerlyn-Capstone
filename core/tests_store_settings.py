"""Store Settings — the singleton row, the editable About Us copy, the
printed-document options, and the storefront hero-slide manager
(core.models.StoreSetting / HomeHeroSlide)."""
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from audit.models import AuditLog
from branches.models import Branch
from core.models import HomeHeroSlide, StoreSetting
from sales.models import SalesTransaction


class StoreSettingSingletonTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_load_returns_one_row_and_stays_one(self):
        a = StoreSetting.load()
        a.tin = "123-456"
        a.save()
        b = StoreSetting.load()
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(StoreSetting.objects.count(), 1)
        self.assertEqual(b.tin, "123-456")

    def test_delete_is_a_no_op(self):
        s = StoreSetting.load()
        s.delete()
        self.assertEqual(StoreSetting.objects.count(), 1)

    def test_save_busts_the_cache(self):
        StoreSetting.load()
        s = StoreSetting.objects.get(pk=StoreSetting.SINGLETON_ID)
        s.about_tagline = "Sa Gerlyn, Lahat Panalo."
        s.save()
        self.assertEqual(StoreSetting.load().about_tagline, "Sa Gerlyn, Lahat Panalo.")


class StoreSettingsUpdateViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(username="owner1", password="Passw0rd-", role="OWNER")
        self.staff = User.objects.create_user(username="staff1", password="Passw0rd-", role="STAFF")

    def _payload(self, **over):
        data = {
            "tin": "000-111-222",
            "vat_registration": "VAT Reg. 000-111-222",
            "about_hero_lead": "We have served Baliwag for ten years.",
            "about_tagline": "Sa Gerlyn, Lahat Panalo.",
            "about_story": "Line one.\n\nLine two.",
            "receipt_letterhead": "Gerlyn Variety Store\n1 Main St, Baliwag",
            "receipt_header": "Head office: 1 Main St",
            "receipt_footer": "This serves as your official receipt.",
            "receipt_thank_you": "Thank you!",
            "receipt_show_logo": "1",
            "receipt_show_tin": "1",
            "receipt_show_vat": "1",
        }
        data.update(over)
        return data

    def test_owner_can_save_all_fields(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("store_settings_update"), self._payload())
        self.assertRedirects(resp, reverse("internal_settings") + "#store-settings")
        s = StoreSetting.load()
        self.assertEqual(s.tin, "000-111-222")
        self.assertEqual(s.about_tagline, "Sa Gerlyn, Lahat Panalo.")
        self.assertIn("Line two.", s.about_story)
        self.assertIn("Gerlyn Variety Store", s.receipt_letterhead)
        self.assertTrue(s.receipt_show_vat)

    def test_unchecked_boxes_read_as_false(self):
        self.client.force_login(self.owner)
        data = self._payload()
        data.pop("receipt_show_tin")
        data.pop("receipt_show_logo")
        self.client.post(reverse("store_settings_update"), data)
        s = StoreSetting.load()
        self.assertFalse(s.receipt_show_tin)
        self.assertFalse(s.receipt_show_logo)
        self.assertTrue(s.receipt_show_vat)

    def test_staff_is_forbidden(self):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("store_settings_update"), self._payload())
        self.assertEqual(resp.status_code, 403)

    def test_settings_page_renders_the_editor_for_the_owner(self):
        HomeHeroSlide.objects.create(placement="rotator", headline="Slide A", body="b")
        self.client.force_login(self.owner)
        html = self.client.get(reverse("internal_settings")).content.decode()
        for marker in ('id="store-settings"', 'id="home-hero"', "ss-doc-paper",
                       "Add a slide", "Slide A"):
            self.assertIn(marker, html)

    def test_settings_page_hides_the_editor_from_staff(self):
        self.client.force_login(self.staff)
        html = self.client.get(reverse("internal_settings")).content.decode()
        self.assertNotIn('id="home-hero"', html)
        self.assertNotIn("store_settings_update", html)


class HeroSlideManagerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(username="owner1", password="Passw0rd-", role="OWNER")
        self.staff = User.objects.create_user(username="staff1", password="Passw0rd-", role="STAFF")

    def _slide_payload(self, **over):
        data = {
            "slide_id": "",
            "placement": "rotator",
            "icon": "pin",
            "eyebrow": "Notice",
            "headline": "Closed June 12",
            "body": "All branches closed for Independence Day.",
            "cta_label": "See details",
            "cta_url": "/about/",
            "sort_order": "1",
            "is_active": "1",
        }
        data.update(over)
        return data

    def test_owner_adds_then_edits_a_slide(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("hero_slide_save"), self._slide_payload())
        slide = HomeHeroSlide.objects.get()
        self.assertEqual(slide.headline, "Closed June 12")
        self.assertEqual(slide.placement, "rotator")

        self.client.post(reverse("hero_slide_save"), self._slide_payload(
            slide_id=str(slide.pk), headline="Closed June 12–13"
        ))
        slide.refresh_from_db()
        self.assertEqual(slide.headline, "Closed June 12–13")
        self.assertEqual(HomeHeroSlide.objects.count(), 1)

    def test_headline_and_body_are_required(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("hero_slide_save"), self._slide_payload(body=""))
        self.assertRedirects(resp, reverse("internal_settings") + "#home-hero")
        self.assertEqual(HomeHeroSlide.objects.count(), 0)

    def test_offsite_cta_url_is_rejected(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("hero_slide_save"), self._slide_payload(cta_url="https://evil.example"))
        self.assertEqual(HomeHeroSlide.objects.count(), 0)

    def test_bad_placement_or_icon_falls_back(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("hero_slide_save"), self._slide_payload(placement="xxx", icon="yyy"))
        slide = HomeHeroSlide.objects.get()
        self.assertEqual(slide.placement, HomeHeroSlide.Placement.ROTATOR)
        self.assertEqual(slide.icon, HomeHeroSlide.Icon.TAG)

    def test_delete_stages_removal_and_keeps_it_live_until_applied(self):
        slide = HomeHeroSlide.objects.create(headline="x", body="y")
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("hero_slide_delete"), {"slide_id": str(slide.pk)})
        self.assertRedirects(resp, reverse("internal_settings") + "#home-hero")
        slide.refresh_from_db()
        self.assertTrue(slide.pending_delete)
        # Still on the live storefront until the Owner applies the change.
        self.assertEqual(len(HomeHeroSlide.rotator_slides()), 1)
        # ...but gone from the preview.
        self.assertEqual(len(HomeHeroSlide.rotator_slides(preview=True)), 0)

        self.client.post(reverse("hero_draft_apply"), {})
        self.assertEqual(HomeHeroSlide.objects.count(), 0)

    def test_staff_cannot_touch_slides(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse("hero_slide_save"), self._slide_payload()).status_code, 403)
        self.assertEqual(self.client.post(reverse("hero_slide_delete"), {"slide_id": "1"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("hero_draft_apply"), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse("hero_draft_discard"), {}).status_code, 403)

    def test_placement_helpers_split_and_cap(self):
        for i in range(3):
            HomeHeroSlide.objects.create(placement="rotator", headline=f"r{i}", body="b", sort_order=i)
        for i in range(3):
            HomeHeroSlide.objects.create(placement="side", headline=f"s{i}", body="b", sort_order=i)
        HomeHeroSlide.objects.create(placement="rotator", headline="hidden", body="b", is_active=False)
        self.assertEqual(len(HomeHeroSlide.rotator_slides()), 3)
        self.assertEqual(len(HomeHeroSlide.side_cards()), 2)  # capped at two


class HeroDraftWorkflowTests(TestCase):
    """Edits are staged: the live storefront keeps the last-applied version
    until the Owner Applies from the /?preview=1 bar; Cancel restores it."""

    def setUp(self):
        cache.clear()
        Branch.objects.create(branch_name="Makinabang", location="Baliwag")
        self.owner = User.objects.create_user(
            username="owner1", password="Passw0rd-", role="OWNER"
        )
        self.slide = HomeHeroSlide.objects.create(
            placement="rotator", headline="Live headline", body="Live body.",
        )

    def _edit_payload(self, **over):
        data = {
            "slide_id": str(self.slide.pk),
            "placement": "rotator", "icon": "pin", "eyebrow": "",
            "headline": "Draft headline", "body": "Draft body.",
            "cta_label": "", "cta_url": "/shop/", "sort_order": "0",
            "is_active": "1",
        }
        data.update(over)
        return data

    def test_edit_is_held_back_from_the_live_page(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("hero_slide_save"), self._edit_payload())

        # Live storefront still shows the applied version.
        self.assertEqual(HomeHeroSlide.rotator_slides()[0].headline, "Live headline")
        # Preview shows the staged edit.
        self.assertEqual(
            HomeHeroSlide.rotator_slides(preview=True)[0].headline, "Draft headline"
        )
        self.assertTrue(HomeHeroSlide.has_pending_changes())

    def test_apply_publishes_the_edit_and_clears_shadows(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("hero_slide_save"), self._edit_payload())
        self.client.post(reverse("hero_draft_apply"), {})

        self.assertEqual(HomeHeroSlide.rotator_slides()[0].headline, "Draft headline")
        self.assertFalse(HomeHeroSlide.has_pending_changes())
        self.assertEqual(HomeHeroSlide.objects.count(), 1)  # shadow gone

    def test_apply_records_the_draft_to_live_audit_update(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("hero_slide_save"), self._edit_payload())

        self.client.post(reverse("hero_draft_apply"), {})

        audit_row = AuditLog.objects.filter(
            entity='core.HomeHeroSlide',
            record_id=str(self.slide.pk),
            action=AuditLog.Action.UPDATE,
            action_label='Published homepage changes',
        ).latest('id')
        self.assertEqual(audit_row.actor_id, self.owner.pk)
        self.assertEqual(audit_row.before, {'is_draft': True})
        self.assertEqual(audit_row.after, {'is_draft': False})

    def test_cancel_restores_the_previous_version(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("hero_slide_save"), self._edit_payload())
        self.client.post(reverse("hero_draft_discard"), {})

        self.assertEqual(HomeHeroSlide.rotator_slides()[0].headline, "Live headline")
        self.assertEqual(
            HomeHeroSlide.rotator_slides(preview=True)[0].headline, "Live headline"
        )
        self.assertFalse(HomeHeroSlide.has_pending_changes())
        self.assertEqual(HomeHeroSlide.objects.count(), 1)

    def test_new_slide_is_a_draft_until_applied_and_vanishes_on_cancel(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("hero_slide_save"), self._edit_payload(
            slide_id="", headline="Brand new", body="New body."
        ))
        self.assertEqual(len(HomeHeroSlide.rotator_slides()), 1)          # live: just the original
        self.assertEqual(len(HomeHeroSlide.rotator_slides(preview=True)), 2)  # preview: + the new one

        self.client.post(reverse("hero_draft_discard"), {})
        self.assertEqual(HomeHeroSlide.objects.filter(headline="Brand new").count(), 0)
        self.assertEqual(HomeHeroSlide.objects.count(), 1)

    def test_undo_removal_keeps_the_slide(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("hero_slide_delete"), {"slide_id": str(self.slide.pk)})
        self.slide.refresh_from_db()
        self.assertTrue(self.slide.pending_delete)

        self.client.post(reverse("hero_slide_save"), {
            "slide_id": str(self.slide.pk), "undo_delete": "1",
        })
        self.slide.refresh_from_db()
        self.assertFalse(self.slide.pending_delete)
        self.assertFalse(HomeHeroSlide.has_pending_changes())
        self.assertEqual(HomeHeroSlide.objects.count(), 1)

    def test_preview_flag_bypasses_the_internal_redirect_for_owner(self):
        self.client.force_login(self.owner)
        # Bare "/" bounces an internal user to their dashboard...
        self.assertEqual(self.client.get("/").status_code, 302)
        # ...but ?preview=1 renders the storefront home.
        resp = self.client.get("/?preview=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("banner-rotator", resp.content.decode())

    def test_preview_flag_is_ignored_for_anonymous_visitors(self):
        resp = self.client.get("/?preview=1")
        self.assertEqual(resp.status_code, 200)
        # The preview bar markup (not the always-present CSS) must be absent.
        self.assertNotIn('aria-label="Home page preview"', resp.content.decode())


class PrintedReceiptTests(TestCase):
    """The sales receipt reads the same StoreSetting letterhead the printed
    purchase order does, and shows or hides the legal lines on the Owner's
    toggles. Receipt No. / date / items come from the order itself."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        cache.clear()

    def _completed_sale(self):
        return SalesTransaction.objects.filter(
            order_status=SalesTransaction.OrderStatus.COMPLETED
        ).first()

    def _owner(self):
        return User.objects.filter(role="OWNER").first()

    def test_receipt_prints_letterhead_and_thank_you_when_set(self):
        s = StoreSetting.load()
        s.tin = "111-222-333-000"
        s.vat_registration = "VAT Reg. 111-222-333-000"
        s.receipt_thank_you = "Salamat sa iyong pagbili!"
        s.receipt_footer = "This serves as your official receipt."
        s.receipt_show_tin = True
        s.receipt_show_vat = True
        s.save()

        sale = self._completed_sale()
        self.client.force_login(self._owner())
        html = self.client.get(f"/sales/{sale.pk}/receipt/").content.decode()

        self.assertIn("SALES RECEIPT", html)
        self.assertIn(sale.order_number, html)
        self.assertIn("111-222-333-000", html)
        self.assertIn("VAT Reg. 111-222-333-000", html)
        self.assertIn("Salamat sa iyong pagbili!", html)
        self.assertIn("This serves as your official receipt.", html)

    def test_receipt_hides_tin_and_vat_when_toggled_off(self):
        s = StoreSetting.load()
        s.tin = "111-222-333-000"
        s.vat_registration = "VAT Reg. 111-222-333-000"
        s.receipt_show_tin = False
        s.receipt_show_vat = False
        s.save()

        sale = self._completed_sale()
        self.client.force_login(self._owner())
        html = self.client.get(f"/sales/{sale.pk}/receipt/").content.decode()

        self.assertNotIn("111-222-333-000", html)

    def test_detail_page_offers_print_receipt_only_for_a_completed_sale(self):
        self.client.force_login(self._owner())
        completed = self._completed_sale()
        html = self.client.get(f"/sales/{completed.pk}/").content.decode()
        self.assertIn("Print Receipt", html)
        self.assertIn(f"/sales/{completed.pk}/receipt/", html)


class StorefrontRenderTests(TestCase):
    """The public pages read the settings and never break when they're empty."""

    def setUp(self):
        cache.clear()
        Branch.objects.create(branch_name="Makinabang", location="Baliwag")

    def test_about_page_uses_owner_copy_when_set(self):
        s = StoreSetting.load()
        s.about_story = "Our own story, told our way."
        s.about_tagline = "Panalo!"
        s.save()
        html = self.client.get("/about/").content.decode()
        self.assertIn("Our own story, told our way.", html)
        self.assertIn("Panalo!", html)

    def test_about_page_falls_back_when_blank(self):
        resp = self.client.get("/about/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Our story", resp.content.decode())

    def test_home_hero_falls_back_with_no_slides(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("banner-rotator", resp.content.decode())

    def test_home_hero_renders_owner_slide(self):
        HomeHeroSlide.objects.create(
            placement="rotator", icon="pin", eyebrow="Notice",
            headline="Holiday hours", body="Open 8am to noon on Dec 25.",
        )
        html = self.client.get("/").content.decode()
        self.assertIn("Holiday hours", html)
        self.assertIn("Open 8am to noon on Dec 25.", html)
