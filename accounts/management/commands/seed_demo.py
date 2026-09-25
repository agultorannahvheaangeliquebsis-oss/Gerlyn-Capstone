"""
seed_demo — populate GerlynPLUS with believable demo data.

Matches the UI mockups: 3 branches, the 15-product catalog, per-branch stock
(including the 6 low/critical-stock alerts), 8 suppliers, 7 purchase orders,
and 10 orders — so every screen renders with real data and the 5 acceptance
scenarios can be demonstrated.

Usage:
    python manage.py seed_demo            # create if empty (idempotent master data)
    python manage.py seed_demo --reset    # wipe demo tables first, then recreate

Demo login password for ALL seeded users:  demo1234
"""
import datetime as dt
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.utils import timezone

from audit.context import suppressed
from accounts.models import (
    BranchManagerProfile,
    CustomerProfile,
    Notification,
    OwnerProfile,
    StaffProfile,
    SupplierCategory,
    SupplierProfile,
    User,
)
from billing.models import BillingRecord
from branches.models import Branch
from inventory.models import (
    Batch,
    Inventory,
    Product,
    ProductCategory,
    Warehouse,
    WarehouseStock,
)
from procurement.models import Procurement, ProcurementItem
from sales.models import SalesItem, SalesTransaction

PASSWORD = "demo1234"
D = Decimal


