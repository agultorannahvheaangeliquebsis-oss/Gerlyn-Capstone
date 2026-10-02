"""P02 - automatic selling-price computation.

Shelf price is suggested from supplier cost as cost x (1 + markup%) x 1.12 and
is never rewritten silently: a cost change notifies the Owner, and only a
product that opted in to auto-apply is repriced without asking.
"""
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Notification, User
from audit.models import AuditLog
from core.models import StoreSetting
from core.module_permissions import EDIT, PRODUCTS, default_module_actions
from inventory import pricing
from inventory.models import Product, ProductCategory, ProductPriceTier
from procurement.models import SupplierProduct, SupplierProductRequest


class PricingBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='p02-owner', password='pw', role=User.Role.OWNER)
        cls.manager = User.objects.create_user(
            username='p02-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        cls.staff = User.objects.create_user(
            username='p02-staff', password='pw', role=User.Role.STAFF)
        cls.supplier = User.objects.create_user(
            username='p02-supplier', password='pw', role=User.Role.SUPPLIER)
        cls.other_supplier = User.objects.create_user(
            username='p02-other', password='pw', role=User.Role.SUPPLIER)
        cls.category = ProductCategory.objects.create(category_name='P02 Grains')

    def setUp(self):
        cache.delete(StoreSetting._CACHE_KEY)
        self.store = StoreSetting.load()

    def set_store_markup(self, value):
        self.store.default_markup_percent = Decimal(value) if value is not None else None
        self.store.save()

    def product(self, price='100.00', **extra):
        return Product.objects.create(
            sku_code=extra.pop('sku_code', f'P02-{Product.objects.count()}'),
            product_name=extra.pop('product_name', 'P02 Rice'),
            unit_price=Decimal(price), category=extra.pop('category', self.category),
            **extra,
        )

    def link(self, product, cost='100.00', supplier=None, **extra):
        return SupplierProduct.objects.create(
            supplier=supplier or self.supplier, product=product,
            current_cost_price=None if cost is None else Decimal(cost),
            status=extra.pop('status', SupplierProduct.Status.APPROVED), **extra,
        )


class FormulaTests(PricingBase):
    def test_markup_applies_to_cost_then_vat_is_added_on_top(self):
        # 100 x 1.25 x 1.12 = 140.00
        self.assertEqual(pricing.price_from_cost('100', '25'), Decimal('140.00'))

    def test_zero_markup_still_adds_vat(self):
        self.assertEqual(pricing.price_from_cost('100', '0'), Decimal('112.00'))

    def test_result_is_rounded_to_centavos(self):
        # 0.446875 x 1.12 = 0.50050 -> 0.50
        self.assertEqual(pricing.price_from_cost('0.446875', '0'), Decimal('0.50'))
        # 10.01 x 1.10 x 1.12 = 12.33232 -> 12.33
        self.assertEqual(pricing.price_from_cost('10.01', '10'), Decimal('12.33'))
        # 10.00 x 1.10 x 1.12 = 12.32 exactly
        self.assertEqual(pricing.price_from_cost('10', '10'), Decimal('12.32'))


class MarkupResolutionTests(PricingBase):
    def test_product_beats_category_beats_store(self):
        self.set_store_markup('10')
        self.category.default_markup_percent = Decimal('20')
        self.category.save()
        product = self.product(markup_percent=Decimal('30'))
        self.assertEqual(pricing.resolve_markup(product), (Decimal('30'), 'product'))
        product.markup_percent = None
        product.save()
        self.assertEqual(pricing.resolve_markup(product), (Decimal('20'), 'category'))
        self.category.default_markup_percent = None
        self.category.save()
        product.refresh_from_db()
        self.assertEqual(pricing.resolve_markup(product), (Decimal('10.00'), 'store'))

    def test_a_zero_override_is_a_real_markup_not_blank(self):
        self.set_store_markup('10')
        product = self.product(markup_percent=Decimal('0'))
        self.assertEqual(pricing.resolve_markup(product), (Decimal('0'), 'product'))

    def test_nothing_configured_means_no_markup(self):
        self.assertEqual(pricing.resolve_markup(self.product()), (None, None))


