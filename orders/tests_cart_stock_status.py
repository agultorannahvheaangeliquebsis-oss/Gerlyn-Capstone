"""S-4 — the cart's low-stock badge used to trigger on a hardcoded
`it.in_stock <= 10`, which disagreed with the reorder_level-based tiering
every other stock list in the app uses (inventory.alerts.stock_status).

A product with a high reorder_level could sit above 10 units and still be
genuinely Critical, and never show the badge. A product with a low
reorder_level could sit at or below 10 and be perfectly healthy, and show
a false low-stock warning. _cart_items() now computes 'stock_status' with
the same helper the Floor/Warehouse Stock pages use, and cart.html reads
that instead of the raw threshold.
"""
import io
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product, ProductImage


class CartStockStatusTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='stock-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Stock Branch', location='Baliuag', owner=self.owner
        )
        self.shopper = User.objects.create_user(
            username='stock-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)
        session = self.client.session
        session['branch_id'] = self.branch.id
        session.save()

    def _cart_response(self, product, quantity_on_hand, reorder_level, qty_in_cart=1):
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=quantity_on_hand, reorder_level=reorder_level,
        )
        session = self.client.session
        session['cart'] = {str(product.pk): qty_in_cart}
        session.save()
        return self.client.get(reverse('orders:cart'))

    def test_above_ten_units_but_at_reorder_level_is_still_flagged(self):
        product = Product.objects.create(
            sku_code='SKU-HIGH-REORDER', product_name='Cement (Bag)',
            unit_of_measure='Bag', unit_price=250,
        )
        # 15 on hand, reorder_level 20 -> Critical, even though 15 > 10.
        response = self._cart_response(product, quantity_on_hand=15, reorder_level=20)

        self.assertEqual(response.context['items'][0]['stock_status'], 'Critical')
        self.assertContains(response, 'stock-status--low')

    def test_ten_or_fewer_units_but_well_above_reorder_level_is_not_flagged(self):
        product = Product.objects.create(
            sku_code='SKU-LOW-REORDER', product_name='Nails (Kg)',
            unit_of_measure='Kg', unit_price=80,
        )
        # 8 on hand, reorder_level 2 -> In Stock, even though 8 <= 10.
        response = self._cart_response(product, quantity_on_hand=8, reorder_level=2)

        self.assertEqual(response.context['items'][0]['stock_status'], 'In Stock')
        self.assertNotContains(response, 'stock-status--low')


_TEST_MEDIA = tempfile.mkdtemp(prefix='cart-thumb-media-')


@override_settings(MEDIA_ROOT=_TEST_MEDIA)
class CartThumbnailTests(TestCase):
    """The cart row showed the category glyph even for a product with an
    uploaded photo, because cart.html called product_icon directly instead of
    the shared partials/product_thumb.html the catalog uses."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA, ignore_errors=True)

    def setUp(self):
        owner = User.objects.create_user(
            username='thumb-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Thumb Branch', location='Baliuag', owner=owner
        )
        shopper = User.objects.create_user(
            username='thumb-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(shopper)

    def _cart_page(self, product):
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=20, reorder_level=2,
        )
        session = self.client.session
        session['branch_id'] = self.branch.id
        session['cart'] = {str(product.pk): 1}
        session.save()
        return self.client.get(reverse('orders:cart'))

    def test_product_with_a_photo_shows_it_in_the_cart(self):
        from PIL import Image

        product = Product.objects.create(
            sku_code='SKU-THUMB-IMG', product_name='Photo Item',
            unit_of_measure='Pc', unit_price=10,
        )
        buf = io.BytesIO()
        Image.new('RGB', (2, 2), color='green').save(buf, format='PNG')
        image = ProductImage.objects.create(
            product=product, is_primary=True,
            image=SimpleUploadedFile('thumb.png', buf.getvalue(), content_type='image/png'),
        )

        response = self._cart_page(product)

        self.assertContains(response, f'<img src="{image.image.url}"')

    def test_product_without_a_photo_falls_back_to_the_glyph(self):
        product = Product.objects.create(
            sku_code='SKU-THUMB-NOIMG', product_name='Plain Item',
            unit_of_measure='Pc', unit_price=10,
        )

        response = self._cart_page(product)

        self.assertContains(response, 'prod-glyph')
        self.assertNotContains(response, 'products/')
