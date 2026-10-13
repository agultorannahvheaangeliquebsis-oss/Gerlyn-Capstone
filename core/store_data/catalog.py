"""The product master list, its suppliers and per-product demand profile.

The 104 products come from ``master_list.py`` (the live site's own export:
SKU, name, barcode, category, unit, prices, bulk tiers, tax flags). Everything
an export does not carry is worked out here, the same way every time, from the
product itself: who supplies it, what it costs, how long it keeps, how fast it
sells, which branches stock it and what its picture looks like. ``look`` drives
the packshot renderer and ``demand`` drives the simulated trading history (see
history.py), so a product's picture, price and sales pattern share one source.
"""
import re
import hashlib
from decimal import Decimal

from .master_list import MASTER

D = Decimal

CATEGORIES = sorted({row[3] for row in MASTER})

# Field order of each PRODUCTS row:
#   sku, name, category, unit, retail, [(min_qty, price)], barcode,
#   description, shelf_life_days, flags, supplier key, look, demand
# flags:  v = VAT-exempt, p = PWD discount, s = Senior discount
# look:   (shape, brand, line 1, line 2, size, primary, secondary, text colour)
# demand: (popularity weight, usual quantities per sale, branches that carry it)
#         branch letters: V Variety Store, M Mini Mart, S Savers, T Tiaong 1

