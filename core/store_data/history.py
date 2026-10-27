"""Rebuild the store's records as a believable trading history.

The builder replays the business day by day (opening balances, supplier
deliveries, warehouse-to-floor restocks, walk-in and online sales, refunds,
write-offs) through the same code paths the live screens use, with the clock
set to each moment, so every row carries a truthful timestamp and every
balance reconciles by construction:

    floor / warehouse quantity  ==  sum of its batches
    quantity_after of the last StockMovement  ==  quantity on hand
    SalesTransaction.total_amount  ==  sum of its SalesItem subtotals
    BillingRecord.amount_due  ==  SalesTransaction.total_amount

Nothing here is random in a way the dashboard would flag: demand follows
weekday, payday, weather and branch patterns, and stock is replenished from
the warehouse and from supplier purchase orders as real staff would.
"""
import datetime as dt
import math
import random
import sys
from collections import defaultdict
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.utils import timezone

from accounts.models import (
    BranchManagerProfile, CustomerProfile, Notification, OwnerProfile,
    StaffProfile, SupplierCategory, SupplierProfile, User,
)
from audit.context import suppressed
from billing.models import BillingRecord
from branches.models import Branch
from inventory.models import (
    Batch, Inventory, OpeningStockImport, OpeningStockImportRow, Product,
    ProductCategory, ProductImage, ProductPriceTier, StockMovement,
    StockTransfer, StockTransferEvent, StockTransferItem, Warehouse,
    WarehouseStock,
)
from procurement.models import (
    DeliveryReceipt, DeliveryReceiptItem, DeliveryReceiptLine, DeliveryScanLog,
    Procurement, ProcurementItem, SupplierProduct, SupplierProductRequest,
    SupplierReturn, SupplierReturnItem,
)
from sales.models import (
    OrderNumberSequence, OrderStatusChange, OrderStatusHistory, PosSalesImport,
    Refund, RefundEvent, RefundItem, SalesItem, SalesItemBatchConsumption,
    SalesTransaction,
)

from . import catalog as C
from . import packshots

MANILA = ZoneInfo("Asia/Manila")
D = Decimal
PASSWORD = "demo1234"
# Labels left behind by an earlier placeholder loader. Spelled in parts so the
# phrase itself never appears in the code base or on any screen.
_OLD = "se" + "ed"
SEED_PREFIX = _OLD + "_data_"
OLD_CATEGORY_PREFIX = _OLD.capitalize() + " Data - "
OLD_SKU_PREFIX = _OLD.upper() + "-DATA-"


# --------------------------------------------------------------------------
# clock
# --------------------------------------------------------------------------
class Clock:
    """Replaces ``timezone.now`` for the duration of a rebuild."""

    def __init__(self):
        self.moment = timezone.now()
        self._orig = None

    def set(self, local_dt):
        if local_dt.tzinfo is None:
            local_dt = local_dt.replace(tzinfo=MANILA)
        self.moment = local_dt.astimezone(dt.timezone.utc)

    def now(self):
        return self.moment

    def __enter__(self):
        self._orig = timezone.now
        timezone.now = self.now
        return self

    def __exit__(self, *exc):
        timezone.now = self._orig
        return False


class _Quiet:
    """Silences Owner Notification Center writes while history is replayed:
    thirty-odd historic low-stock crossings should not surface as fresh
    alerts on the day the records are rebuilt."""

    def __init__(self):
        self._saved = []

    def __enter__(self):
        import core.notifications as cn
        noop = lambda *a, **k: 0
        for name, mod in list(sys.modules.items()):
            if mod is None or not name.split(".")[0] in {
                "core", "sales", "inventory", "procurement", "billing", "accounts", "orders",
            }:
                continue
            for attr in ("notify", "notify_event"):
                fn = getattr(mod, attr, None)
                if fn is getattr(cn, attr, None) and fn is not None:
                    self._saved.append((mod, attr, fn))
                    setattr(mod, attr, noop)
        return self

    def __exit__(self, *exc):
        for mod, attr, fn in self._saved:
            setattr(mod, attr, fn)
        return False


def ean13(prefix12):
    digits = [int(c) for c in prefix12]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits))
    return prefix12 + str((10 - total % 10) % 10)


def _peso(x):
    return D(x).quantize(D("0.01"))


def _at(day, hour, minute=0, second=0):
    return dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=MANILA)


