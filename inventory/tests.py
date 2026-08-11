from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import ProductCategory


class CategoryModalFlowTests(TestCase):
    """Add/Edit Category moved from their own pages into a modal on the
    category list. These cover the parts of that move a template cannot show:
    that the old URLs still land somewhere sensible, and that a rejected
    submit comes back with the modal open and the typed values intact."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)

    def test_get_add_url_redirects_to_list_with_open_flag(self):
        """A bookmark of the old Add Category page opens the modal instead."""
        response = self.client.get(reverse('category_add'))
        self.assertRedirects(response, reverse('category_list') + '?add=1')

    def test_get_edit_url_redirects_to_list_with_pk(self):
        category = ProductCategory.objects.create(category_name='Canned Goods')
        response = self.client.get(reverse('category_edit', args=[category.pk]))
        self.assertRedirects(
            response, reverse('category_list') + f'?edit={category.pk}'
        )

    def test_post_creates_category_and_returns_to_list(self):
        response = self.client.post(
            reverse('category_add'),
            {'category_name': 'Canned Goods', 'description': 'Tinned food'},
        )
        self.assertRedirects(response, reverse('category_list'))
        category = ProductCategory.objects.get(category_name='Canned Goods')
        self.assertEqual(category.description, 'Tinned food')

    def test_blank_name_reopens_modal_and_keeps_description(self):
        response = self.client.post(
            reverse('category_add'),
            {'category_name': '', 'description': 'Tinned food'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertIn('Category name is required.', response.context['add_errors'])
        # The typed description survives the round trip — a redirect would
        # have discarded it.
        self.assertContains(response, 'Tinned food')
        self.assertFalse(ProductCategory.objects.exists())

    def test_duplicate_name_is_rejected_with_the_modal_open(self):
        ProductCategory.objects.create(category_name='Canned Goods')
        response = self.client.post(
            reverse('category_add'), {'category_name': 'canned goods'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertEqual(ProductCategory.objects.count(), 1)

    def test_edit_saves_and_a_clash_reopens_the_edit_modal(self):
        first = ProductCategory.objects.create(category_name='Canned Goods')
        second = ProductCategory.objects.create(category_name='Snacks')

        ok = self.client.post(
            reverse('category_edit', args=[second.pk]),
            {'category_name': 'Beverages', 'description': ''},
        )
        self.assertRedirects(ok, reverse('category_list'))
        second.refresh_from_db()
        self.assertEqual(second.category_name, 'Beverages')

        clash = self.client.post(
            reverse('category_edit', args=[second.pk]),
            {'category_name': 'Canned Goods'},
        )
        self.assertEqual(clash.status_code, 200)
        self.assertTrue(clash.context['show_edit_modal'])
        self.assertEqual(clash.context['edit_category'].pk, second.pk)
        second.refresh_from_db()
        self.assertEqual(second.category_name, 'Beverages')
        # Renaming to its own current name is not a clash with itself.
        same = self.client.post(
            reverse('category_edit', args=[first.pk]),
            {'category_name': 'Canned Goods', 'description': 'Tinned'},
        )
        self.assertRedirects(same, reverse('category_list'))

    def test_list_opens_the_edit_modal_for_a_valid_edit_param(self):
        category = ProductCategory.objects.create(category_name='Canned Goods')
        response = self.client.get(
            reverse('category_list'), {'edit': category.pk}
        )
        self.assertTrue(response.context['show_edit_modal'])
        self.assertEqual(response.context['edit_category'].pk, category.pk)

    def test_list_ignores_an_unknown_edit_param(self):
        response = self.client.get(reverse('category_list'), {'edit': '9999'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('show_edit_modal', response.context)

    def test_product_list_still_renders_after_the_modal_move(self):
        """The modal CSS and initModal() moved out of product_list.html into
        gerlyn.css and static/js/modal.js. Nothing about the page should have
        changed, so this is here to catch it if the extraction broke it."""
        response = self.client.get(reverse('product_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'add-product-modal')

    def test_staff_cannot_post_a_category(self):
        staff = User.objects.create_user(
            username='staff', password='pw', role=User.Role.STAFF
        )
        self.client.force_login(staff)
        response = self.client.post(
            reverse('category_add'), {'category_name': 'Canned Goods'}
        )
        self.assertRedirects(response, reverse('category_list'))
        self.assertFalse(ProductCategory.objects.exists())