# lower-case name prefix -> (brand shown on the pack, supplier key, primary, secondary, text)
BRANDS = [
    ("bear brand", "Bear Brand", "nestle", "#1b4f9c", "#ffffff", "#ffffff"),
    ("chuckie", "Chuckie", "nestle", "#4a2a1a", "#f6c21b", "#ffffff"),
    ("coffee mate", "Coffee Mate", "nestle", "#c8102e", "#ffffff", "#ffffff"),
    ("nescafe", "Nescafe", "nestle", "#b3141f", "#3a2418", "#ffffff"),
    ("nestea", "Nestea", "nestle", "#e8a21c", "#c8102e", "#ffffff"),
    ("nestogen", "Nestogen", "nestle", "#2a7fc1", "#f6c21b", "#ffffff"),
    ("nestle", "Nestle", "nestle", "#1d6fc4", "#ffffff", "#ffffff"),
    ("nido", "Nido", "nestle", "#f2b01e", "#1d4fa3", "#1d4fa3"),
    ("magnolia", "Magnolia", "sanmiguel", "#1d4fa3", "#ffffff", "#ffffff"),
    ("century", "Century", "century", "#1f4e9c", "#e8322c", "#ffffff"),
    ("bingo", "Bingo", "century", "#c8102e", "#f6c21b", "#ffffff"),
    ("san marino", "San Marino", "century", "#c8102e", "#1f4e9c", "#ffffff"),
    ("hokkaido", "Hokkaido", "century", "#1b6fb8", "#f6c21b", "#ffffff"),
    ("cdo", "CDO", "sanmiguel", "#e0261f", "#fcd116", "#ffffff"),
    ("energen", "Energen", "monde", "#f39a1e", "#1b4f9c", "#ffffff"),
    ("coconut brand", "Coconut Brand", "nutriasia", "#2f7d4a", "#f6c21b", "#ffffff"),
    ("silver swan", "Silver Swan", "nutriasia", "#1a1a1a", "#f6f0e0", "#f6f0e0"),
    ("domex", "Domex", "unilever", "#1d6fc4", "#f6c21b", "#ffffff"),
    ("surf", "Surf", "unilever", "#1b6fb8", "#f2c200", "#ffffff"),
    ("scotch brite", "Scotch-Brite", "unilever", "#f2c200", "#1d8a3c", "#1a1a1a"),
    ("queen", "Queen", "pantry", "#c8102e", "#ffffff", "#ffffff"),
    ("santa monica", "Santa Monica", "pantry", "#e8651c", "#ffffff", "#ffffff"),
    ("goodlife", "Goodlife", "pantry", "#2f8f3a", "#f6c21b", "#ffffff"),
    ("gardenia", "Gardenia", "pantry", "#2f7d4a", "#ffffff", "#ffffff"),
    ("super delights", "Super Delights", "pantry", "#7a3b1c", "#f6c21b", "#ffffff"),
    ("wonder", "Wonder", "pantry", "#e8321c", "#f6c21b", "#ffffff"),
    ("bread pan", "Bread Pan", "liwayway", "#e8651c", "#f6c21b", "#ffffff"),
    ("martys", "Marty's", "liwayway", "#c8102e", "#f6c21b", "#ffffff"),
    ("oishi", "Oishi", "liwayway", "#e8321c", "#1b6fb8", "#ffffff"),
    ("pillows", "Pillows", "liwayway", "#6a3f9c", "#ffffff", "#ffffff"),
    ("oheya", "Oheya", "liwayway", "#f2b01e", "#c8102e", "#7a1410"),
    ("bench", "Bench", "bench", "#1a1a1a", "#e0679a", "#ffffff"),
    ("eight", "Eight", "bench", "#1b6fb8", "#ffffff", "#ffffff"),
    ("charmee", "Charmee", "hygiene", "#e0679a", "#ffffff", "#ffffff"),
    ("happy", "HAPPY", "hygiene", "#f2b01e", "#7cc4ee", "#ffffff"),
    ("500ml green cross", "Green Cross", "hygiene", "#1d8a3c", "#ffffff", "#ffffff"),
    ("chesterfield", "Chesterfield", "tobacco", "#c8102e", "#ffffff", "#ffffff"),
    ("fortune", "Fortune", "tobacco", "#1d8a3c", "#f6c21b", "#ffffff"),
    ("marlboro", "Marlboro", "tobacco", "#c8102e", "#ffffff", "#ffffff"),
    ("mighty", "Mighty", "tobacco", "#1d8a3c", "#ffffff", "#ffffff"),
    ("winnsboro", "Winnsboro", "tobacco", "#b3141f", "#ffffff", "#ffffff"),
    ("winston", "Winston", "tobacco", "#b3141f", "#ffffff", "#ffffff"),
]
# a flavour or variant word in the name recolours the pack
VARIANT_COLOURS = [
    ("blossom", "#e0679a"), ("kalamansi", "#6fae3a"), ("sunfresh", "#f2a900"),
    ("tawas", "#1b6fb8"), ("lights", "#6fa8dc"), ("blue", "#1b6fb8"),
    ("green", "#1d8a3c"), ("red", "#c8102e"), ("white", "#c4ccd6"),
    ("chicken inasal", "#e8651c"), ("cheese", "#f2b01e"), ("vanilla", "#f2d27a"),
    ("ube", "#6a3f9c"), ("chili", "#c8102e"), ("decaf", "#3a2418"),
]
CATEGORY_SUPPLIER = {
    "Beverages & Drinks": "nestle", "Canned & Packaged Goods": "century",
    "Cleaning Supplies": "unilever", "Cooking Essentials & Condiments": "nutriasia",
    "Fresh, Dairy, & Eggs": "nestle", "Personal Care & Hygiene": "hygiene",
    "Rice, Staples & Dry Goods": "pantry", "Snacks & Sweets": "liwayway",
    "Tobacco, Health & Extras": "tobacco",
}
FALLBACK_COLOURS = {
    "Beverages & Drinks": ("#1d6fc4", "#ffffff"), "Canned & Packaged Goods": ("#c8102e", "#f6c21b"),
    "Cleaning Supplies": ("#1b6fb8", "#f2c200"), "Cooking Essentials & Condiments": ("#2f7d4a", "#f6c21b"),
    "Fresh, Dairy, & Eggs": ("#1d4fa3", "#ffffff"), "Personal Care & Hygiene": ("#e0679a", "#ffffff"),
    "Rice, Staples & Dry Goods": ("#d9c9a0", "#b43a2c"), "Snacks & Sweets": ("#e8651c", "#f6c21b"),
    "Tobacco, Health & Extras": ("#b3141f", "#ffffff"),
}
SHELF_LIFE = {
    "Beverages & Drinks": 360, "Canned & Packaged Goods": 900, "Cleaning Supplies": 1000,
    "Cooking Essentials & Condiments": 540, "Fresh, Dairy, & Eggs": 450,
    "Personal Care & Hygiene": 1000, "Rice, Staples & Dry Goods": 360,
    "Snacks & Sweets": 180, "Tobacco, Health & Extras": 540,
}
# share of the retail price the shop pays its supplier
MARGIN = {
    "Beverages & Drinks": .85, "Canned & Packaged Goods": .84, "Cleaning Supplies": .80,
    "Cooking Essentials & Condiments": .80, "Fresh, Dairy, & Eggs": .86,
    "Personal Care & Hygiene": .80, "Rice, Staples & Dry Goods": .88,
    "Snacks & Sweets": .82, "Tobacco, Health & Extras": .93,
}
CATEGORY_POPULARITY = {
    "Beverages & Drinks": 5.0, "Canned & Packaged Goods": 5.0, "Cleaning Supplies": 3.5,
    "Cooking Essentials & Condiments": 4.0, "Fresh, Dairy, & Eggs": 2.5,
    "Personal Care & Hygiene": 3.0, "Rice, Staples & Dry Goods": 2.5,
    "Snacks & Sweets": 6.0, "Tobacco, Health & Extras": 4.0,
}
# chance that a branch stocks a line that is not a top seller
BRANCH_REACH = {"V": 1.0, "M": 0.93, "S": 0.87, "T": 0.72}

