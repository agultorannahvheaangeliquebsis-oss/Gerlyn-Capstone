"""Build the storefront's two-level category tree.

Additive, and safe to run on a live catalog — which is what separates it from
seed_demo, whose first act is to delete every product, branch and sale. This
one creates only what is missing, files existing products into a shelf where
the product name settles it, and deletes or renames nothing. Running it twice
changes nothing the second time.

The tree it builds is a starting point, not a fixture: the owner adds to it,
renames it and re-parents it from Inventory > Categories like any other data.

"General" — the placeholder category a catalog starts life with because
Product needs one — is deliberately left in place. Once its products are
filed into real shelves it holds nothing, and
core.context_processors.storefront_categories stops drawing empty categories,
so it disappears from the shop by itself while staying where the owner
manages it.

    python manage.py seed_categories
    python manage.py seed_categories --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import Product, ProductCategory

# department -> its shelves, in the order a shopper reads them.
TREE = {
    'Rice, Oils & Sugars': ['Rice', 'Cooking Oil', 'Sugar & Salt'],
    'Canned Goods': ['Sardines & Fish', 'Meat & Loaf', 'Canned Fruits & Vegetables'],
    'Noodles & Pasta': ['Instant Noodles', 'Pasta & Sauces'],
    'Beverages': ['Coffee & Creamer', 'Juices & Softdrinks', 'Drinking Water'],
    'Dairy & Chilled': ['Milk & Powdered Milk', 'Cheese & Spreads'],
    'Snacks & Sweets': ['Biscuits', 'Chips & Nuts', 'Candies & Chocolates'],
    'Condiments & Seasonings': ['Sauces & Vinegar', 'Seasoning Mixes'],
    'Household': ['Laundry & Cleaning', 'Paper & Disposables'],
    'Health & Beauty': ['Personal Care', 'Baby Care'],
}

# sku -> the shelf it belongs on. Only the products whose name settles it
# beyond doubt; anything not listed here keeps the category it already has,
# because guessing at a filing is worse than leaving it for the owner.
FILING = {
    'DEMO-001': 'Rice',
    'DEMO-002': 'Cooking Oil',
    'DEMO-003': 'Sardines & Fish',
}


class Command(BaseCommand):
    help = "Create the storefront's two-level category tree. Additive; deletes nothing."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options['dry_run']
        shelves = {}
        created = []
        reparented = []

        for department_name, shelf_names in TREE.items():
            # get_or_create on the name, so a category the owner already
            # typed in is adopted into the tree rather than duplicated
            # alongside it under the same label.
            department = ProductCategory.objects.filter(
                category_name=department_name
            ).first()
            if department is None:
                created.append(department_name)
                if not dry_run:
                    department = ProductCategory.objects.create(
                        category_name=department_name
                    )

            for shelf_name in shelf_names:
                shelf = ProductCategory.objects.filter(category_name=shelf_name).first()
                if shelf is None:
                    created.append(shelf_name)
                    if not dry_run:
                        shelf = ProductCategory.objects.create(
                            category_name=shelf_name, parent=department
                        )
                elif shelf.parent_id is None:
                    # An existing row that is already filed somewhere keeps
                    # its own parent — this command files orphans, it does
                    # not overrule a decision the owner has made.
                    reparented.append(f'{shelf_name} -> {department_name}')
                    if not dry_run:
                        shelf.parent = department
                        shelf.save(update_fields=['parent'])
                shelves[shelf_name] = shelf

        filed = []
        for sku, shelf_name in FILING.items():
            product = Product.objects.filter(sku_code=sku).first()
            if product is None or shelves.get(shelf_name) is None:
                continue
            if product.category_id == getattr(shelves[shelf_name], 'pk', None):
                continue
            filed.append(f'{product.product_name} -> {shelf_name}')
            if not dry_run:
                product.category = shelves[shelf_name]
                product.save(update_fields=['category'])

        for label, rows in (
            ('created', created), ('filed under a department', reparented),
            ('products moved', filed),
        ):
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