class CostBasisTests(PricingBase):
    def setUp(self):
        super().setUp()
        self.set_store_markup('0')
        self.item = self.product()

    def test_preferred_approved_link_wins_even_when_dearer(self):
        self.link(self.item, '80.00')
        self.link(self.item, '95.00', supplier=self.other_supplier, is_preferred=True)
        self.assertEqual(pricing.cost_basis(self.item), Decimal('95.00'))

    def test_without_a_preferred_link_the_cheapest_approved_cost_is_used(self):
        self.link(self.item, '90.00')
        self.link(self.item, '85.00', supplier=self.other_supplier)
        self.assertEqual(pricing.cost_basis(self.item), Decimal('85.00'))

    def test_a_preferred_link_without_a_cost_falls_back_to_the_others(self):
        self.link(self.item, None, is_preferred=True)
        self.link(self.item, '70.00', supplier=self.other_supplier)
        self.assertEqual(pricing.cost_basis(self.item), Decimal('70.00'))

    def test_unapproved_offers_are_ignored(self):
        self.link(self.item, '10.00', status=SupplierProduct.Status.PROPOSED)
        self.link(self.item, '11.00', supplier=self.other_supplier,
                  status=SupplierProduct.Status.REJECTED)
        self.assertIsNone(pricing.cost_basis(self.item))
        self.assertFalse(pricing.suggest_price(self.item).available)

    def test_no_markup_means_no_suggestion_even_with_a_cost(self):
        self.set_store_markup(None)
        self.link(self.item, '50.00')
        self.assertFalse(pricing.suggest_price(self.item).available)

    def test_suggestion_combines_cost_and_markup(self):
        self.set_store_markup('25')
        self.link(self.item, '100.00')
        suggestion = pricing.suggest_price(self.item)
        self.assertEqual(suggestion.suggested_price, Decimal('140.00'))
        self.assertEqual(suggestion.markup_source, 'store')


class OnCostChangeTests(PricingBase):
    def setUp(self):
        super().setUp()
        self.set_store_markup('25')
        self.item = self.product('100.00')
        self.link(self.item, '100.00')   # suggests 140.00

    def notices(self):
        return Notification.objects.filter(event_type='price_suggestion')

    def test_suggest_only_notifies_the_owner_and_leaves_the_price_alone(self):
        self.assertEqual(pricing.on_cost_change(self.item, self.supplier), 'notified')
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('100.00'))
        notice = self.notices().get()
        self.assertEqual(notice.user, self.owner)
        self.assertIn('140.00', notice.message)

    def test_nothing_is_sent_when_the_price_already_matches(self):
        self.item.unit_price = Decimal('140.00')
        self.item.save()
        self.assertIsNone(pricing.on_cost_change(self.item, self.supplier))
        self.assertFalse(self.notices().exists())

    def test_a_manual_override_stays_quiet(self):
        self.item.price_is_manual = True
        self.item.save()
        self.assertIsNone(pricing.on_cost_change(self.item, self.supplier))
        self.assertFalse(self.notices().exists())

    def test_no_suggestion_no_notice(self):
        self.set_store_markup(None)
        self.assertIsNone(pricing.on_cost_change(self.item, self.supplier))

    def test_opt_in_auto_apply_reprices_and_tells_the_owner(self):
        self.item.auto_apply_suggested_price = True
        self.item.save()
        self.assertEqual(pricing.on_cost_change(self.item, self.supplier), 'applied')
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('140.00'))
        self.assertFalse(self.item.price_is_manual)
        self.assertIn('automatically', self.notices().get().message)

    def test_auto_apply_never_overrides_a_manual_price(self):
        self.item.auto_apply_suggested_price = True
        self.item.price_is_manual = True
        self.item.save()
        pricing.on_cost_change(self.item, self.supplier)
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('100.00'))

    def test_auto_apply_that_would_invert_the_tiers_falls_back_to_a_notice(self):
        ProductPriceTier.objects.create(
            product=self.item, min_quantity=10, unit_price=Decimal('90.00'))
        self.item.auto_apply_suggested_price = True
        self.item.save()
        # A much cheaper supplier drags the suggestion to 14.00, under the
        # 90.00 tier, so applying it would invert the ladder.
        self.link(self.item, '10.00', supplier=self.other_supplier)
        self.assertEqual(pricing.on_cost_change(self.item, self.supplier), 'notified')
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('100.00'))


