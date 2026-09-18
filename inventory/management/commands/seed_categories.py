"""Create the storefront's starting set of categories, flat.

Additive, and safe to run on a live catalog — which is what separates it from
seed_demo, whose first act is to delete every product, branch and sale. This
one creates only what is missing, files existing products into a category
where the product name settles it, and deletes or renames nothing. Running it
twice changes nothing the second time.

The set it creates is a starting point, not a fixture: the owner adds to it,
renames it and reassigns it from Inventory > Categories like any other data.

"General" — the placeholder category a catalog starts life with because
Product needs one — is deliberately left in place. Once its products are
filed into real categories it holds nothing, and
core.context_processors.storefront_categories stops drawing empty categories,
so it disappears from the shop by itself while staying where the owner
manages it.

    python manage.py seed_categories
    python manage.py seed_categories --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import Product, ProductCategory

# Every category to create, flat.
CATEGORIES = [
    'Rice, Oils & Sugars', 'Rice', 'Cooking Oil', 'Sugar & Salt',
    'Canned Goods', 'Sardines & Fish', 'Meat & Loaf', 'Canned Fruits & Vegetables',
    'Noodles & Pasta', 'Instant Noodles', 'Pasta & Sauces',
    'Beverages', 'Coffee & Creamer', 'Juices & Softdrinks', 'Drinking Water',
    'Dairy & Chilled', 'Milk & Powdered Milk', 'Cheese & Spreads',
    'Snacks & Sweets', 'Biscuits', 'Chips & Nuts', 'Candies & Chocolates',
    'Condiments & Seasonings', 'Sauces & Vinegar', 'Seasoning Mixes',
    'Household', 'Laundry & Cleaning', 'Paper & Disposables',
    'Health & Beauty', 'Personal Care', 'Baby Care',
]

# sku -> the category it belongs in. Only the products whose name settles it
# beyond doubt; anything not listed here keeps the category it already has,
# because guessing at a filing is worse than leaving it for the owner.
FILING = {
    'DEMO-001': 'Rice',
    'DEMO-002': 'Cooking Oil',
    'DEMO-003': 'Sardines & Fish',
}


class Command(BaseCommand):
    help = "Create the storefront's starting categories, flat. Additive; deletes nothing."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options['dry_run']
        categories = {}
        created = []

        for name in CATEGORIES:
            # get_or_create on the name, so a category the owner already
            # typed in is adopted rather than duplicated alongside it under
            # the same label.
            category = ProductCategory.objects.filter(category_name=name).first()
            if category is None:
                created.append(name)
                if not dry_run:
                    category = ProductCategory.objects.create(category_name=name)
            categories[name] = category

        filed = []
        for sku, category_name in FILING.items():
            product = Product.objects.filter(sku_code=sku).first()
            if product is None or categories.get(category_name) is None:
                continue
            if product.category_id == getattr(categories[category_name], 'pk', None):
                continue
            filed.append(f'{product.product_name} -> {category_name}')
            if not dry_run:
                product.category = categories[category_name]
                product.save(update_fields=['category'])

        for label, rows in (('created', created), ('products moved', filed)):
            self.stdout.write(f'{label}: {len(rows)}')
            for row in rows:
                self.stdout.write(f'  {row}')

        if dry_run:
            self.stdout.write(self.style.WARNING('dry run — nothing written'))
            transaction.set_rollback(True)
        else:
            # The internal pickers read categories through a 60-second cache;
            # without this the Add Product dropdown would not show any of
            # this until it expired.
            from core.refdata import bust_categories_cache

            bust_categories_cache()
            self.stdout.write(self.style.SUCCESS(
                f'{ProductCategory.objects.count()} categories in the catalog'
            ))
