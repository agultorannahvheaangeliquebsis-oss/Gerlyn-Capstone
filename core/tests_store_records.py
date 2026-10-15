import datetime as dt
import os
import shutil
import tempfile
from io import BytesIO, StringIO

from django.core.files.base import ContentFile
from django.db.models import F, Sum
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from branches.models import Branch
from core.store_data import catalog, history
from billing.models import BillingRecord
from inventory.models import Batch, Inventory, Product, ProductImage, WarehouseStock
from procurement.models import Procurement
from reports.services import gross_sales_total
from sales.models import SalesItem, SalesTransaction

LIVE_BRANCHES = {"Mini Mart", "Savers Mini Mart", "Tiaong 1", "Variety Store"}


class RebuildStoreRecordsTests(TestCase):
    """A short, low-traffic rebuild must still satisfy every invariant the
    dashboard and stock screens rely on."""

    @classmethod
    def setUpClass(cls):
        # The override must be live before TestCase.setUpClass runs
        # setUpTestData, which is what writes the product pictures.
        cls._media = tempfile.mkdtemp()
        cls._override = override_settings(MEDIA_ROOT=cls._media)
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    @classmethod
    def setUpTestData(cls):
        cls.end = timezone.localtime(timezone.now()).replace(microsecond=0)
        cls.start = cls.end.date() - dt.timedelta(days=9)
        cls.builder, cls.checks = history.rebuild(
            StringIO(), start=cls.start, end_dt=cls.end, scale=0.2,
            seed=7, images=True, reset_counters=False)

    def test_every_consistency_check_passes(self):
        failures = [f"{label}: {detail}" for label, ok, detail in self.checks if not ok]
        self.assertEqual(failures, [])

    def test_every_master_list_product_has_a_white_background_image(self):
        # The master list is the single source of truth for the count — it was
        # 30 products until it became the 104-product list.
        self.assertEqual(Product.objects.count(), len(catalog.PRODUCTS))
        for product in Product.objects.prefetch_related("images"):
            images = list(product.images.all())
            self.assertEqual(len(images), 1, product.sku_code)
            with Image.open(images[0].image.path) as img:
                self.assertEqual(img.size, (800, 800), product.sku_code)
                self.assertTrue(history.packshots.is_white_background(img), product.sku_code)

    def test_branch_names_are_exactly_the_live_four(self):
        self.assertEqual(set(Branch.objects.values_list("branch_name", flat=True)), LIVE_BRANCHES)

    def test_sales_cover_the_window_and_stop_at_now(self):
        first = SalesTransaction.objects.order_by("transaction_date").first()
        last = SalesTransaction.objects.order_by("transaction_date").last()
        self.assertEqual(timezone.localtime(first.transaction_date).date(), self.start)
        self.assertLessEqual(last.transaction_date, self.end)

    def test_revenue_equals_paid_billing_and_line_items(self):
        completed = SalesTransaction.objects.filter(order_status="Completed")
        revenue = gross_sales_total(SalesTransaction.objects.all())
        lines = SalesItem.objects.filter(transaction__in=completed).aggregate(t=Sum("subtotal"))["t"]
        billed = BillingRecord.objects.filter(
            transaction__in=completed).aggregate(t=Sum("amount_due"))["t"]
        self.assertEqual(revenue, lines)
        self.assertEqual(revenue, billed)

    def test_no_negative_or_orphan_stock(self):
        self.assertFalse(Inventory.objects.filter(quantity_on_hand__lt=0).exists())
        for ws in WarehouseStock.objects.select_related("warehouse"):
            in_batches = Batch.objects.filter(
                product=ws.product, branch=ws.warehouse.branch, location="Warehouse",
            ).aggregate(t=Sum("quantity"))["t"] or 0
            self.assertEqual(in_batches, ws.quantity_on_hand)

    def test_receipts_never_precede_their_purchase_order(self):
        for po in Procurement.objects.exclude(actual_receipt_date=None):
            self.assertGreaterEqual(po.actual_receipt_date, po.order_date, po.po_number)

    def test_no_placeholder_labels_anywhere(self):
        from accounts.models import User
        from inventory.models import ProductCategory

        needle = "se" + "ed"
        for text in (list(Product.objects.values_list("product_name", "sku_code", "description"))
                     + list(ProductCategory.objects.values_list("category_name")) + list(
                         User.objects.values_list("username", "first_name"))):
            for part in text:
                self.assertNotIn(needle, str(part).lower())

    def test_catalog_flags_match_the_master_list(self):
        exempt = set(Product.objects.filter(is_vat_exempt=True).values_list("sku_code", flat=True))
        self.assertEqual(exempt, {r[0] for r in catalog.PRODUCTS if "v" in r[9]})


class RebuildWithoutImagesTests(TestCase):
    """--no-images must leave the existing product pictures alone: same rows,
    same files. It is the only way to rebuild records without re-uploading
    (and renumbering) every picture."""

    @classmethod
    def setUpClass(cls):
        cls._media = tempfile.mkdtemp()
        cls._override = override_settings(MEDIA_ROOT=cls._media)
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def test_rebuild_with_images_false_keeps_the_same_product_images(self):
        end = timezone.localtime(timezone.now()).replace(microsecond=0)
        window = {"start": end.date() - dt.timedelta(days=2), "end_dt": end,
                  "scale": 0.05, "seed": 7, "reset_counters": False}
        # Build the products without pictures (rendering the full set takes
        # minutes), then give each one a small picture of its own to protect.
        history.rebuild(StringIO(), images=False, **window)
        buf = BytesIO()
        Image.new("RGB", (8, 8), "white").save(buf, "JPEG")
        for product in Product.objects.all():
            ProductImage(product=product, is_primary=True).image.save(
                f"{product.sku_code.lower()}.jpg", ContentFile(buf.getvalue()), save=True)

        def snapshot():
            return sorted(ProductImage.objects.values_list("id", "product_id", "image"))

        before = snapshot()
        self.assertEqual(len(before), len(catalog.PRODUCTS))
        sizes = {name: os.path.getsize(os.path.join(self._media, name))
                 for _, _, name in before}

        _, checks = history.rebuild(StringIO(), images=False, **window)

        self.assertEqual(snapshot(), before)
        for name, size in sizes.items():
            self.assertEqual(os.path.getsize(os.path.join(self._media, name)), size)
        self.assertEqual([c for c in checks if not c[1]], [])

    def test_no_images_build_does_not_demand_a_primary_picture(self):
        # A live catalogue can hold pictures that are not flagged primary (or
        # none at all). With --no-images they are left alone, so the build
        # must not fail its report over pictures it was told not to touch.
        end = timezone.localtime(timezone.now()).replace(microsecond=0)
        _, checks = history.rebuild(
            StringIO(), start=end.date() - dt.timedelta(days=2), end_dt=end,
            scale=0.05, seed=7, images=False, reset_counters=False)

        self.assertFalse(ProductImage.objects.exists())
        self.assertEqual([c[0] for c in checks if not c[1]], [])
        self.assertNotIn("every product has a primary image", [c[0] for c in checks])