_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s?(ml|kg|g|liters?|litres?|l)\b", re.I)
_COUNT = re.compile(r"\bx\s?(\d+)s?\b", re.I)


def _frac(sku, salt):
    """A stable number in [0, 1) for this product, so a rebuild never shuffles."""
    return int(hashlib.md5(f"{sku}|{salt}".encode()).hexdigest()[:8], 16) / 2 ** 32


def _brand(name):
    lname = name.lower()
    for key, display, supplier, c1, c2, tc in BRANDS:
        if lname.startswith(key):
            return display, supplier, c1, c2, tc, key
    return None


def _size_label(name):
    found = _SIZE.findall(name)
    count = _COUNT.findall(name)
    label = ""
    if found:
        num, unit = found[-1]
        label = f"{num} {'L' if unit.lower().startswith('l') else unit.lower()}"
    if count:
        label = f"{label} x {count[-1]}s".strip()
    return label


def _shape(name, category, unit):
    n = name.lower()
    if category.startswith("Tobacco"):
        return "box"
    if "surf bar" in n:
        return "soapbox"
    if "scotch brite" in n:
        return "flatpack"
    if "diaper" in n:
        return "bigpack"
    if "charmee" in n:
        return "flatpack"
    if any(w in n for w in ("alcohol", "domex")) or n.startswith(("bench", "eight")):
        return "bottle"
    if category.startswith("Canned"):
        if unit == "Can" or any(w in n for w in ("tuna", "mackerel", "corned", "luncheon")):
            return "can"
        return "pouch"
    if category.startswith("Fresh"):
        return "can" if ("nestogen" in n or "nido" in n) else "box"
    if category.startswith("Beverages"):
        if unit == "Bottle":
            return "bottle"
        return "box" if re.search(r"x\s?\d+s?\b", n) else "pouch"
    if category.startswith("Snacks"):
        return "flatpack" if any(w in n for w in ("toasties", "brownies", "donuts")) else "chips"
    if unit == "Bottle":
        return "bottle"
    if unit == "Piece":
        return "soapbox"
    return "pouch"


def _look(sku, name, category, unit):
    brand = _brand(name)
    if brand:
        shown, supplier, c1, c2, tc, key = brand
    else:
        shown, supplier, key = name.split()[0], None, name.split()[0].lower()
        (c1, c2), tc = FALLBACK_COLOURS[category], "#ffffff"
    lname = name.lower()
    rest = lname[len(key):] if lname.startswith(key) else lname
    # soaps and cigarettes are told apart by variant, so the variant sets the pack colour
    if supplier in ("unilever", "tobacco"):
        for word, colour in VARIANT_COLOURS:
            if word in rest:
                c1 = colour
                break
    rest = _COUNT.sub("", _SIZE.sub("", rest))
    words = [w for w in rest.split() if w not in ("with", "in", "of", "and", "&")]
    line1 = " ".join(words[:2]).upper()
    line2 = " ".join(words[2:5]).title()
    return (_shape(name, category, unit), shown, line1, line2, _size_label(name), c1, c2, tc)


def _shelf_life(name, category):
    # The replay does not age stock out on the shelf, so no line is given a
    # shorter life than the 180 days the whole catalog has always had as a floor.
    n = name.lower()
    if "fresh milk" in n or "sterilized" in n:
        return 270
    return SHELF_LIFE[category]


def _quantities(price, category):
    if price <= 15:
        return [1, 2, 2, 3, 4, 5, 6, 10]
    if price <= 30:
        return [1, 1, 2, 2, 3, 4, 6]
    if price <= 60:
        return [1, 1, 1, 2, 2, 3]
    if price <= 130:
        return [1, 1, 1, 2, 2]
    if price <= 250:
        return [1, 1, 1, 2]
    return [1]