class Command(BaseCommand):
    help = "Seed GerlynPLUS with demo data matching the UI mockups."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing demo data before seeding.",
        )

    @transaction.atomic
    @suppressed()
    def handle(self, *args, **opts):
        # Honour --verbosity so the test runner's output stays readable.
        self.quiet = opts.get("verbosity", 1) == 0

        if opts["reset"]:
            self._reset()

        self._say(self.style.MIGRATE_HEADING("Seeding GerlynPLUS demo data..."))

        owner = self._owner()
        branches = self._branches(owner)
        self._managers(branches)
        staff = self._staff(branches)
        suppliers = self._suppliers()
        customers = self._customers()
        categories = self._categories()
        products = self._products(categories)
        self._warehouses(branches)
        self._inventory_and_batches(products, branches)

        if not Procurement.objects.exists():
            self._purchase_orders(branches, suppliers, owner, products)
        else:
            self._say("  Purchase orders already present — skipped.")

        if not SalesTransaction.objects.exists():
            self._orders(branches, staff, customers, products)
        else:
            self._say("  Orders already present — skipped.")

        self._summary()

    def _say(self, message):
        """Write unless --verbosity 0."""
        if not getattr(self, "quiet", False):
            self.stdout.write(message)

    # ------------------------------------------------------------------ reset
    def _reset(self):
        self._say(self.style.WARNING("Resetting demo tables..."))
        SalesItem.objects.all().delete()
        BillingRecord.objects.all().delete()
        SalesTransaction.objects.all().delete()
        ProcurementItem.objects.all().delete()
        Batch.objects.all().delete()
        Procurement.objects.all().delete()
        Inventory.objects.all().delete()
        WarehouseStock.objects.all().delete()
        Warehouse.objects.all().delete()
        Product.objects.all().delete()
        ProductCategory.objects.all().delete()
        Branch.objects.all().delete()
        # Owner Notification Center rows (core.notifications.notify()) aren't
        # demo/master data, but they reference the demo Users/Branches this
        # reset is about to delete out from under them, and re-seeding itself
        # raises a few (e.g. accounts.signals.create_user_profile notifies on
        # every new Staff account) — so a reset that skipped this left old
        # notifications pointing at deleted branches, piling up further with
        # every re-seed. Deleted before the User delete below rather than
        # relying on the FK's CASCADE, so admin's own notifications (excluded
        # from that delete) are cleared too.
        Notification.objects.all().delete()
        # keep no demo users lingering
        User.objects.exclude(is_superuser=True, username="admin").delete()

        # Reset AUTO_INCREMENT so auto-numbers restart at 001 (MySQL keeps the
        # counter across DELETEs, otherwise ORD/PO numbers drift after re-seeds).
        from django.db import connection

        tables = [
            m._meta.db_table
            for m in (
                SalesItem, BillingRecord, SalesTransaction, ProcurementItem,
                Batch, Procurement, Inventory, WarehouseStock, Warehouse,
                Product, ProductCategory, Branch, Notification,
            )
        ]
        with connection.cursor() as cur:
            for t in tables:
                cur.execute(f"ALTER TABLE `{t}` AUTO_INCREMENT = 1")

    # ------------------------------------------------------------------ helper
    def _make_user(self, username, role, is_active=True, **extra):
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"role": role, "is_active": is_active, **extra},
        )
        if created:
            user.set_password(PASSWORD)
            user.save()
        return user

    # ------------------------------------------------------------------ owner
    def _owner(self):
        owner = self._make_user(
            "gerlyn", User.Role.OWNER,
            email="gerlyn@gerlynvariety.ph",
            first_name="Gerlyn", last_name="Santos",
            is_staff=True, is_superuser=True,
        )
        # Profiles are auto-created (blank) by accounts.signals on user creation,
        # so update_or_create to write the real names/branches.
        OwnerProfile.objects.update_or_create(
            user=owner,
            defaults={"owner_name": "Gerlyn Santos", "contact_number": "0917-000-0001"},
        )
        return owner

    # --------------------------------------------------------------- branches
    def _branches(self, owner):
        data = [
            ("Tarcan", "Brgy. Tarcan, Baliuag, Bulacan"),
            ("Makinabang", "Brgy. Makinabang, Baliuag, Bulacan"),
            ("Tiaong Baliwag Mini Mart", "Brgy. Tiaong, Baliuag, Bulacan"),
        ]
        branches = {}
        for name, loc in data:
            b, _ = Branch.objects.get_or_create(
                branch_name=name, defaults={"owner": owner, "location": loc},
            )
            branches[name] = b
        return branches

    def _managers(self, branches):
        specs = [
            ("carlo", "Carlo Reyes", "Tarcan", "0917-000-0002"),
            ("edu", "Eduardo Lim", "Tiaong Baliwag Mini Mart", "0917-000-0003"),
        ]
        for username, name, branch_name, contact in specs:
            u = self._make_user(
                username, User.Role.BRANCH_MANAGER,
                email=f"{username}@gerlynvariety.ph",
                first_name=name.split()[0], last_name=name.split()[-1],
            )
            BranchManagerProfile.objects.update_or_create(
                user=u,
                defaults={
                    "branch": branches[branch_name],
                    "manager_name": name,
                    "contact_number": contact,
                },
            )

    def _staff(self, branches):
        specs = [
            ("ana", "Ana Reyes", "Tarcan", True),
            ("ben", "Benedicto Cruz", "Makinabang", True),
            ("rose", "Roselyn Diaz", "Tiaong Baliwag Mini Mart", False),
        ]
        staff = {}
        for username, name, branch_name, active in specs:
            u = self._make_user(
                username, User.Role.STAFF, is_active=active,
                email=f"{username}@gerlynvariety.ph",
                first_name=name.split()[0], last_name=name.split()[-1],
            )
            StaffProfile.objects.update_or_create(
                user=u,
                defaults={
                    "branch": branches[branch_name],
                    "staff_name": name,
                    "contact_number": "0918-000-0000",
                },
            )
            staff[branch_name] = u
        return staff

    def _suppliers(self):
        specs = [
            ("century", "Century Pacific Food Inc.", "Canned Goods", "Ricardo Buenaventura", "0917-123-4567"),
            ("monde", "Monde Nissin Corporation", "Noodles & Pasta", "Maria Santos", "0918-234-5678"),
            ("nestle", "Nestlé Philippines Inc.", "Beverages & Dairy", "Jose Dela Cruz", "0919-345-6789"),
            ("sanmiguel", "San Miguel Purefoods Co. Inc.", "Canned Goods", "Lina Reyes", "0920-456-7890"),
            ("delmonte", "Del Monte Philippines Inc.", "Condiments", "Fernando Aguilar", "0921-567-8901"),
            ("unilever", "Unilever Philippines", "Personal Care & Condiments", "Carla Mendez", "0922-678-9012"),
            ("luckyme", "Lucky Me! (Monde Nissin)", "Noodles & Pasta", "Tony Cruz", "0923-789-0123"),
            ("pg", "Procter & Gamble Philippines", "Baby & Personal Care", "Grace Villanueva", "0924-890-1234"),
        ]
        suppliers = {}
        for username, name, cat, person, contact in specs:
            u = self._make_user(
                username, User.Role.SUPPLIER,
                email=f"{username}@supplier.ph", first_name=name.split()[0],
            )
            category, _ = SupplierCategory.objects.get_or_create(name=cat)
            SupplierProfile.objects.update_or_create(
                user=u,
                defaults={
                    "supplier_name": name,
                    "contact_number": contact,
                    "category": category,
                    "contact_person": person,
                },
            )
            suppliers[name] = u
        return suppliers

    def _customers(self):
        specs = [
            ("juan", "Juan", "dela Cruz"),
            ("maria", "Maria", "Santos"),
            ("pedro", "Pedro", "Ramos"),
            ("anav", "Ana", "Villanueva"),
        ]
        customers = {}
        for username, fn, ln in specs:
            u = self._make_user(
                username, User.Role.CUSTOMER,
                email=f"{username}@customer.ph", first_name=fn, last_name=ln,
            )
            CustomerProfile.objects.update_or_create(
                user=u,
                defaults={
                    "first_name": fn, "last_name": ln,
                    "contact_number": "0999-000-0000",
                    "address": "Baliuag, Bulacan",
                },
            )
            customers[f"{fn} {ln}"] = u
        return customers

    # ------------------------------------------------------------- categories
    def _categories(self):
        names = [
            "Canned Goods", "Noodles & Pasta", "Beverages", "Dairy & Milk",
            "Condiments", "Baby & Personal Care", "Rice & Grains", "Snacks",
        ]
        cats = {}
        for n in names:
            c, _ = ProductCategory.objects.get_or_create(category_name=n)
            cats[n] = c
        return cats

    def _products(self, cats):
        # (sku, name, category, unit, retail, [(buy at least, price per unit), ...])
        # Bulk breaks sit at a plausible case/box size for each item. SKU-001
        # and SKU-007 carry a full three-step ladder so the demo shows one.
        # NOTE: unit strings (Can/Pack/Bottle/Box) are stored for demo fidelity;
        # extend Product.UnitOfMeasure choices on Day 2 (Inventory) so forms accept them.
        specs = [
            ("SKU-001", "Century Tuna Flakes in Oil 155g", "Canned Goods", "Can", 32, [(12, 29), (24, 27), (48, 25)]),
            ("SKU-002", "555 Sardines in Tomato Sauce 155g", "Canned Goods", "Can", 26, [(12, 22)]),
            ("SKU-003", "Mega Sardines in Tomato Sauce 155g", "Canned Goods", "Can", 24, [(12, 20)]),
            ("SKU-004", "Lucky Me Pancit Canton Original 80g", "Noodles & Pasta", "Pack", 15, [(24, 12)]),
            ("SKU-005", "Argentina Vienna Sausage 230g", "Canned Goods", "Can", 48, [(12, 41)]),
            ("SKU-006", "Purefoods Corned Beef 210g", "Canned Goods", "Can", 58, [(12, 50)]),
            ("SKU-007", "Bear Brand Powdered Milk 300g", "Dairy & Milk", "Pack", 85, [(6, 80), (12, 74), (24, 70)]),
            ("SKU-008", "Nescafe 3-in-1 Original 28g x10", "Beverages", "Box", 95, [(6, 83)]),
            ("SKU-009", "Del Monte Tomato Sauce 250g", "Condiments", "Pack", 28, [(12, 23)]),
            ("SKU-010", "Pampers Diapers Medium 28s", "Baby & Personal Care", "Pack", 345, [(4, 308)]),
            ("SKU-011", "Silver Swan Soy Sauce 1L", "Condiments", "Bottle", 42, [(12, 36)]),
            ("SKU-012", "CDO Karne Norte 150g", "Canned Goods", "Can", 38, [(12, 32)]),
            ("SKU-013", "Knorr Sinigang Mix 40g", "Condiments", "Pack", 12, [(24, 9)]),
            ("SKU-014", "UFC Banana Ketchup 320g", "Condiments", "Bottle", 35, [(12, 29)]),
            ("SKU-015", "Milo Activ-Go 400g", "Beverages", "Pack", 155, [(6, 130)]),
        ]
        products = {}
        for sku, name, cat, unit, retail, tiers in specs:
            p, _ = Product.objects.get_or_create(
                sku_code=sku,
                defaults={
                    "product_name": name,
                    "category": cats[cat],
                    "unit_of_measure": unit,
                    "unit_price": D(retail),
                },
            )
            # get_or_create per rung keeps a re-seed idempotent — no duplicate
            # breaks, and no clobbering a price someone has since edited.
            for min_quantity, price in tiers:
                p.price_tiers.get_or_create(
                    min_quantity=min_quantity, defaults={"unit_price": D(price)},
                )
            products[sku] = p
        return products

    def _warehouses(self, branches):
        for name, b in branches.items():
            Warehouse.objects.get_or_create(
                branch=b, defaults={"name": f"{name} Warehouse"},
            )

    # -------------------------------------------------------------- inventory
    def _inventory_and_batches(self, products, branches):
        T, M, G = "Tarcan", "Makinabang", "Tiaong Baliwag Mini Mart"
        DEFAULT = (150, 50)

        # Which branches carry each SKU (matches "Availability" column).
        availability = {
            "SKU-001": [T, M, G], "SKU-002": [T, M], "SKU-003": [M, G],
            "SKU-004": [T, M, G], "SKU-005": [T, M, G], "SKU-006": [T, M],
            "SKU-007": [T, G], "SKU-008": [T, M, G], "SKU-009": [T, G],
            "SKU-010": [T], "SKU-011": [T, M, G], "SKU-012": [G],
            "SKU-013": [T, M, G], "SKU-014": [T, M], "SKU-015": [T, M, G],
        }
        # Explicit (qty, reorder) — produces the 6 low/critical alerts.
        levels = {
            ("SKU-001", T): (332, 80), ("SKU-001", M): (180, 60), ("SKU-001", G): (95, 40),
            ("SKU-002", T): (255, 60), ("SKU-002", M): (48, 60),   # Makinabang LOW
            ("SKU-003", M): (46, 80), ("SKU-003", G): (120, 50),   # Makinabang LOW
            ("SKU-004", T): (280, 70), ("SKU-004", M): (310, 70), ("SKU-004", G): (95, 40),
            ("SKU-005", T): (195, 50), ("SKU-005", M): (88, 50), ("SKU-005", G): (150, 50),
            ("SKU-006", T): (35, 60), ("SKU-006", M): (120, 50),   # Tarcan LOW
            ("SKU-009", T): (150, 40), ("SKU-009", G): (22, 40),   # Tiaong CRITICAL
            ("SKU-012", G): (15, 40),                              # Tiaong CRITICAL
            ("SKU-014", T): (120, 40), ("SKU-014", M): (35, 40),   # Makinabang LOW
        }
        # Nearest-expiry per SKU (for Stock/Alerts expiry column).
        expiry = {
            "SKU-001": dt.date(2026, 5, 1),
            "SKU-002": dt.date(2027, 1, 15),
            "SKU-003": dt.date(2026, 4, 20),
            "SKU-004": dt.date(2026, 12, 1),
            "SKU-005": dt.date(2027, 6, 15),
        }
        default_expiry = dt.date(2027, 1, 1)

        for sku, product in products.items():
            for branch_name in availability[sku]:
                branch = branches[branch_name]
                qty, reorder = levels.get((sku, branch_name), DEFAULT)
                exp = expiry.get(sku, default_expiry)
                inv, created = Inventory.objects.get_or_create(
                    product=product, branch=branch,
                    defaults={"quantity_on_hand": qty, "reorder_level": reorder,
                              "expiry_date": exp},
                )
                # Backroom (warehouse) stock — source for withdrawal slips.
                warehouse = Warehouse.objects.get(branch=branch)
                WarehouseStock.objects.get_or_create(
                    warehouse=warehouse, product=product,
                    defaults={"quantity_on_hand": qty * 2, "reorder_level": reorder},
                )
                if created:
                    Batch.objects.get_or_create(
                        product=product, branch=branch, location=Batch.Location.FLOOR,
                        expiry_date=exp,
                        defaults={
                            "quantity": qty,
                            "received_date": timezone.now().date() - dt.timedelta(days=30),
                        },
                    )

        # A few near-expiry batches so the Expiry Alerts panel has data
        # (expiring within the 14-day window relative to "today").
        today = timezone.now().date()
        near = [
            ("SKU-005", G, 3, 10),
            ("SKU-001", T, 7, 20),
            ("SKU-004", M, 10, 15),
            ("SKU-008", T, 12, 8),
            ("SKU-011", T, 14, 25),
        ]
        for sku, branch_name, days, qty in near:
            product = products[sku]
            branch = branches[branch_name]
            _batch, batch_created = Batch.objects.get_or_create(
                product=product, branch=branch,
                location=Batch.Location.FLOOR,
                expiry_date=today + dt.timedelta(days=days),
                defaults={"quantity": qty, "received_date": today},
            )
            # This is an ADDITIONAL lot on top of whatever the main loop
            # above already created for this product/branch, so the
            # aggregate row has to grow with it — otherwise batches would
            # sum to more than quantity_on_hand, and the storefront/Floor
            # Stock would show more stock than the row claims to have.
            if batch_created:
                Inventory.objects.filter(product=product, branch=branch).update(
                    quantity_on_hand=models.F("quantity_on_hand") + qty
                )

    # --------------------------------------------------------------------- PO
    def _purchase_orders(self, branches, suppliers, owner, products):
        T, M, G = "Tarcan", "Makinabang", "Tiaong Baliwag Mini Mart"
        S = suppliers
        P = products
        today = timezone.localtime().date()
        # Relative dates again — a PO dated last March would show as months
        # overdue forever. One is deliberately left overdue to demo that state.
        # (branch, supplier, ordered_days_ago, due_in_days, status, total, [(sku, qty, cost)])
        pos = [
            (T, "Century Pacific Food Inc.", 12, -8, "Received", 9500,
             [("SKU-001", 200, 27), ("SKU-005", 100, 41)]),
            (M, "Monde Nissin Corporation", 10, -6, "Received", 3600,
             [("SKU-004", 300, 12)]),
            (G, "Del Monte Philippines Inc.", 4, 3, "Incoming", 2920,
             [("SKU-009", 80, 23), ("SKU-014", 40, 29)]),
            (T, "San Miguel Purefoods Co. Inc.", 8, -2, "Incoming", 5000,   # overdue on purpose
             [("SKU-006", 100, 50)]),
            (M, "Century Pacific Food Inc.", 1, 6, "Draft", 5700,
             [("SKU-002", 150, 22), ("SKU-003", 120, 20)]),
            (T, "Nestlé Philippines Inc.", 14, -10, "Received", 16200,
             [("SKU-007", 100, 74), ("SKU-008", 80, 83), ("SKU-015", 15, 130)]),
            (G, "Century Pacific Food Inc.", 20, -17, "Cancelled", 1600,
             [("SKU-001", 60, 27)]),
        ]
        for branch, supplier, ordered_ago, due_in, status, total, items in pos:
            po = Procurement.objects.create(
                branch=branches[branch], supplier=S[supplier], owner=owner,
                created_by=owner,
                order_date=today - dt.timedelta(days=ordered_ago),
                delivery_date=today + dt.timedelta(days=due_in),
                actual_receipt_date=(
                    today + dt.timedelta(days=due_in)
                    if status == "Received" else None
                ),
                status=status, total_cost=D(total),
                funding_source=Procurement.FundingSource.BANK,
                funding_note='Demo data',
            )
            for sku, qty, cost in items:
                ProcurementItem.objects.create(
                    procurement=po, product=P[sku],
                    quantity_ordered=qty,
                    quantity_received=qty if status == "Received" else 0,
                    cost_price=D(cost),
                )

    # ------------------------------------------------------------------ orders
    def _orders(self, branches, staff, customers, products):
        T, M, G = "Tarcan", "Makinabang", "Tiaong Baliwag Mini Mart"
        P = products
        Tx = SalesTransaction
        today = timezone.localtime().date()

        # Dates are RELATIVE to today (days_ago), not fixed. Otherwise the demo
        # rots: "today's sales" reads ₱0 and the trend chart renders empty the
        # moment you seed on a later date than the one hard-coded here.
        # (days_ago, customer|None, branch, channel, mop, pay_status, order_status, total, [(sku,qty,price)])
        #
        # Item prices are what each sale was frozen at when it was rung up —
        # SalesItem.unit_price is history, not a live lookup — so they are not
        # expected to match today's bulk ladder above.
        orders = [
            (0, None, T, "Physical", "Cash", "Paid", "Completed", 310,
             [("SKU-001", 5, 32), ("SKU-004", 10, 15)]),
            (0, "Juan dela Cruz", T, "Online", "Cash on Delivery", "Pending", "Approved", 334,
             [("SKU-005", 5, 48), ("SKU-013", 8, 12)]),
            (0, None, M, "Physical", "Cash", "Paid", "Completed", 1104,
             [("SKU-007", 12, 74), ("SKU-011", 2, 36)]),
            (1, "Maria Santos", M, "Online", "Pay at Store", "Pending", "Preparing", 435,
             [("SKU-004", 15, 15), ("SKU-003", 9, 24)]),
            (1, None, G, "Physical", "Cash", "Paid", "Completed", 120,
             [("SKU-013", 10, 12)]),
            (2, "Pedro Ramos", T, "Online", "Cash on Delivery", "Pending", "Pending", 200,
             [("SKU-001", 5, 32), ("SKU-013", 3, 12)]),
            (3, None, T, "Physical", "Cash", "Paid", "Completed", 407,
             [("SKU-006", 5, 58), ("SKU-011", 3, 42)]),
            (5, "Ana Villanueva", T, "Online", "GCash", "Paid", "Completed", 775,
             [("SKU-010", 2, 345), ("SKU-008", 1, 95)]),
            (7, "Juan dela Cruz", M, "Online", "Cash on Delivery", "Paid", "Completed", 294,
             [("SKU-004", 12, 15), ("SKU-013", 9, 12)]),
            (9, None, M, "Physical", "Cash", "Paid", "Completed", 1992,
             [("SKU-010", 5, 308), ("SKU-007", 3, 74)]),
            (11, None, G, "Physical", "Cash", "Paid", "Completed", 645,
             [("SKU-008", 3, 95), ("SKU-002", 6, 26)]),
            (13, None, T, "Physical", "Cash", "Paid", "Completed", 1230,
             [("SKU-005", 30, 41)]),
        ]
        for days_ago, cust, branch_name, channel, mop, pay_status, order_status, total, items in orders:
            odate = (today - dt.timedelta(days=days_ago)).isoformat()
            branch = branches[branch_name]
            tx = Tx.objects.create(
                branch=branch,
                staff=staff.get(branch_name) if channel == "Physical" else None,
                customer=customers.get(cust) if cust else None,
                total_amount=D(total),
                transaction_type=channel,
                mode_of_payment=mop,
                order_status=order_status,
                delivery_address="Baliuag, Bulacan" if channel == "Online" else "",
            )
            # Backfill the display date (auto_now_add can't be set at create()).
            aware = timezone.make_aware(
                dt.datetime.fromisoformat(odate + "T10:00:00")
            )
            Tx.objects.filter(pk=tx.pk).update(transaction_date=aware)
            for sku, qty, price in items:
                SalesItem.objects.create(
                    transaction=tx, product=P[sku],
                    quantity_sold=qty, unit_price=D(price),
                )
            # billing.signals auto-creates a BillingRecord on the sale's first
            # save; update it to reflect this order's payment method/status.
            BillingRecord.objects.filter(transaction=tx).update(
                amount_due=D(total),
                payment_method=mop,
                status=pay_status,
                reference_number="GC-2026-0001" if mop == "GCash" else "",
            )

    # ----------------------------------------------------------------- summary
    def _summary(self):
        if getattr(self, "quiet", False):
            return
        self.stdout.write(self.style.SUCCESS("\n✔ Demo data ready.\n"))
        self.stdout.write(f"  Branches ........ {Branch.objects.count()}")
        self.stdout.write(f"  Users ........... {User.objects.count()}")
        self.stdout.write(f"  Products ........ {Product.objects.count()}")
        self.stdout.write(f"  Inventory rows .. {Inventory.objects.count()}")
        self.stdout.write(f"  Batches ......... {Batch.objects.count()}")
        low = Inventory.objects.filter(restock=True).count()
        self.stdout.write(f"  Low/critical .... {low}")
        self.stdout.write(f"  Purchase orders . {Procurement.objects.count()}")
        self.stdout.write(f"  Orders .......... {SalesTransaction.objects.count()}")
        self.stdout.write(f"  Billing records . {BillingRecord.objects.count()}")
        self.stdout.write(self.style.MIGRATE_HEADING("\n  Logins (password: demo1234)"))
        for u, r in [
            ("gerlyn", "Owner — all branches"),
            ("carlo", "Manager — Tarcan"),
            ("edu", "Manager — Tiaong"),
            ("ana", "Staff — Tarcan"),
            ("ben", "Staff — Makinabang"),
            ("juan", "Customer"),
            ("century", "Supplier"),
        ]:
            self.stdout.write(f"    {u:<10} {r}")