class SupplierCostHooksTests(PricingBase):
    def setUp(self):
        super().setUp()
        self.set_store_markup('25')
        self.item = self.product('100.00')

    def test_supplier_changing_an_approved_cost_raises_a_suggestion(self):
        link = self.link(self.item, '50.00')
        self.client.force_login(self.supplier)
        self.client.post(
            reverse('supplier_set_product_cost', args=[link.pk]),
            {'current_cost_price': '100.00'})
        self.assertTrue(Notification.objects.filter(
            user=self.owner, event_type='price_suggestion').exists())

    def test_an_unapproved_offer_cost_does_not(self):
        link = self.link(self.item, '50.00', status=SupplierProduct.Status.PROPOSED)
        self.client.force_login(self.supplier)
        self.client.post(
            reverse('supplier_set_product_cost', args=[link.pk]),
            {'current_cost_price': '100.00'})
        self.assertFalse(Notification.objects.filter(
            event_type='price_suggestion').exists())

    def test_approving_an_offer_raises_a_suggestion(self):
        link = self.link(self.item, '100.00', status=SupplierProduct.Status.PROPOSED)
        self.client.force_login(self.owner)
        self.client.post(
            reverse('supplier_offer_review', args=[self.supplier.pk, link.pk]),
            {'decision': 'approve'})
        self.assertTrue(Notification.objects.filter(
            user=self.owner, event_type='price_suggestion').exists())

    def test_rejecting_an_offer_does_not(self):
        link = self.link(self.item, '100.00', status=SupplierProduct.Status.PROPOSED)
        self.client.force_login(self.owner)
        self.client.post(
            reverse('supplier_offer_review', args=[self.supplier.pk, link.pk]),
            {'decision': 'reject', 'note': 'no'})
        self.assertFalse(Notification.objects.filter(
            event_type='price_suggestion').exists())


@override_settings(AUDIT_ENABLED=True)
class ApplySuggestedPriceViewTests(PricingBase):
    def setUp(self):
        super().setUp()
        self.set_store_markup('25')
        self.item = self.product('100.00', price_is_manual=True)
        self.link(self.item, '100.00')
        self.url = reverse('product_apply_suggested_price', args=[self.item.pk])

    def test_owner_applies_the_price_and_regains_computed_control(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url)
        self.assertRedirects(
            response, reverse('product_edit', args=[self.item.pk]),
            fetch_redirect_response=False)
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('140.00'))
        self.assertFalse(self.item.price_is_manual)

    def test_the_change_is_audit_logged_with_its_own_label(self):
        self.client.force_login(self.owner)
        self.client.post(self.url)
        self.assertTrue(AuditLog.objects.filter(
            entity='inventory.Product', record_id=str(self.item.pk),
            action_label='Applied the computed selling price').exists())

    def test_manager_needs_the_products_edit_grant(self):
        # Branch Managers get no Product edit by default (role matrix), and
        # applying a price is a product edit, so it is gated the same way.
        self.client.force_login(self.manager)
        self.client.post(self.url)
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('100.00'))

    def test_manager_with_products_edit_may_apply(self):
        self.manager.allowed_modules = [PRODUCTS]
        self.manager.module_actions = {
            code: ([EDIT] if code == PRODUCTS else [])
            for code in default_module_actions()
        }
        self.manager.save(update_fields=['allowed_modules', 'module_actions'])
        self.client.force_login(self.manager)
        self.client.post(self.url)
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('140.00'))

    def test_staff_and_suppliers_cannot(self):
        for user in (self.staff, self.supplier):
            self.client.force_login(user)
            self.client.post(self.url)
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('100.00'))

    def test_get_is_not_allowed(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_refused_when_nothing_can_be_computed(self):
        self.set_store_markup(None)
        self.client.force_login(self.owner)
        response = self.client.post(self.url, follow=True)
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('100.00'))
        self.assertIn('No price can be computed', str(list(response.context['messages'])))

    def test_refused_when_it_would_not_clear_the_bulk_tiers(self):
        ProductPriceTier.objects.create(
            product=self.item, min_quantity=10, unit_price=Decimal('150.00'))
        self.client.force_login(self.owner)
        self.client.post(self.url)
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal('100.00'))

    def test_tiers_are_never_rewritten(self):
        ProductPriceTier.objects.create(
            product=self.item, min_quantity=10, unit_price=Decimal('90.00'))
        self.client.force_login(self.owner)
        self.client.post(self.url)
        self.assertEqual(
            list(self.item.price_tiers.values_list('unit_price', flat=True)),
            [Decimal('90.00')])