def _demand(sku, category, price):
    weight = CATEGORY_POPULARITY[category] * min(1.5, max(0.45, (50 / price) ** 0.35))
    weight = round(weight * (0.7 + 0.6 * _frac(sku, "pop")), 2)
    letters = ""
    for letter, reach in BRANCH_REACH.items():
        if weight >= 7.0 or _frac(sku, letter) < reach:
            letters += letter
    return weight, _quantities(price, category), letters or "V"


def _build():
    rows = []
    for sku, name, barcode, category, unit, retail, tiers, flags in MASTER:
        price = float(retail)
        brand = _brand(name)
        supplier = brand[1] if brand else CATEGORY_SUPPLIER[category]
        rows.append((
            sku, name, category, unit, price,
            [(qty, float(p)) for qty, p in tiers], barcode, "",
            _shelf_life(name, category), flags, supplier,
            _look(sku, name, category, unit), _demand(sku, category, price)))
    return rows


PRODUCTS = _build()

# Existing supplier accounts are matched by username; these keys name them.
EXISTING_SUPPLIERS = {
    "century": "century", "monde": "monde", "nestle": "nestle",
    "sanmiguel": "sanmiguel", "delmonte": "delmonte", "unilever": "unilever",
    "pg": "pg",
}
EXISTING_SUPPLIER_DEFS = [
    # username, name, category, contact person, number
    ("century", "Century Pacific Food Inc.", "Canned Goods", "Ricardo Buenaventura", "0917-123-4567"),
    ("monde", "Monde Nissin Corporation", "Noodles & Pasta", "Maria Santos", "0918-234-5678"),
    ("nestle", "Nestlé Philippines Inc.", "Beverages & Dairy", "Jose Dela Cruz", "0919-345-6789"),
    ("sanmiguel", "San Miguel Purefoods Co. Inc.", "Canned Goods", "Lina Reyes", "0920-456-7890"),
    ("delmonte", "Del Monte Philippines Inc.", "Condiments", "Fernando Aguilar", "0921-567-8901"),
    ("unilever", "Unilever Philippines", "Personal Care & Condiments", "Carla Mendez", "0922-678-9012"),
    ("luckyme", "Lucky Me! (Monde Nissin)", "Noodles & Pasta", "Tony Cruz", "0923-789-0123"),
    ("pg", "Procter & Gamble Philippines", "Baby & Personal Care", "Grace Villanueva", "0924-890-1234"),
]
NEW_SUPPLIERS = [
    # key, username, name, category, contact person, number
    ("nutriasia", "nutriasia", "NutriAsia Inc.", "Condiments",
     "Henry Salonga", "0919-407-8836"),
    ("liwayway", "liwayway", "Liwayway Marketing Corporation", "Snacks & Beverages",
     "Dolores Pascual", "0920-163-7752"),
    ("pantry", "pantrybakery", "Pantry & Bakery Trading", "Rice, Staples & Dry Goods",
     "Rodel Manalo", "0917-318-4420"),
    ("hygiene", "luzonhygiene", "Luzon Hygiene Products Trading", "Baby & Personal Care",
     "Cherry Domingo", "0918-552-0173"),
    ("bench", "benchfragrance", "Bench Fragrance Distribution", "Baby & Personal Care",
     "Aldrin Bautista", "0921-884-3061"),
    ("tobacco", "bulacantobacco", "Bulacan Tobacco Distributors", "Tobacco, Health & Extras",
     "Ernesto Valdez", "0922-310-6684"),
]
# accounts created by the previous 30-product build that no product uses now
RETIRED_SUPPLIER_USERNAMES = ["bulacangrains", "alaska", "urc", "cocacola"]
# products that a second supplier can also provide
SECOND_SOURCES = {"luckyme": ["SKU-004"]}

# Branch size and character. Names are looked up as they exist in the system.
BRANCH_PROFILE = {
    "Variety Store":    {"traffic": 30, "basket": 1.00, "code": "V"},
    "Mini Mart":        {"traffic": 23, "basket": 0.95, "code": "M"},
    "Savers Mini Mart": {"traffic": 19, "basket": 1.12, "code": "S"},
    "Tiaong 1":         {"traffic": 12, "basket": 0.90, "code": "T"},
}