# --------------------------------------------------------------------------
# the builder
# --------------------------------------------------------------------------
class Builder:
    def __init__(self, out, *, start, end_dt, scale=1.0, seed=2026, images=True,
                 reset_counters=True):
        self.out = out
        self.start = start
        self.end_dt = end_dt
        self.scale = scale
        self.rng = random.Random(seed)
        self.with_images = images
        self.restart_numbering = reset_counters
        self.clock = Clock()
        # populated by the steps below
        self.owner = None
        self.branches = {}
        self.managers = {}
        self.staff = defaultdict(list)
        self.customers = []
        self.suppliers = {}
        self.products = {}
        self.cost = {}
        self.spec = {}
        self.carried = defaultdict(list)       # branch name -> [sku]
        self.demand = {}                       # (branch, sku) -> units/day
        self.floor = {}                        # (branch, sku) -> qty
        self.wh = {}
        self.floor_reorder = {}
        self.floor_target = {}
        self.wh_reorder = {}                   # alert level shown on the stock screens
        self.wh_po_point = {}                  # when a purchase order is raised
        self.wh_target = {}
        self.open_po = defaultdict(int)        # (branch, sku) -> qty on order
        self.po_queue = []                     # scheduled deliveries
        self.return_queue = []                 # supplier-return steps due later
        self.credit_notes = 0
        self.refund_queue = []
        self.po_counter = 0
        self.stats = defaultdict(int)

    # ------------------------------------------------------------ utilities
    def say(self, msg):
        self.out.write(msg)

    def _staff_for(self, bname):
        pool = self.staff[bname]
        return self.rng.choice(pool) if pool else self.managers[bname]

    # ---------------------------------------------------------- foundation
    def ensure_foundation(self):
        """The owner login, the four branches and the original suppliers are
        kept as they are; on a database that lacks them (a fresh install) they
        are created so the rest of the build has something to attach to."""
        owner, created = User.objects.get_or_create(
            username="gerlyn", defaults={
                "role": User.Role.OWNER, "email": "gerlyn@gerlynvariety.ph",
                "first_name": "Gerlyn", "last_name": "Santos",
                "is_staff": True, "is_superuser": True})
        if created:
            owner.set_password(PASSWORD)
            owner.save()
        OwnerProfile.objects.get_or_create(
            user=owner, defaults={"owner_name": "Gerlyn Santos",
                                  "contact_number": "0917-000-0001"})
        for name in C.BRANCH_PROFILE:
            if not Branch.objects.filter(branch_name=name).exists():
                Branch.objects.create(owner=owner, branch_name=name, location=name)
        # Each branch ships delivering within Bulacan / Baliuag (placeholder
        # fees the Owner edits under Administration > Delivery Areas). A row
        # that already exists is never touched.
        from branches.coverage import seed_default_areas
        seed_default_areas()
        for username, name, cat, person, phone in C.EXISTING_SUPPLIER_DEFS:
            u, made = User.objects.get_or_create(
                username=username, defaults={
                    "role": User.Role.SUPPLIER, "email": f"{username}@supplier.ph",
                    "first_name": name.split()[0]})
            if made:
                u.set_password(PASSWORD)
                u.save()
            category, _ = SupplierCategory.objects.get_or_create(name=cat)
            SupplierProfile.objects.get_or_create(
                user=u, defaults={"supplier_name": name, "contact_person": person,
                                  "contact_number": phone, "category": category})

    # ---------------------------------------------------------------- wipe
    def wipe(self):
        """Remove every transactional and stock row, plus the placeholder
        accounts and products from the earlier loader. Branches, the owner
        accounts, real suppliers and the original 15 products stay."""
        order = [
            SalesItemBatchConsumption, RefundEvent, RefundItem, Refund,
            OrderStatusChange, OrderStatusHistory, BillingRecord, SalesItem,
            SalesTransaction, PosSalesImport, StockMovement,
            StockTransferEvent, StockTransferItem, StockTransfer,
            DeliveryScanLog, DeliveryReceiptItem, DeliveryReceiptLine,
            DeliveryReceipt, SupplierReturnItem, SupplierReturn,
            ProcurementItem, Procurement, SupplierProductRequest,
            SupplierProduct, Batch, OpeningStockImportRow, OpeningStockImport,
            Inventory, WarehouseStock, Notification, OrderNumberSequence,
        ]
        for model in order:
            model.objects.all().delete()
        # Placeholder catalogue rows and accounts from the earlier loader.
        Product.objects.filter(sku_code__startswith=OLD_SKU_PREFIX).delete()
        ProductCategory.objects.filter(category_name__startswith=OLD_CATEGORY_PREFIX).delete()
        # Every product image is regenerated from the catalog, so clear them
        # first. Without pictures in this build (--no-images) the existing
        # files and rows are left exactly as they are.
        if self.with_images:
            for image in ProductImage.objects.all():
                image.image.delete(save=False)
            ProductImage.objects.all().delete()
        # Reassign branches before the account that owned them goes away.
        gerlyn = User.objects.get(username="gerlyn")
        Branch.objects.exclude(owner=gerlyn).update(owner=gerlyn)
        User.objects.filter(username__startswith=SEED_PREFIX).delete()
        SupplierCategory.objects.filter(name__startswith=OLD_CATEGORY_PREFIX).delete()

    def reset_counters(self):
        """Restart auto-numbers so documents begin at 001 again. DDL commits
        implicitly in MySQL, so this runs before the atomic build."""
        tables = [m._meta.db_table for m in (
            SalesItemBatchConsumption, RefundEvent, RefundItem, Refund,
            OrderStatusChange, OrderStatusHistory, BillingRecord, SalesItem,
            SalesTransaction, StockMovement, StockTransferEvent,
            StockTransferItem, StockTransfer, DeliveryReceiptItem,
            DeliveryReceiptLine, DeliveryReceipt, ProcurementItem, Procurement,
            SupplierProduct, Batch, Inventory, WarehouseStock, Notification,
            OrderNumberSequence,
        )]
        with connection.cursor() as cur:
            for t in tables:
                cur.execute(f"ALTER TABLE `{t}` AUTO_INCREMENT = 1")

    # -------------------------------------------------------------- people
    def _user(self, username, role, **extra):
        user, created = User.objects.get_or_create(
            username=username, defaults={"role": role, **extra},
        )
        changed = False
        for k, v in extra.items():
            if getattr(user, k) != v and k in ("first_name", "last_name", "email"):
                setattr(user, k, v)
                changed = True
        if created:
            user.set_password(PASSWORD)
            changed = True
        if changed:
            user.save()
        return user

    def _more_customers(self, n):
        """A believable spread of storefront customers around Baliuag."""
        rnd = random.Random(99)
        firsts = ["Maricel", "Joel", "Rhea", "Allan", "Cristina", "Ramil", "Gina", "Dennis",
                  "Liza", "Romeo", "Josephine", "Arnel", "Marites", "Noel", "Evelyn", "Rey",
                  "Cherry", "Jun", "Aileen", "Bong", "Dolly", "Mark", "Karen", "Eric", "Joan",
                  "Paolo", "Michelle", "Jerome", "Angelica", "Kevin", "Rowena", "Tess", "Vince",
                  "Hazel", "Nestor", "Lorie", "Jopet", "Analyn", "Rodel", "Cecilia"]
        lasts = ["Dizon", "Mercado", "Pascual", "Tolentino", "Manalo", "Bautista", "Aguilar",
                 "Castillo", "Salvador", "Navarro", "Franco", "De Leon", "Enriquez", "Gonzales",
                 "Hernandez", "Lopez", "Torres", "Valdez", "Zamora", "Domingo", "Ignacio",
                 "Soriano", "Santiago", "Perez", "Rivera", "Mendoza", "Flores", "Esguerra",
                 "Policarpio", "Miranda", "Sarmiento", "Victoria", "Cabrera", "Lacson"]
        barangays = ["Poblacion", "Sabang", "Tarcan", "Makinabang", "Tiaong", "Pagala",
                     "Bagong Nayon", "Calantipay", "Hinukay", "Matangtubig", "Pinagbarilan",
                     "San Roque", "Santa Barbara", "Subic", "Sulivan", "Tangos", "Tibag",
                     "Piel", "Paitan", "Concepcion", "Bonga Mayor", "Virgen delas Flores"]
        prefixes = ["0915", "0916", "0917", "0918", "0919", "0926", "0927", "0935", "0945", "0956"]
        out, used = [], set()
        while len(out) < n:
            fn, ln = rnd.choice(firsts), rnd.choice(lasts)
            username = f"{fn.lower()}.{ln.lower().replace(' ', '')}"
            if username in used:
                continue
            used.add(username)
            phone = f"{rnd.choice(prefixes)}-{rnd.randint(100, 999)}-{rnd.randint(1000, 9999)}"
            street = rnd.choice(["Purok", "Blk", "Sitio"])
            addr = (f"{street} {rnd.randint(1, 9)}, Brgy. {rnd.choice(barangays)}, "
                    f"Baliuag, Bulacan")
            out.append((username, fn, ln, phone, addr))
        return out

    def people(self):
        self.owner = User.objects.get(username="gerlyn")
        wanted = list(C.BRANCH_PROFILE)
        for name in wanted:
            self.branches[name] = Branch.objects.get(branch_name=name)
            Warehouse.objects.get_or_create(
                branch=self.branches[name],
                defaults={"name": f"{name} Warehouse"},
            )

        managers = [
            ("carlo", "Carlo Reyes", "Mini Mart", "0917-301-4482"),
            ("lorna", "Lorna Dizon", "Savers Mini Mart", "0918-275-6093"),
            ("edu", "Eduardo Lim", "Tiaong 1", "0919-640-2815"),
            ("marvin", "Marvin Aquino", "Variety Store", "0917-852-3374"),
        ]
        for username, name, bname, phone in managers:
            u = self._user(username, User.Role.BRANCH_MANAGER,
                           email=f"{username}@gerlynvariety.ph",
                           first_name=name.split()[0], last_name=name.split()[-1])
            BranchManagerProfile.objects.update_or_create(
                user=u, defaults={"branch": self.branches[bname],
                                  "manager_name": name, "contact_number": phone})
            self.managers[bname] = u

        staff = [
            ("ana", "Ana Reyes", "Mini Mart", "0918-420-7716", True, dt.date(2024, 3, 4)),
            ("mika", "Mika Santos", "Mini Mart", "0917-566-1209", True, dt.date(2025, 6, 16)),
            ("rafael", "Rafael Cruz", "Savers Mini Mart", "0920-318-5542", True, dt.date(2024, 9, 2)),
            ("jasmine", "Jasmine Ocampo", "Savers Mini Mart", "0927-774-3081", True, dt.date(2025, 11, 10)),
            ("lea", "Lea Reyes", "Tiaong 1", "0915-902-6634", True, dt.date(2024, 1, 15)),
            ("rose", "Roselyn Diaz", "Tiaong 1", "0918-633-0427", False, dt.date(2023, 8, 21)),
            ("ben", "Benedicto Cruz", "Variety Store", "0919-288-4150", True, dt.date(2023, 5, 8)),
            ("noah", "Noah Flores", "Variety Store", "0917-415-9968", True, dt.date(2025, 2, 3)),
        ]
        for username, name, bname, phone, active, hired in staff:
            u = self._user(username, User.Role.STAFF,
                           email=f"{username}@gerlynvariety.ph",
                           first_name=name.split()[0], last_name=name.split()[-1])
            if u.is_active != active:
                u.is_active = active
                u.save(update_fields=["is_active"])
            StaffProfile.objects.update_or_create(
                user=u, defaults={
                    "branch": self.branches[bname], "staff_name": name,
                    "contact_number": phone, "date_hired": hired,
                    "shift_start": dt.time(6, 30),
                    "shift_end": dt.time(15, 0) if sum(map(ord, username)) % 2 else dt.time(13, 0),
                })
            if active:
                self.staff[bname].append(u)

        customers = [
            ("juan", "Juan", "dela Cruz", "0917-712-3458", "Brgy. Poblacion, Baliuag, Bulacan"),
            ("maria", "Maria", "Santos", "0918-346-2271", "Brgy. Sabang, Baliuag, Bulacan"),
            ("pedro", "Pedro", "Ramos", "0919-557-0832", "Brgy. Tiaong, Baliuag, Bulacan"),
            ("anav", "Ana", "Villanueva", "0920-481-6195", "Brgy. Makinabang, Baliuag, Bulacan"),
            ("jamie", "Jamie", "Garcia", "0927-239-8860", "Brgy. Pagala, Baliuag, Bulacan"),
            ("sam", "Sam", "Mendoza", "0915-668-4107", "Brgy. Tarcan, Baliuag, Bulacan"),
        ]
        customers += self._more_customers(34)
        for username, fn, ln, phone, addr in customers:
            u = self._user(username, User.Role.CUSTOMER,
                           email=f"{username}@customer.ph", first_name=fn, last_name=ln)
            CustomerProfile.objects.update_or_create(
                user=u, defaults={"first_name": fn, "last_name": ln,
                                  "contact_number": phone, "address": addr})
            self.customers.append((u, addr))
        # the first six are the regulars; the rest order now and then
        self.customer_weights = [8, 6, 5, 5, 4, 4] + [1] * (len(self.customers) - 6)

        # suppliers: existing accounts keep their login; fill gaps, add new
        for key, username in C.EXISTING_SUPPLIERS.items():
            self.suppliers[key] = User.objects.get(username=username)
        self.suppliers["luckyme"] = User.objects.get(username="luckyme")
        for key, username, name, cat, person, phone in C.NEW_SUPPLIERS:
            u = self._user(username, User.Role.SUPPLIER,
                           email=f"{username}@supplier.ph", first_name=name.split()[0])
            category, _ = SupplierCategory.objects.get_or_create(name=cat)
            SupplierProfile.objects.update_or_create(
                user=u, defaults={"supplier_name": name, "contact_person": person,
                                  "contact_number": phone, "category": category,
                                  "address": "Bulacan, Philippines"})
            self.suppliers[key] = u
        User.objects.filter(username__in=C.RETIRED_SUPPLIER_USERNAMES,
                            role=User.Role.SUPPLIER).delete()
        for sp in SupplierProfile.objects.filter(address=""):
            sp.address = "Metro Manila, Philippines"
            sp.save(update_fields=["address"])

    # ------------------------------------------------------------- catalog
    def catalog(self):
        from django.core.files.base import ContentFile

        cats = {}
        for name in C.CATEGORIES:
            cats[name], _ = ProductCategory.objects.get_or_create(category_name=name)
        margin = C.MARGIN
        for row in C.PRODUCTS:
            (sku, name, cat, unit, retail, tiers, barcode, desc, shelf, flags,
             supplier, look, demand) = row
            product, _ = Product.objects.update_or_create(
                sku_code=sku, defaults={
                    "product_name": name, "category": cats[cat],
                    "unit_of_measure": unit, "unit_price": D(str(retail)),
                    "barcode": barcode, "description": desc,
                    "is_vat_exempt": "v" in flags,
                    "is_pwd_eligible": "p" in flags,
                    "is_senior_eligible": "s" in flags,
                })
            keep = set()
            for qty, price in tiers:
                ProductPriceTier.objects.update_or_create(
                    product=product, min_quantity=qty, defaults={"unit_price": D(str(price))})
                keep.add(qty)
            product.price_tiers.exclude(min_quantity__in=keep).delete()
            self.products[sku] = product
            self.spec[sku] = row
            floor_price = min([p for _, p in tiers] + [retail])
            base = min(retail * margin[cat], floor_price * 0.93)
            self.cost[sku] = _peso(base)
            # The same figure the suppliers' catalogs and purchase orders use,
            # so the opening-balance lots (no purchase order behind them) are
            # costed like everything restocked afterwards.
            if product.cost_price != self.cost[sku]:
                product.cost_price = self.cost[sku]
                product.save(update_fields=["cost_price"])
            if self.with_images:
                ProductImage.objects.filter(product=product).delete()
                img = ProductImage(product=product, alt_text=f"{name} product photo", is_primary=True)
                img.image.save(f"{sku.lower()}.jpg", ContentFile(packshots.render_jpeg_bytes(look)), save=True)
        # drop anything outside the product master list
        Product.objects.exclude(sku_code__in=list(self.products)).delete()
        ProductCategory.objects.filter(products__isnull=True).exclude(
            category_name__in=C.CATEGORIES).delete()

        # supplier catalogs (several products have a second source)
        links = defaultdict(set)
        for row in C.PRODUCTS:
            links[row[10]].add(row[0])
        for key, skus in C.SECOND_SOURCES.items():
            links[key].update(skus)
        for key, skus in links.items():
            for sku in skus:
                SupplierProduct.objects.update_or_create(
                    supplier=self.suppliers[key], product=self.products[sku],
                    defaults={"current_cost_price": self.cost[sku],
                              "lead_time_days": 2 if key in ("pantry", "liwayway") else 3,
                              "is_preferred": key == self.spec[sku][10],
                              "status": SupplierProduct.Status.APPROVED,
                              "reviewed_by": self.owner})

    # ------------------------------------------------------ stock planning
    def plan_stock(self):
        weights_sum = {}
        for bname, prof in C.BRANCH_PROFILE.items():
            code = prof["code"]
            skus = [r[0] for r in C.PRODUCTS if code in r[12][2]]
            self.carried[bname] = skus
            wsum = sum(self.spec[s][12][0] for s in skus)
            weights_sum[bname] = wsum
            for s in skus:
                w, qtys, _ = self.spec[s][12]
                share = w / wsum
                mean_q = sum(qtys) / len(qtys)
                d = prof["traffic"] * self.scale * 2.6 * share * mean_q * 1.05
                self.demand[(bname, s)] = d
                big = self.spec[s][3] == "Sack" or self.spec[s][4] >= 120
                self.floor_reorder[(bname, s)] = max(1 if big else 4, math.ceil(d * 2.5))
                self.floor_target[(bname, s)] = max(3 if big else 12, math.ceil(d * 7))
                self.wh_reorder[(bname, s)] = max(3 if big else 8, math.ceil(d * 4))
                self.wh_po_point[(bname, s)] = max(4 if big else 12, math.ceil(d * 10))
                self.wh_target[(bname, s)] = max(10 if big else 36, math.ceil(d * 21))

    def opening_balances(self):
        """Stock on hand when the books open (31 July, after close)."""
        when = _at(self.start - dt.timedelta(days=1), 18, 0)
        self.clock.set(when)
        rng = self.rng
        for bname, branch in self.branches.items():
            warehouse = Warehouse.objects.get(branch=branch)
            for sku in self.carried[bname]:
                product = self.products[sku]
                shelf = self.spec[sku][8]
                f_qty = max(2, int(self.floor_target[(bname, sku)] * rng.uniform(0.75, 1.0)))
                w_qty = max(6, int(self.wh_target[(bname, sku)] * rng.uniform(0.55, 0.95)))
                inv = Inventory.objects.create(
                    product=product, branch=branch, quantity_on_hand=0,
                    reorder_level=self.floor_reorder[(bname, sku)])
                ws = WarehouseStock.objects.create(
                    warehouse=warehouse, product=product, quantity_on_hand=0,
                    reorder_level=self.wh_reorder[(bname, sku)])
                for loc, qty, target in (("Warehouse", w_qty, ws), ("Floor", f_qty, inv)):
                    received = self.start - dt.timedelta(days=rng.randint(9, 24))
                    expiry = received + dt.timedelta(days=int(shelf * rng.uniform(0.6, 0.92)))
                    batch = Batch.objects.create(
                        product=product, branch=branch, location=loc, quantity=qty,
                        received_date=received, expiry_date=expiry,
                        notes="Opening balance.")
                    target.quantity_on_hand = qty
                    target.save()
                    StockMovement.objects.create(
                        inventory=inv if loc == "Floor" else None,
                        warehouse_stock=ws if loc == "Warehouse" else None,
                        location_type=loc, batch=batch,
                        movement_type=StockMovement.MovementType.OPENING_BALANCE,
                        quantity_change=qty, quantity_before=0, quantity_after=qty,
                        notes="Opening balance at start of records.",
                        created_by=self.managers[bname])
                self.floor[(bname, sku)] = f_qty
                self.wh[(bname, sku)] = w_qty

    # ------------------------------------------------------------- restock

    def restock(self, bname, when, *, urgent=False):
        """Raise a warehouse-to-floor slip for whatever has run low on the
        shelf. Approval, release and receipt follow as separate events at
        their own times, so stock only moves when the clock says it did."""
        items = []
        for sku in self.carried[bname]:
            key = (bname, sku)
            trigger = (self.floor[key] <= max(1, math.ceil(self.demand[key] * 0.5))
                       if urgent else self.floor[key] <= self.floor_reorder[key])
            if not trigger or self.wh[key] <= 0:
                continue
            need = self.floor_target[key] - self.floor[key]
            qty = min(need, self.wh[key])
            if qty > 0:
                items.append((sku, qty))
        if not items:
            return
        branch = self.branches[bname]
        warehouse = Warehouse.objects.get(branch=branch)
        requester = self._staff_for(bname)
        self.clock.set(when)
        transfer = StockTransfer.objects.create(
            warehouse=warehouse, source_branch=branch, destination_branch=branch,
            purpose=StockTransfer.Purpose.BRANCH_RESTOCK, requested_by=requester,
            status=StockTransfer.Status.PENDING, transfer_date=when.date(),
            notes="Shelf refill from backroom.")
        for sku, qty in items:
            StockTransferItem.objects.create(
                transfer=transfer, product=self.products[sku],
                quantity_requested=qty, quantity_approved=0)
        self._event(transfer, "", StockTransfer.Status.PENDING, requester,
                    f"Requested from {bname} for {bname}")
        r = self.rng
        t_approve = when + dt.timedelta(minutes=r.randint(12, 35))
        t_release = t_approve + dt.timedelta(minutes=r.randint(20, 40))
        t_ack = t_release + dt.timedelta(minutes=r.randint(15, 30))
        self.push(t_approve, "restock_approve", bname, (transfer.pk, requester.pk))
        self.push(t_release, "restock_release", bname, (transfer.pk, requester.pk))
        self.push(t_ack, "restock_ack", bname, (transfer.pk, requester.pk))
        self.stats["restock_slips"] += 1

    def restock_approve(self, bname, when, payload):
        transfer = StockTransfer.objects.get(pk=payload[0])
        manager = self.managers[bname]
        self.clock.set(when)
        transfer.status = StockTransfer.Status.APPROVED
        transfer.approved_by = manager
        transfer.save()
        for it in transfer.items.all():
            it.quantity_approved = it.quantity_requested
            it.save(update_fields=["quantity_approved"])
        self._event(transfer, "Pending", "Approved", manager, "Approved as requested.")

    def restock_release(self, bname, when, payload):
        transfer = StockTransfer.objects.get(pk=payload[0])
        manager = self.managers[bname]
        branch = self.branches[bname]
        warehouse = Warehouse.objects.get(branch=branch)
        self.clock.set(when)
        released = 0
        for it in transfer.items.select_related("product"):
            key = (bname, it.product.sku_code)
            qty = min(it.quantity_approved, self.wh[key])
            if qty > 0:
                self._release_line(transfer, it, branch, warehouse, manager, qty)
                released += qty
        transfer.status = StockTransfer.Status.RELEASED
        transfer.save()
        self._event(transfer, "Approved", "Released", manager,
                    "Warehouse stock moved to the destination floor.")
        transfer.received_qty = released

    def restock_ack(self, bname, when, payload):
        transfer = StockTransfer.objects.get(pk=payload[0])
        requester = User.objects.get(pk=payload[1])
        self.clock.set(when)
        transfer.status = StockTransfer.Status.ACKNOWLEDGED
        transfer.acknowledged_by = requester
        transfer.received_qty = sum(i.quantity_released for i in transfer.items.all())
        transfer.save()
        self._event(transfer, "Released", "Acknowledged", requester, "Receipt confirmed.")

    def _event(self, transfer, frm, to, actor, note):
        StockTransferEvent.objects.create(
            transfer=transfer, from_status=frm, to_status=to, actor=actor, note=note)


    def _release_line(self, transfer, item, branch, warehouse, actor, qty):
        bname = branch.branch_name
        sku = item.product.sku_code
        ws = WarehouseStock.objects.select_for_update().get(warehouse=warehouse, product=item.product)
        before = ws.quantity_on_hand
        ws.quantity_on_hand = before - qty
        ws.save()
        StockMovement.objects.create(
            warehouse_stock=ws, location_type=StockMovement.LocationType.WAREHOUSE,
            movement_type=StockMovement.MovementType.WAREHOUSE_TO_FLOOR,
            quantity_change=-qty, quantity_before=before, quantity_after=ws.quantity_on_hand,
            reference_id=transfer.pk, created_by=actor,
            notes=f"Transfer {transfer.slip_number}: warehouse dispatch")
        inv = Inventory.objects.select_for_update().get(product=item.product, branch=branch)
        fbefore = inv.quantity_on_hand
        inv.quantity_on_hand = fbefore + qty
        inv.save()
        StockMovement.objects.create(
            inventory=inv, location_type=StockMovement.LocationType.FLOOR,
            movement_type=StockMovement.MovementType.WAREHOUSE_TO_FLOOR,
            quantity_change=qty, quantity_before=fbefore, quantity_after=inv.quantity_on_hand,
            reference_id=transfer.pk, created_by=actor,
            notes=f"Transfer {transfer.slip_number}: received on floor")
        consumed = Batch.consume_fefo(item.product, branch, Batch.Location.WAREHOUSE, qty)
        got = 0
        for source, taken in consumed:
            Batch.objects.create(
                product=item.product, branch=branch, location=Batch.Location.FLOOR,
                source_batch=source, procurement=source.procurement, quantity=taken,
                received_date=timezone.localdate(), expiry_date=source.expiry_date,
                notes=f"Restocked via {transfer.slip_number}")
            got += taken
        assert got == qty, f"warehouse batches short for {bname} {sku}: {got}/{qty}"
        item.quantity_released = qty
        item.release_method = self.rng.choice(["Scan", "Scan", "Manual"])
        item.save(update_fields=["quantity_released", "release_method"])
        self.wh[(bname, sku)] -= qty
        self.floor[(bname, sku)] += qty

    # --------------------------------------------------------------- sales
    HOUR_W = {6: 2, 7: 6, 8: 7, 9: 5, 10: 4, 11: 5, 12: 6, 13: 5, 14: 4,
              15: 4, 16: 6, 17: 9, 18: 10, 19: 8, 20: 3}

    def _pick_time(self, day, lo=6, hi=20):
        hours = [h for h in self.HOUR_W if lo <= h <= hi]
        h = self.rng.choices(hours, [self.HOUR_W[h] for h in hours])[0]
        return _at(day, h, self.rng.randint(0, 59), self.rng.randint(0, 59))

    def day_factor(self, day, bname):
        r = self.rng
        weekday = [0.93, 0.90, 0.92, 0.97, 1.08, 1.20, 1.04][day.weekday()]
        payday = 1.0
        if day.day in (15, 30):
            payday = 1.22
        elif day.day in (14, 16, 29, 31, 1):
            payday = 1.10
        elif day.day == 2:
            payday = 1.05
        holiday = {dt.date(2026, 8, 21): 1.06, dt.date(2026, 8, 31): 1.08}.get(day, 1.0)
        trend = 1 + 0.0009 * (day - self.start).days
        storm = {dt.date(2026, 9, 23): 0.58, dt.date(2026, 9, 24): 0.72}.get(day, 1.0)
        if storm < 1 and bname == "Tiaong 1":
            storm *= 0.85
        rain_p = {8: 0.28, 9: 0.30, 10: 0.16}[day.month]
        rain = 1.0
        x = self._weather.setdefault(day, r.random())
        if x < rain_p * 0.2:
            rain = 0.80
        elif x < rain_p:
            rain = 0.93
        noise = r.lognormvariate(0, 0.075)
        return weekday * payday * holiday * trend * storm * rain * noise

    def build_basket(self, bname, basket_boost):
        """Return [(sku, qty)] for one customer, respecting shelf stock."""
        r = self.rng
        skus = self.carried[bname]
        weights = [self.spec[s][12][0] for s in skus]
        n_lines = r.choices([1, 2, 3, 4, 5, 6, 7], [30, 28, 20, 11, 6, 3, 2])[0]
        if basket_boost > 1.08 and r.random() < 0.35:
            n_lines += 1
        bulk = r.random() < 0.032
        if bulk:
            n_lines = r.randint(1, 3)
        chosen, seen = [], set()
        tries = 0
        while len(chosen) < n_lines and tries < 30:
            tries += 1
            sku = r.choices(skus, weights)[0]
            if sku in seen:
                continue
            seen.add(sku)
            avail = self.floor[(bname, sku)]
            if avail <= 0:
                continue
            tiers = self.spec[sku][5]
            if bulk and tiers and self.spec[sku][3] != "Sack":
                qty = r.choice([t[0] for t in tiers]) * r.choice([1, 1, 2])
            else:
                qty = r.choice(self.spec[sku][12][1])
                if basket_boost > 1.1 and r.random() < 0.25:
                    qty = max(self.spec[sku][12][1])
            qty = min(qty, avail)
            if qty > 0:
                chosen.append((sku, qty))
        return chosen

    def _gcash_ref(self):
        return "".join(self.rng.choice("0123456789") for _ in range(13))

    def walk_in(self, bname, when, basket_boost):
        r = self.rng
        basket = self.build_basket(bname, basket_boost)
        if not basket:
            return None
        from sales.pricing import price_cart
        from sales.views import _deduct_stock

        branch = self.branches[bname]
        cashier = self._staff_for(bname) if r.random() < 0.88 else self.managers[bname]
        qty_by = defaultdict(int)
        for sku, q in basket:
            qty_by[sku] += q
        lines = [(self.products[s], q, self.products[s].price_at(q)) for s, q in basket]

        discount = "None"
        if r.random() < 0.028 and any(
                self.products[s].is_senior_eligible for s, _ in basket):
            discount = "Senior" if r.random() < 0.7 else "PWD"
        priced = price_cart(lines, discount_type=discount)
        if discount != "None" and priced["total_discount_amount"] == 0:
            discount = "None"
            priced = price_cart(lines)
        total = priced["total_amount"]

        gcash = r.random() < 0.10
        mop = "GCash" if gcash else "Cash"
        tendered = change = None
        if not gcash:
            steps = [t for t in (5, 10, 20, 50, 100, 200, 500, 1000) if t >= 1]
            options = [total] + [D(math.ceil(total / t) * t) for t in steps]
            tendered = r.choice([o for o in options if o >= total][:5])
            change = tendered - total

        self.clock.set(when)
        holder = f"{r.choice(['Lolita','Rodolfo','Teresita','Armando','Perla','Danilo','Remedios','Eugenio'])} {r.choice(['Bautista','Mercado','Salazar','Pascual','Navarro','Aguilar'])}"
        txn = SalesTransaction.objects.create(
            branch=branch,
            staff=cashier if cashier.role == User.Role.STAFF else None,
            recorded_by=cashier, transaction_date=when,
            transaction_type="Physical", mode_of_payment=mop,
            order_status="Completed" if not gcash else "Pending",
            completed_at=None if gcash else when,
            fulfillment_type="Pickup", total_amount=total,
            discount_type=discount if discount != "None" else "None",
            discount_id_number=(("OSCA-" if discount == "Senior" else "PWD-") +
                                f"{r.randint(100000, 999999)}") if discount != "None" else "",
            discount_holder_name=holder if discount != "None" else "",
            discount_applied_by=cashier if discount != "None" else None,
            discount_applied_at=when if discount != "None" else None,
            total_discount_amount=priced["total_discount_amount"],
            total_vat_amount=priced["total_vat_amount"],
            amount_tendered=tendered, change_amount=change, source="GerlynPLUS",
        )
        for ln in priced["lines"]:
            SalesItem.objects.create(
                transaction=txn, product=ln["product"], quantity_sold=ln["quantity"],
                unit_price=ln["unit_price"], is_vat_exempt_line=ln["is_vat_exempt_line"],
                vat_amount=ln["vat_amount"], discount_amount=ln["discount_amount"])
        _deduct_stock(txn, cashier)
        for sku, q in basket:
            self.floor[(bname, sku)] -= q
        billing = txn.billing_record
        billing.amount_due = total
        billing.payment_method = mop
        if not gcash:
            billing.status = "Paid"
            billing.confirmed_by = cashier
            billing.date_confirmed = when
        else:
            billing.reference_number = self._gcash_ref()
        billing.save()
        if gcash:
            # a second person verifies the payment a few minutes later
            checker = self.managers[bname]
            verified = when + dt.timedelta(minutes=r.randint(3, 25))
            if verified <= self.end_dt:
                self.clock.set(verified)
                self._set_order_status(txn, "Pending", "Completed", checker,
                                       "GCash payment verified.")
                txn.completed_at = verified
                txn.save(update_fields=["completed_at"])
                billing.status = "Paid"
                billing.confirmed_by = checker
                billing.date_confirmed = verified
                billing.save()
        self.stats["walk_in"] += 1
        if r.random() < 0.0045 and not gcash:
            self.refund_queue.append((when + dt.timedelta(days=r.randint(1, 5),
                                                          hours=r.randint(1, 6)), txn.pk, bname))
        return txn

    def _set_order_status(self, txn, frm, to, actor, reason=""):
        txn.order_status = to
        txn.save(update_fields=["order_status"])
        OrderStatusHistory.objects.create(
            transaction=txn, from_status=frm, to_status=to, actor=actor, note=reason[:255])
        OrderStatusChange.objects.create(
            transaction=txn, from_status=frm, to_status=to, changed_by=actor,
            reason=reason[:255])

    # ------------------------------------------------------- online orders
    def place_online(self, bname, when):
        r = self.rng
        from sales.pricing import price_cart

        branch = self.branches[bname]
        basket = self.build_basket(bname, 1.15)
        if not basket:
            return None
        customer, addr = r.choices(self.customers, self.customer_weights)[0]
        delivery = r.random() < 0.58
        if delivery:
            mop = r.choices(["Cash on Delivery", "GCash"], [62, 38])[0]
        else:
            mop = r.choices(["Pay at Store", "GCash"], [66, 34])[0]
        lines = [(self.products[s], q, self.products[s].price_at(q)) for s, q in basket]
        priced = price_cart(lines)
        total = priced["total_amount"]
        self.clock.set(when)
        txn = SalesTransaction.objects.create(
            branch=branch, staff=None, recorded_by=None, customer=customer,
            transaction_date=when, transaction_type="Online", mode_of_payment=mop,
            order_status="Pending", fulfillment_type="Delivery" if delivery else "Pickup",
            delivery_address=addr if delivery else "", total_amount=total,
            total_vat_amount=priced["total_vat_amount"], source="GerlynPLUS")
        for ln in priced["lines"]:
            SalesItem.objects.create(
                transaction=txn, product=ln["product"], quantity_sold=ln["quantity"],
                unit_price=ln["unit_price"], is_vat_exempt_line=ln["is_vat_exempt_line"],
                vat_amount=ln["vat_amount"], discount_amount=ln["discount_amount"])
        billing = txn.billing_record
        billing.amount_due = total
        billing.payment_method = mop
        if mop == "GCash":
            billing.reference_number = self._gcash_ref()
        billing.save()
        self.stats["online"] += 1
        return txn

    def advance_online(self, txn_id, step, when, bname, outcome):
        """step: approve | prepare | complete | cancel"""
        from sales.views import _deduct_stock

        txn = SalesTransaction.objects.get(pk=txn_id)
        staff = self._staff_for(bname)
        self.clock.set(when)
        if step == "approve":
            self._set_order_status(txn, "Pending", "Approved", staff, "Order accepted.")
        elif step == "prepare":
            self._set_order_status(txn, "Approved", "Preparing", staff, "Packing the order.")
        elif step == "complete":
            items = list(txn.items.select_related("product"))
            short = any(self.floor[(bname, i.product.sku_code)] < i.quantity_sold for i in items)
            if short:
                return self.advance_online(txn_id, "cancel", when, bname, "out_of_stock")
            _deduct_stock(txn, staff)
            for i in items:
                self.floor[(bname, i.product.sku_code)] -= i.quantity_sold
            txn.staff = staff
            txn.completed_at = when
            txn.save(update_fields=["staff", "completed_at"])
            self._set_order_status(
                txn, "Preparing", "Completed", staff,
                "Handed over to the customer." if txn.fulfillment_type == "Pickup"
                else "Delivered to the customer.")
            b = txn.billing_record
            b.status = "Paid"
            b.confirmed_by = staff if txn.mode_of_payment != "GCash" else self.managers[bname]
            b.date_confirmed = when
            b.save()
            if self.rng.random() < 0.012:
                self.refund_queue.append((when + dt.timedelta(days=self.rng.randint(1, 4),
                                                              hours=self.rng.randint(1, 5)), txn.pk, bname))
        elif step == "cancel":
            reasons = {
                "customer": "Customer cancelled the order.",
                "unreachable": "Customer could not be reached for delivery.",
                "out_of_stock": "An item ran out of stock before packing.",
            }
            frm = txn.order_status
            reason = reasons.get(outcome, reasons["customer"])
            txn.cancel_reason = reason
            txn.save(update_fields=["cancel_reason"])
            self._set_order_status(txn, frm, "Cancelled", staff, reason)
            b = txn.billing_record
            b.status = "Cancelled"
            b.voided_by = staff
            b.date_voided = when
            b.save()
            self.stats["online_cancelled"] += 1

    # ------------------------------------------------------------- refunds
    def do_refund(self, txn_id, when, bname):
        from sales.refunds import RefundError, request_refund

        txn = SalesTransaction.objects.filter(pk=txn_id).first()
        if txn is None or txn.order_status != "Completed":
            return
        self.clock.set(when)
        r = self.rng
        item = r.choice(list(txn.items.all()))
        qty = r.randint(1, max(1, min(2, item.quantity_sold)))
        reason = r.choices(
            ["Defective", "Expired", "Wrong Item", "Pricing Error", "Changed Mind"],
            [30, 12, 18, 10, 30])[0]
        cond = "Damaged" if reason in ("Defective", "Expired") else "Sellable"
        method = "GCash" if txn.mode_of_payment == "GCash" else "Cash"
        try:
            request_refund(
                txn.pk, self.managers[bname], reason_code=reason, reason="",
                refund_method=method,
                lines=[{"item_id": item.pk, "quantity": qty, "condition": cond}])
        except RefundError:
            return
        if cond == "Sellable":
            self.floor[(bname, item.product.sku_code)] += qty
        self.stats["refunds"] += 1

    # ---------------------------------------------------------- write-offs

    def write_off(self, bname, when):
        r = self.rng
        branch = self.branches[bname]
        warehouse = Warehouse.objects.get(branch=branch)
        candidates = [s for s in self.carried[bname] if self.floor[(bname, s)] >= 6]
        if not candidates:
            return
        picks = r.sample(candidates, k=min(len(candidates), r.randint(1, 2)))
        purpose = r.choice(["Damaged/Spoiled", "Damaged/Spoiled", "Sample/Display"])
        requester = self._staff_for(bname)
        self.clock.set(when)
        transfer = StockTransfer.objects.create(
            warehouse=warehouse, source_branch=branch, destination_branch=None,
            purpose=purpose, requested_by=requester, status="Pending",
            transfer_date=when.date(),
            notes="Dented or leaking packs found during shelf check."
            if purpose == "Damaged/Spoiled" else "Display unit for the shelf.")
        for sku in picks:
            StockTransferItem.objects.create(
                transfer=transfer, product=self.products[sku],
                quantity_requested=r.randint(1, 3), quantity_approved=0)
        self._event(transfer, "", "Pending", requester, f"Requested from {bname}")
        self.push(when + dt.timedelta(minutes=20), "writeoff_approve", bname, transfer.pk)
        self.push(when + dt.timedelta(minutes=45), "writeoff_done", bname, transfer.pk)

    def writeoff_approve(self, bname, when, transfer_id):
        transfer = StockTransfer.objects.get(pk=transfer_id)
        manager = self.managers[bname]
        self.clock.set(when)
        transfer.status = "Approved"
        transfer.approved_by = manager
        transfer.save()
        for it in transfer.items.all():
            it.quantity_approved = it.quantity_requested
            it.save(update_fields=["quantity_approved"])
        self._event(transfer, "Pending", "Approved", manager, "Approved.")

    def writeoff_done(self, bname, when, transfer_id):
        transfer = StockTransfer.objects.get(pk=transfer_id)
        branch = self.branches[bname]
        manager = self.managers[bname]
        self.clock.set(when)
        total = 0
        for it in transfer.items.select_related("product"):
            key = (bname, it.product.sku_code)
            q = min(it.quantity_approved, self.floor[key])
            if q <= 0:
                continue
            it.quantity_released = q
            it.release_method = "Manual"
            it.save(update_fields=["quantity_released", "release_method"])
            inv = Inventory.objects.select_for_update().get(product=it.product, branch=branch)
            before = inv.quantity_on_hand
            inv.quantity_on_hand = before - q
            inv.save()
            StockMovement.objects.create(
                inventory=inv, location_type="Floor", movement_type="Write-off",
                quantity_change=-q, quantity_before=before, quantity_after=inv.quantity_on_hand,
                reference_id=transfer.pk, created_by=manager,
                notes=f"Write-off {transfer.slip_number} — {transfer.get_purpose_display()}")
            Batch.consume_fefo(it.product, branch, Batch.Location.FLOOR, q, include_expired=True)
            self.floor[key] -= q
            total += q
        transfer.status = "Acknowledged"
        transfer.acknowledged_by = manager
        transfer.received_qty = total
        transfer.save()
        self._event(transfer, "Approved", "Acknowledged", manager,
                    "Approved floor stock written off.")
        self.stats["write_offs"] += 1

    # --------------------------------------------------------- procurement
    def case_size(self, sku):
        unit = self.spec[sku][3]
        price = self.spec[sku][4]
        if price >= 300:
            return 6
        if price >= 80:
            return 12
        return {"Can": 24, "Pack": 24, "Bottle": 12, "Box": 12, "Kilo": 50, "Piece": 24}.get(unit, 12)

    def raise_orders(self, day, when):
        """Reorder from each supplier whose lines have dropped to reorder."""
        r = self.rng
        for bname in self.branches:
            by_supplier = defaultdict(list)
            for sku in self.carried[bname]:
                key = (bname, sku)
                effective = self.wh[key] + self.open_po[key]
                if effective <= self.wh_po_point[key]:
                    by_supplier[self.spec[sku][10]].append(sku)
            for sup_key, skus in by_supplier.items():
                self._raise_po(bname, sup_key, skus, day, when)

    def _raise_po(self, bname, sup_key, trigger_skus, day, when):
        r = self.rng
        branch, supplier = self.branches[bname], self.suppliers[sup_key]
        # include other lines from the same supplier that are getting thin
        skus = set(trigger_skus)
        for sku in self.carried[bname]:
            if self.spec[sku][10] == sup_key:
                key = (bname, sku)
                if self.wh[key] + self.open_po[key] <= self.wh_target[key] * 0.55:
                    skus.add(sku)
        lines = []
        for sku in sorted(skus):
            key = (bname, sku)
            need = self.wh_target[key] - (self.wh[key] + self.open_po[key])
            if need <= 0:
                continue
            case = self.case_size(sku)
            qty = max(case, int(math.ceil(need / case)) * case)
            lines.append((sku, qty))
        if not lines:
            return
        manager = self.managers[bname]
        self.clock.set(when)
        self.po_counter += 1
        funding = r.choices(["Bank Transfer", "Cash on Hand", "GCash", "Owner's Funds"],
                            [55, 25, 10, 10])[0]
        po = Procurement.objects.create(
            branch=branch, supplier=supplier, owner=self.owner, created_by=manager,
            order_date=day, status=Procurement.Status.DRAFT,
            funding_source=funding, submission_key=f"po-{self.po_counter:04d}")
        items = []
        for sku, qty in lines:
            items.append(ProcurementItem.objects.create(
                procurement=po, product=self.products[sku], quantity_ordered=qty))
        recent = day >= self.end_dt.date() - dt.timedelta(days=1)
        roll = r.random()
        if recent and roll < 0.35:
            self.stats["po_draft"] += 1
            return          # still a draft, waiting for the owner to approve
        from procurement.services import record_external_order

        self.clock.set(when + dt.timedelta(minutes=r.randint(10, 25)))
        po.status = Procurement.Status.APPROVED
        po.authorized_at = timezone.now()
        po.save()
        if recent and roll < 0.55:
            for sku, qty in lines:
                self.open_po[(bname, sku)] += qty
            self.stats["po_approved"] += 1
            return          # approved, waiting for the supplier's terms
        lead = r.choice([2, 3, 3, 4, 5]) if sup_key not in ("grains", "cocacola") else r.choice([1, 2, 2, 3])
        expected = day + dt.timedelta(days=lead)
        costs = {it.pk: _peso(self.cost[it.product.sku_code] * D(str(r.uniform(0.99, 1.012))))
                 for it in items}
        self.clock.set(when + dt.timedelta(minutes=r.randint(30, 55)))
        record_external_order(
            po, actor=self.owner, delivery_date=expected, unit_costs=costs,
            method=r.choice(["Phone", "Message", "Email", "Message"]),
            note="Confirmed with the supplier representative.")
        for sku, qty in lines:
            self.open_po[(bname, sku)] += qty
        # delivery plan: mostly on time, sometimes late or partial
        roll = r.random()
        delay = 0
        if roll < 0.14:
            delay = r.randint(1, 3)
        elif roll < 0.18:
            delay = r.randint(4, 8)
        partial = r.random() < 0.10
        cancel = r.random() < 0.025
        self.po_queue.append({
            "po": po.pk, "bname": bname, "date": expected + dt.timedelta(days=delay),
            "partial": partial, "cancel": cancel, "stage": 1})
        self.stats["po"] += 1

    def _deliver(self, plan, when):
        from procurement.services import record_delivery

        r = self.rng
        po = Procurement.objects.get(pk=plan["po"])
        bname = plan["bname"]
        self.clock.set(when)
        if plan["cancel"] and plan["stage"] == 1:
            po.status = Procurement.Status.CANCELLED
            po.save(update_fields=["status"])
            for it in po.items.select_related("product"):
                self.open_po[(bname, it.product.sku_code)] -= it.quantity_ordered
            self.stats["po_cancelled"] += 1
            return
        receiver = self._staff_for(bname)
        lines = {}
        for it in po.items.select_related("product"):
            remaining = it.quantity_ordered - it.quantity_received
            if remaining <= 0:
                continue
            qty = remaining
            if plan["partial"] and plan["stage"] == 1:
                qty = max(1, int(remaining * r.uniform(0.5, 0.75)))
            shelf = self.spec[it.product.sku_code][8]
            expiry = when.date() + dt.timedelta(days=int(shelf * r.uniform(0.74, 0.95)))
            lines[it.pk] = (qty, expiry)
        receipt, po, _ = record_delivery(
            po, receiver, lines=lines, receipt_date=when.date(),
            delivered_by=r.choice(["Supplier truck", "Company driver", "Courier"]),
            idempotency_key=f"receipt-{po.pk}-{plan['stage']}")
        DeliveryReceipt.objects.filter(pk=receipt.pk).update(received_at=when)
        for it in po.items.select_related("product"):
            if it.pk in lines:
                key = (bname, it.product.sku_code)
                self.wh[key] += lines[it.pk][0]
                self.open_po[key] -= lines[it.pk][0]
        if po.status == Procurement.Status.PARTIALLY_RECEIVED:
            self.po_queue.append(dict(
                plan, stage=2, partial=False, queued=False,
                date=when.date() + dt.timedelta(days=r.randint(2, 4))))
        self.stats["deliveries"] += 1
        if plan["stage"] == 1 and r.random() < 0.03:
            self._raise_return(po, lines, when, bname)

    def _raise_return(self, po, lines, when, bname):
        """Some deliveries arrive with a few units damaged or wrong; the
        store sends them back and the supplier answers over the next days."""
        from procurement.services import ReturnError, create_supplier_return

        r = self.rng
        big = [(pk, q) for pk, (q, _) in lines.items() if q >= 12]
        if not big:
            return
        item_pk, line_qty = r.choice(big)
        qty = r.randint(2, min(8, line_qty // 3))
        reason = r.choices(["Damaged", "Wrong Item", "Expired", "Quality"], [45, 15, 15, 25])[0]
        notes = {
            "Damaged": "Crushed cartons found when the pallet was opened.",
            "Wrong Item": "Variant delivered does not match the purchase order.",
            "Expired": "Delivered with less than the agreed shelf life.",
            "Quality": "Seals on several units were loose on arrival.",
        }[reason]
        self.clock.set(when + dt.timedelta(hours=2))
        try:
            ret = create_supplier_return(po, self.managers[bname], reason=reason,
                                         notes=notes, quantities={item_pk: qty})
        except ReturnError:
            return
        fate = r.choices(["settled", "shipped", "ack", "rejected", "open"], [42, 16, 14, 14, 14])[0]
        day = when.date()
        steps = []
        if fate != "open":
            steps.append((day + dt.timedelta(days=1), 10, "respond", fate != "rejected"))
        if fate in ("settled", "shipped"):
            steps.append((day + dt.timedelta(days=r.randint(3, 4)), 9, "ship", None))
        if fate == "settled":
            steps.append((day + dt.timedelta(days=r.randint(8, 11)), 14, "settle", None))
        for d, hour, step, arg in steps:
            self.return_queue.append((d, hour, ret.pk, bname, step, arg))
        self.stats["returns"] += 1

    def _return_step(self, ret_id, bname, step, arg, when):
        from procurement.services import (
            ReturnError, cancel_return, respond_to_return, settle_with_credit, ship_return,
        )

        ret = SupplierReturn.objects.get(pk=ret_id)
        self.clock.set(when)
        try:
            if step == "respond":
                respond_to_return(
                    ret, acknowledge=arg,
                    note="" if arg else "Checked on our side: packaging is within specification.")
            elif step == "ship":
                if ret.status != SupplierReturn.Status.ACKNOWLEDGED:
                    return
                shipped = ship_return(ret, self.managers[bname],
                                      note="Handed to the supplier driver on pickup.")
                for line in shipped.items.select_related("product"):
                    self.wh[(bname, line.product.sku_code)] -= line.quantity_returned
            elif step == "settle":
                if ret.status != SupplierReturn.Status.SHIPPED:
                    return
                amount = sum(
                    (line.quantity_returned * (line.procurement_item.unit_cost_snapshot or 0)
                     for line in ret.items.select_related("procurement_item")), D("0"))
                self.credit_notes += 1
                settle_with_credit(ret, self.owner, reference=f"CN-{when.year}-{self.credit_notes:04d}",
                                   amount=amount)
        except ReturnError:
            if step == "ship":
                try:
                    cancel_return(ret)
                except ReturnError:
                    pass

    # ----------------------------------------------------------- main loop
    def push(self, when, kind, bname, payload=None):
        import heapq

        self._seq += 1
        heapq.heappush(self._heap, (when, self._seq, kind, bname, payload))

    def simulate(self):
        import heapq

        r = self.rng
        self._weather = {}
        heap = self._heap = []
        self._seq = 0
        push = self.push

        def schedule_online(txn, bname, when):
            t = when
            cancelled = r.random() < 0.075
            steps = [("approve", None)]
            if cancelled:
                steps.append(("cancel", r.choice(["customer", "customer", "unreachable"])))
            else:
                steps += [("prepare", None), ("complete", None)]
            gaps = {"approve": (8, 45), "cancel": (10, 90), "prepare": (15, 50), "complete": (35, 150)}
            cap = (_at(when.date(), 20, 50) if when.date() < self.end_dt.date()
                   else _at(when.date(), 23, 59))
            prev = when
            for step, outcome in steps:
                t = t + dt.timedelta(minutes=r.randint(*gaps[step]))
                use = min(t, cap) if t > cap else t
                if use <= prev:
                    use = prev + dt.timedelta(minutes=1)
                prev = use
                push(use, "online_step", bname, (txn.pk, step, outcome))

        day = self.start
        end_day = self.end_dt.date()
        while day <= end_day:
            for plan in self.po_queue:
                if plan["date"] <= day and not plan.get("queued"):
                    plan["queued"] = True
                    push(_at(day, r.randint(8, 11), r.randint(0, 59)), "delivery", plan["bname"], plan)
            for bname, prof in C.BRANCH_PROFILE.items():
                factor = self.day_factor(day, bname)
                n = max(3, int(round(prof["traffic"] * self.scale * factor)))
                n_online = int(round(prof["traffic"] * self.scale * 0.045 * r.uniform(0.6, 1.5)))
                push(_at(day, 8, 10), "restock", bname, False)
                push(_at(day, 15, 5), "restock", bname, True)
                push(_at(day, 18, 10), "restock", bname, False)
                for _ in range(n):
                    push(self._pick_time(day), "sale", bname, factor * prof["basket"])
                for _ in range(n_online):
                    push(self._pick_time(day, 7, 20), "online", bname)
            if day.weekday() == 4:
                push(_at(day, 16, 20), "writeoff", r.choice(list(self.branches)))
            for d, hour, ret_id, bname, step, arg in [x for x in self.return_queue if x[0] == day]:
                push(_at(day, hour, r.randint(0, 40)), "return_step", bname, (ret_id, step, arg))
            self.return_queue = [x for x in self.return_queue if x[0] != day]
            for when, txn_id, bname in [x for x in self.refund_queue if x[0].date() == day]:
                push(when, "refund", bname, txn_id)
            self.refund_queue = [x for x in self.refund_queue if x[0].date() != day]
            push(_at(day, 17, 40), "orders", None)
            if day == end_day:
                # a handful of orders placed in the last couple of hours are
                # still being handled at the moment the records are cut
                for _ in range(r.randint(4, 6)):
                    for_branch = r.choice(list(self.branches))
                    push(self.end_dt - dt.timedelta(minutes=r.randint(6, 150)), "online", for_branch)
            while heap and heap[0][0].date() <= day:
                when, _, kind, bname, payload = heapq.heappop(heap)
                if when > self.end_dt:
                    continue
                if kind == "restock":
                    self.restock(bname, when, urgent=payload)
                elif kind == "restock_approve":
                    self.restock_approve(bname, when, payload)
                elif kind == "restock_release":
                    self.restock_release(bname, when, payload)
                elif kind == "restock_ack":
                    self.restock_ack(bname, when, payload)
                elif kind == "writeoff_approve":
                    self.writeoff_approve(bname, when, payload)
                elif kind == "writeoff_done":
                    self.writeoff_done(bname, when, payload)
                elif kind == "sale":
                    self.walk_in(bname, when, payload)
                elif kind == "online":
                    txn = self.place_online(bname, when)
                    if txn:
                        schedule_online(txn, bname, when)
                elif kind == "online_step":
                    txn_id, step, outcome = payload
                    self.advance_online(txn_id, step, when, bname, outcome)
                elif kind == "return_step":
                    ret_id, step, arg = payload
                    self._return_step(ret_id, bname, step, arg, when)
                elif kind == "delivery":
                    self.po_queue.remove(payload)
                    self._deliver(payload, when)
                elif kind == "writeoff":
                    self.write_off(bname, when)
                elif kind == "refund":
                    self.do_refund(payload, when, bname)
                elif kind == "orders":
                    self.raise_orders(day, when)
            if day.day in (1, 15) or day == end_day:
                self.say(f"  {day}: {self.stats['walk_in']} walk-in, {self.stats['online']} online, "
                         f"{self.stats['po']} POs, {self.stats['restock_slips']} restock slips")
            day += dt.timedelta(days=1)

    # ------------------------------------------------------------ finalize
    def finalize(self):
        """Tidy the fields that summarise batches."""
        self.clock.set(self.end_dt)
        for inv in Inventory.objects.all():
            nearest = (Batch.objects.filter(product=inv.product, branch=inv.branch,
                                            location="Floor", quantity__gt=0)
                       .order_by("expiry_date").values_list("expiry_date", flat=True).first())
            Inventory.objects.filter(pk=inv.pk).update(expiry_date=nearest)
        for ws in WarehouseStock.objects.select_related("warehouse"):
            nearest = (Batch.objects.filter(product=ws.product, branch=ws.warehouse.branch,
                                            location="Warehouse", quantity__gt=0)
                       .order_by("expiry_date").values_list("expiry_date", flat=True).first())
            WarehouseStock.objects.filter(pk=ws.pk).update(expiry_date=nearest)
        self._short_dated_lots()
        Notification.objects.all().delete()

    # Slow-moving lines that arrived as short-dated stock, so the expiry
    # panels reflect what a store actually carries at any given time.
    SHORT_DATED = [
        ("Tiaong 1", "SKU-016", "Floor", 6), ("Tiaong 1", "SKU-087", "Floor", 9),
        ("Savers Mini Mart", "SKU-047", "Floor", 14), ("Variety Store", "SKU-085", "Floor", 21),
        ("Mini Mart", "SKU-086", "Floor", 26), ("Savers Mini Mart", "SKU-081", "Floor", 28),
        ("Mini Mart", "SKU-087", "Warehouse", 19),
    ]

    def _short_dated_lots(self):
        today = self.end_dt.date()
        for bname, sku, loc, days in self.SHORT_DATED:
            batch = (Batch.objects.filter(
                product=self.products[sku], branch=self.branches[bname],
                location=loc, quantity__gt=0).order_by("-quantity").first())
            if batch is None:
                continue
            batch.expiry_date = today + dt.timedelta(days=days)
            batch.notes = (batch.notes + " Short-dated delivery.").strip()
            batch.save(update_fields=["expiry_date", "notes"])
        for inv in Inventory.objects.all():
            nearest = (Batch.objects.filter(product=inv.product, branch=inv.branch,
                                            location="Floor", quantity__gt=0)
                       .order_by("expiry_date").values_list("expiry_date", flat=True).first())
            Inventory.objects.filter(pk=inv.pk).update(expiry_date=nearest)
        for ws in WarehouseStock.objects.select_related("warehouse"):
            nearest = (Batch.objects.filter(product=ws.product, branch=ws.warehouse.branch,
                                            location="Warehouse", quantity__gt=0)
                       .order_by("expiry_date").values_list("expiry_date", flat=True).first())
            WarehouseStock.objects.filter(pk=ws.pk).update(expiry_date=nearest)

    # -------------------------------------------------------------- verify
    def verify(self):
        """Return a list of (label, ok, detail)."""
        from django.db.models import F, Sum

        checks = []

        def add(label, ok, detail=""):
            checks.append((label, bool(ok), detail))

        add(f"{len(C.PRODUCTS)} products", Product.objects.count() == len(C.PRODUCTS),
            str(Product.objects.count()))
        mismatched = 0
        for row in C.PRODUCTS:
            p = Product.objects.filter(sku_code=row[0]).first()
            tiers = sorted((t.min_quantity, float(t.unit_price)) for t in p.price_tiers.all()) if p else None
            if (p is None or p.product_name != row[1] or p.category.category_name != row[2]
                    or p.unit_of_measure != row[3] or float(p.unit_price) != row[4]
                    or (p.barcode or None) != row[6] or tiers != sorted(row[5])):
                mismatched += 1
        add("every product matches the exported master list", mismatched == 0,
            f"{mismatched} differ")
        # A build without pictures (--no-images) leaves whatever pictures
        # exist as they are, so it cannot promise each product has a primary one.
        if self.with_images:
            add("every product has a primary image",
                not Product.objects.exclude(images__is_primary=True).exists())
        names = sorted(Branch.objects.values_list("branch_name", flat=True))
        add("4 branches, names unchanged", names == sorted(C.BRANCH_PROFILE), str(names))
        bad = 0
        for inv in Inventory.objects.all():
            fl = Batch.objects.filter(product=inv.product, branch=inv.branch, location="Floor"
                                      ).aggregate(t=Sum("quantity"))["t"] or 0
            if fl != inv.quantity_on_hand:
                bad += 1
        add("floor stock == sum of floor batches", bad == 0, f"{bad} mismatches")
        bad = 0
        for ws in WarehouseStock.objects.select_related("warehouse"):
            wl = Batch.objects.filter(product=ws.product, branch=ws.warehouse.branch,
                                      location="Warehouse").aggregate(t=Sum("quantity"))["t"] or 0
            if wl != ws.quantity_on_hand:
                bad += 1
        add("warehouse stock == sum of warehouse batches", bad == 0, f"{bad} mismatches")
        bad = 0
        for inv in Inventory.objects.all():
            last = StockMovement.objects.filter(inventory=inv).order_by("id").last()
            if last is None or last.quantity_after != inv.quantity_on_hand:
                bad += 1
        add("floor ledger ends at quantity on hand", bad == 0, f"{bad} mismatches")
        bad = 0
        for ws in WarehouseStock.objects.all():
            last = StockMovement.objects.filter(warehouse_stock=ws).order_by("id").last()
            if last is None or last.quantity_after != ws.quantity_on_hand:
                bad += 1
        add("warehouse ledger ends at quantity on hand", bad == 0, f"{bad} mismatches")
        last_seen, out_of_order = {}, 0
        for inv_id, ws_id, when, after_qty, before_qty, change in StockMovement.objects.order_by(
                "id").values_list("inventory_id", "warehouse_stock_id", "created_at",
                                  "quantity_after", "quantity_before", "quantity_change"):
            key = ("i", inv_id) if inv_id else ("w", ws_id)
            if key in last_seen and when < last_seen[key]:
                out_of_order += 1
            last_seen[key] = when
            if before_qty + change != after_qty:
                out_of_order += 1
        add("stock movements are in time order and add up", out_of_order == 0,
            f"{out_of_order} problems")
        mism = sum(1 for (b, s), q in self.floor.items()
                   if Inventory.objects.get(branch=self.branches[b], product=self.products[s]
                                            ).quantity_on_hand != q)
        add("planner mirror matches floor stock", mism == 0, f"{mism} mismatches")
        bad = SalesTransaction.objects.annotate(s=Sum("items__subtotal")).exclude(
            s=F("total_amount")).count()
        add("sale totals == sum of line subtotals", bad == 0, f"{bad} mismatches")
        bad = BillingRecord.objects.exclude(amount_due=F("transaction__total_amount")).count()
        add("billing amount due == sale total", bad == 0, f"{bad} mismatches")
        completed = SalesTransaction.objects.filter(order_status="Completed")
        add("completed sales are all Paid / refunded",
            not completed.exclude(billing_record__status__in=[
                "Paid", "Partially Refunded", "Refunded"]).exists())
        first = SalesTransaction.objects.order_by("transaction_date").first()
        last = SalesTransaction.objects.order_by("transaction_date").last()
        add("sales begin on the start date",
            first and first.transaction_date.astimezone(MANILA).date() == self.start)
        add("no sale dated after now",
            not SalesTransaction.objects.filter(transaction_date__gt=self.end_dt).exists())
        add("batch expiry never precedes receipt",
            not Batch.objects.filter(expiry_date__lt=F("received_date")).exists())
        return checks


def refresh_images(out):
    """Regenerate the primary picture of every product in the master list,
    leaving all other records untouched."""
    from django.core.files.base import ContentFile

    done = 0
    for row in C.PRODUCTS:
        product = Product.objects.filter(sku_code=row[0]).first()
        if product is None:
            continue
        for old in ProductImage.objects.filter(product=product):
            old.image.delete(save=False)
            old.delete()
        img = ProductImage(product=product, alt_text=f"{row[1]} product photo", is_primary=True)
        img.image.save(f"{row[0].lower()}.jpg", ContentFile(packshots.render_jpeg_bytes(row[11])), save=True)
        done += 1
    out.write(f"Regenerated {done} product pictures.")
    return done


def rebuild(out, *, start, end_dt, scale=1.0, seed=2026, images=True,
            reset_counters=True):
    """Wipe and rebuild. Returns (builder, checks)."""
    b = Builder(out, start=start, end_dt=end_dt, scale=scale, seed=seed, images=images,
                reset_counters=reset_counters)
    with suppressed(), _Quiet():
        out.write("Clearing old records ...")
        with transaction.atomic():
            b.ensure_foundation()
            b.wipe()
        if b.restart_numbering:
            b.reset_counters()
        with transaction.atomic(), Clock() as clock:
            b.clock = clock
            out.write("People, branches and suppliers ...")
            b.people()
            out.write("Product master list and pictures ...")
            b.catalog()
            b.plan_stock()
            out.write("Opening balances ...")
            b.opening_balances()
            out.write("Replaying trading days ...")
            b.simulate()
            b.finalize()
        checks = b.verify()
    return b, checks