class ProductFormTests(PricingBase):
    def setUp(self):
        super().setUp()
        self.set_store_markup('25')
        self.client.force_login(self.owner)

    def edit(self, item, **overrides):
        data = {
            'product_name': item.product_name,
            'unit_of_measure': Product.UnitOfMeasure.PIECE,
            'unit_price': str(item.unit_price),
            'category': self.category.pk,
        }
        data.update(overrides)
        return self.client.post(reverse('product_edit', args=[item.pk]), data)

    def test_edit_shows_the_computed_price_panel(self):
        item = self.product('100.00')
        self.link(item, '100.00')
        response = self.client.get(reverse('product_edit', args=[item.pk]))
        self.assertEqual(response.context['suggestion'].suggested_price, Decimal('140.00'))
        self.assertTrue(response.context['suggestion_differs'])
        self.assertContains(response, 'Apply suggested price')

    def test_a_price_that_departs_from_the_suggestion_is_marked_manual(self):
        item = self.product('140.00')
        self.link(item, '100.00')
        self.edit(item, unit_price='150.00')
        item.refresh_from_db()
        self.assertTrue(item.price_is_manual)

    def test_taking_the_suggested_price_clears_manual(self):
        item = self.product('150.00', price_is_manual=True)
        self.link(item, '100.00')
        self.edit(item, unit_price='140.00')
        item.refresh_from_db()
        self.assertFalse(item.price_is_manual)

    def test_without_a_suggestion_the_flag_is_left_alone(self):
        item = self.product('10.00', price_is_manual=True)
        self.edit(item, unit_price='11.00')
        item.refresh_from_db()
        self.assertTrue(item.price_is_manual)

    def test_markup_override_and_auto_apply_are_saved(self):
        item = self.product('100.00')
        self.edit(item, markup_percent='15.5', auto_apply_suggested_price='1')
        item.refresh_from_db()
        self.assertEqual(item.markup_percent, Decimal('15.50'))
        self.assertTrue(item.auto_apply_suggested_price)

    def test_blank_markup_inherits_and_unticked_auto_apply_is_off(self):
        item = self.product('100.00', markup_percent=Decimal('9'),
                            auto_apply_suggested_price=True)
        self.edit(item, markup_percent='')
        item.refresh_from_db()
        self.assertIsNone(item.markup_percent)
        self.assertFalse(item.auto_apply_suggested_price)

    def test_bad_markup_is_refused_on_edit_and_add(self):
        item = self.product('100.00')
        for bad in ('abc', '-1', '10000', 'NaN'):
            response = self.edit(item, markup_percent=bad)
            self.assertEqual(response.status_code, 200, bad)
            self.assertTrue(response.context['edit_errors'], bad)
        response = self.client.post(reverse('product_add'), {
            'product_name': 'P02 New', 'unit_of_measure': 'Piece',
            'unit_price': '10.00', 'markup_percent': '-5',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(product_name='P02 New').exists())

    def test_add_saves_markup_override_and_hands_the_calculator_its_defaults(self):
        self.category.default_markup_percent = Decimal('12')
        self.category.save()
        page = self.client.get(reverse('product_add'))
        self.assertEqual(page.context['markup_defaults']['store'], '25.00')
        self.assertEqual(
            page.context['markup_defaults']['categories'][str(self.category.pk)], '12.00')
        self.client.post(reverse('product_add'), {
            'product_name': 'P02 Added', 'unit_of_measure': 'Piece',
            'unit_price': '10.00', 'markup_percent': '8',
            'category': self.category.pk,
        })
        self.assertEqual(
            Product.objects.get(product_name='P02 Added').markup_percent, Decimal('8.00'))

    def test_staff_cannot_edit_pricing_fields(self):
        item = self.product('100.00')
        self.client.force_login(self.staff)
        self.edit(item, markup_percent='99')
        item.refresh_from_db()
        self.assertIsNone(item.markup_percent)


class CategoryAndSettingsTests(PricingBase):
    def test_category_markup_is_saved_cleared_and_validated(self):
        self.client.force_login(self.owner)
        url = reverse('category_edit', args=[self.category.pk])
        self.client.post(url, {'category_name': 'P02 Grains', 'default_markup_percent': '18'})
        self.category.refresh_from_db()
        self.assertEqual(self.category.default_markup_percent, Decimal('18.00'))
        self.client.post(url, {'category_name': 'P02 Grains', 'default_markup_percent': '-2'})
        self.category.refresh_from_db()
        self.assertEqual(self.category.default_markup_percent, Decimal('18.00'))
        self.client.post(url, {'category_name': 'P02 Grains', 'default_markup_percent': ''})
        self.category.refresh_from_db()
        self.assertIsNone(self.category.default_markup_percent)

    def test_owner_sets_the_store_default(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('store_settings_update'), {'default_markup_percent': '22.5'})
        self.assertEqual(StoreSetting.load().default_markup_percent, Decimal('22.50'))

    def test_a_bad_store_default_is_refused(self):
        self.set_store_markup('10')
        self.client.force_login(self.owner)
        self.client.post(reverse('store_settings_update'), {'default_markup_percent': 'x'})
        self.assertEqual(StoreSetting.load().default_markup_percent, Decimal('10.00'))

    def test_a_settings_post_without_the_field_leaves_it_alone(self):
        self.set_store_markup('10')
        self.client.force_login(self.owner)
        self.client.post(reverse('store_settings_update'), {'tin': '123'})
        self.assertEqual(StoreSetting.load().default_markup_percent, Decimal('10.00'))

    def test_manager_cannot_change_the_store_default(self):
        self.set_store_markup('10')
        self.client.force_login(self.manager)
        self.client.post(reverse('store_settings_update'), {'default_markup_percent': '99'})
        self.assertEqual(StoreSetting.load().default_markup_percent, Decimal('10.00'))


class RequestApprovalTests(PricingBase):
    def setUp(self):
        super().setUp()
        self.set_store_markup('25')
        self.req = SupplierProductRequest.objects.create(
            supplier=self.supplier, product_name='P02 Brand New',
            unit_of_measure='Piece', proposed_cost=Decimal('100.00'))
        self.client.force_login(self.owner)

    def review(self, price):
        return self.client.post(
            reverse('supplier_request_review', args=[self.supplier.pk, self.req.pk]),
            {'decision': 'approve', 'unit_price': price, 'category': self.category.pk})

    def test_form_prefills_the_computed_price(self):
        page = self.client.get(reverse('supplier_detail', args=[self.supplier.pk]))
        req = page.context['pending_requests'][0]
        self.assertEqual(req.price_hint, Decimal('140.00'))
        self.assertContains(page, 'value="140.00"')

    def test_category_markup_changes_the_per_category_hint(self):
        self.category.default_markup_percent = Decimal('0')
        self.category.save()
        page = self.client.get(reverse('supplier_detail', args=[self.supplier.pk]))
        req = page.context['pending_requests'][0]
        self.assertEqual(req.price_hint_by_category[self.category.pk], Decimal('112.00'))

    def test_no_markup_no_hint(self):
        self.set_store_markup(None)
        page = self.client.get(reverse('supplier_detail', args=[self.supplier.pk]))
        self.assertIsNone(page.context['pending_requests'][0].price_hint)

    def test_taking_the_suggestion_leaves_the_product_on_computed_pricing(self):
        self.review('140.00')
        self.assertFalse(Product.objects.get(product_name='P02 Brand New').price_is_manual)

    def test_typing_a_different_price_records_an_override(self):
        self.review('150.00')
        product = Product.objects.get(product_name='P02 Brand New')
        self.assertEqual(product.unit_price, Decimal('150.00'))
        self.assertTrue(product.price_is_manual)


class ApiAlignmentTests(PricingBase):
    def test_api_price_change_marks_the_override_like_the_form_does(self):
        self.set_store_markup('25')
        item = self.product('140.00')
        self.link(item, '100.00')
        self.client.force_login(self.owner)
        response = self.client.patch(
            f'/api/products/{item.pk}/', {'retailPrice': '150.00'},
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal('150.00'))
        self.assertTrue(item.price_is_manual)
