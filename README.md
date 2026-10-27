# GerlynPLUS

**Process-Linked Unified Systems Enterprise** — an integrated information system for
GERLYN, a multi-branch retail enterprise in Baliuag, Bulacan.

Capstone project · BSIS · Bulacan State University · CICT

> 👉 **New to this project, or want to test it?** Start with
> **[PROJECT-LOG.md](PROJECT-LOG.md)** — it explains what was built, every bug that
> was fixed, and has step-by-step testing instructions anyone on the team can follow.

---

## What it does

One centralized system replacing fragmented, per-branch record-keeping. It unifies the
five core business processes identified in the process analysis:

| Process | What the system does |
|---|---|
| **Sales & Orders** | Walk-in POS + an Order Console for online orders (Pending → Approved → Preparing → Completed / Cancelled) |
| **Billing** | A billing record per sale; payment method + status recorded, GCash reference & proof upload |
| **Inventory** | Product master list, per-branch stock, FEFO batches/expiry, withdrawal slips with receipt acknowledgment, damaged-product reports with photo evidence |
| **Procurement** | Suppliers, purchase orders, and deliveries that update stock only after receipt validation |
| **Reporting** | Dashboard + Sales/Inventory/Procurement/Billing/Profitability reports, CSV, PDF & Excel export (each PDF/Excel opens with a Summary), consolidated or per-branch |
| **Expenses & profit** | Operating expenses (rent, utilities, salaries ...) recorded by the Owner / Branch Managers; a Profitability report (sales - refunds - cost of goods - expenses) and a Net Profit card on the Dashboard |

**Roles (RBAC):** Owner · Branch Manager · Staff · Supplier · Customer.
Managers and staff are scoped to their own branch; the Owner sees every branch.

---

## Run it (7 steps)

```bash
# 1. Get the code
git clone <repo-url> && cd Gerlyn

# 2. Create and activate a virtual environment
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create the MySQL database
mysql -u root -p -e "CREATE DATABASE gerlyn_db CHARACTER SET utf8mb4;"

# 5. Configure your environment
cp .env.example .env        # then edit DB_PASSWORD and DJANGO_SECRET_KEY

# 6. Build the schema and load demo data
python manage.py migrate
python manage.py seed_demo --reset

# Optional: replace the demo records with the four-branch trading history
# (30 products with pictures, Aug 2026 to today) — see "Store records" below
python manage.py rebuild_store_records --yes

# 7. Start it
python manage.py runserver
```

Open <http://127.0.0.1:8000/> — the storefront. Log in at `/login/` for the internal system.

**To let other branches connect during the demo,** add the server's LAN IP to
`DJANGO_ALLOWED_HOSTS` in `.env`, then run
`python manage.py runserver 0.0.0.0:8000`.

### Email delivery

Development uses Django's console email backend by default, so verification
messages appear in the terminal without SMTP credentials.

**Railway (Free/Trial/Hobby plans) blocks outbound SMTP**, so Gmail/SMTP
silently fails there. Deploy with Brevo's HTTPS API instead; set these in
Railway Variables (never commit real values) and leave `EMAIL_BACKEND` unset:

```text
BREVO_API_KEY=<Brevo API key>
DEFAULT_FROM_EMAIL=<sender address verified in Brevo>
PUBLIC_BASE_URL=https://gerlynplus.up.railway.app
```

Failed sends are logged to the `accounts.email` logger (Railway deploy logs)
with the status/error type only, never the key or SMTP password; users just see
a generic "couldn't send" message.

For hosts that allow SMTP (Railway Pro, a VPS), set these instead:

```text
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_USE_SSL=False
EMAIL_HOST_USER=the-sender@example.com
EMAIL_HOST_PASSWORD=provider-app-password
DEFAULT_FROM_EMAIL=the-sender@example.com
PUBLIC_BASE_URL=https://your-public-domain.example
```

Use either TLS on port 587 or SSL on port 465, never both. Gmail requires an
App Password when two-step verification is enabled, and the sender must be the
authenticated account or an authorized alias. After setting deployment
variables, verify delivery without creating an account:

```bash
python manage.py sendtestemail your-inbox@example.com
```

The command reports SMTP configuration/authentication failures to the operator;
it does not print the configured password. After it succeeds, register a test
customer and confirm that the received verification link starts with
`PUBLIC_BASE_URL`.

The public **Forgot password?** flow uses this same mail configuration. Reset
links are generated with `PUBLIC_BASE_URL`, expire after 30 minutes, become
invalid immediately after a successful reset, and do not sign the user in.
Keep `PUBLIC_BASE_URL` on the canonical HTTPS application origin in every
production environment. The shared cache table/Redis cache also enforces the
per-email and per-IP reset-request limits; Railway creates the database cache
table during deploy, while a manual installation should run
`python manage.py createcachetable` once.

### Address maps and geocoding

The address picker uses the bundled Philippine PSGC data for its cascading
Region/Province/City/Barangay fields. Automatic pin placement is optional and
is performed server-side through a Nominatim-compatible provider; the browser
never receives the provider URL or key. Configure `GEOCODING_SEARCH_URL`,
`GEOCODING_REVERSE_URL`, and an identifying `GEOCODING_USER_AGENT` in `.env`.
Set `GEOCODING_API_KEY` and `GEOCODING_API_KEY_PARAM` only when the selected
provider requires them.

For map imagery, configure `MAP_TILE_URL_TEMPLATE` with `{z}`, `{x}`, and `{y}`
placeholders (plus `{api_key}` when needed) and keep the key in
`MAP_TILE_API_KEY`. Both geocoder results and tiles are cached. With these
settings blank or while a provider is unavailable, the coordinate grid,
manual coordinate entry, and draggable pin remain available.
Set `MAP_TILE_ATTRIBUTION` to the credit line required by the chosen tile
provider. The default provider-wide geocoding interval is one second and can
be adjusted with `GEOCODING_PROVIDER_MIN_INTERVAL_SECONDS` to match policy.

### Automatic fulfillment branch

The storefront assigns one active branch to the complete cart; it never
splits an order. When the customer explicitly presses **Use current
location**, the browser's Geolocation API supplies coordinates for a
straight-line Haversine comparison. These coordinates stay only in the
server-side session, expire after 30 minutes, and are cleared after checkout.
Saved address coordinates are reused when available. Without coordinates,
the resolver tries matching branch city/barangay service areas, preserves a
valid automatic cart assignment, then uses `DEFAULT_FULFILLMENT_BRANCH_ID`
or the lowest active branch ID as a deterministic fallback.

Set `DEFAULT_FULFILLMENT_BRANCH_ID` in `.env` to an active Branch database ID
when a specific store should be the fallback. Owners maintain branch active
status and coordinates under **Administration → Branches** (or Django admin).
Distance is approximate straight-line distance, not road distance or a
delivery-time quote; no third-party routing service is used.

### Delivery areas (where each branch delivers)

The Owner decides where each branch delivers under **Administration →
Delivery Areas**: add, edit, activate and deactivate provinces, municipalities
and barangays per branch, each with an optional delivery fee, minimum order and
note. Every change is in the Audit Log. Places are chosen from the bundled PSGC
lists and matched **by code**, never by spelling (the official name is
*Baliuag*; *Baliwag* is accepted when typed).

How an address is matched (`branches/coverage.py`, the one place the rule
lives): the most specific entry for a place decides it. An active province
covers every municipality and barangay in it; an active municipality covers its
barangays; an **inactive** barangay or municipality is excluded even inside an
active province; a place with no entry is outside. A specific entry's fee and
minimum order replace the broader one's, and a blank value inherits.

- A branch with **no** delivery areas is not restricted (the Delivery Areas
  page warns about it). Once it has any, only what its active entries cover is
  delivered to.
- Migration `branches/0005` and `python manage.py seed_service_areas` give each
  branch placeholder entries (all of Bulacan, ₱100; Baliuag, ₱50; no minimum
  order). Existing entries are never changed. Edit them before relying on them.
- **Storefront:** a sticky "We deliver to…" banner on the home page, catalog
  and cart/checkout (dismissible for the session, always back at checkout) and a
  public page, *Where we deliver* (`/shop/service-areas/`). Checkout uses
  Province → Municipality → Barangay dropdowns (with "Other"), warns inline when
  the branch does not deliver there and turns off *Place Order*, offering
  *Switch to Pickup* or *Choose another branch*. A pickup order placed after
  such a refusal sends the customer an in-app notification. New customers get a
  one-time notice at registration.
- **Enforced on the server**, not by the page: `checkout()` and `POST
  /api/sales/` apply the same rule and refuse an outside delivery or one under
  the area's minimum order. API clients send `deliveryProvince`,
  `deliveryCity` and `deliveryBarangay` (PSGC codes or words). The delivery fee
  is shown to customers but is **not** added to the order total (the cart already
  says delivery is arranged with the branch).
- **Older saved addresses:** an address saved as text only (no PSGC codes) can't
  be checked, so checkout asks the customer to pick the place from the lists.
  After deploying, run `python manage.py backfill_address_codes --dry-run`, then
  without `--dry-run`. It reads each address's text strictly by position
  (`…, barangay, municipality, province`), fills only blank code columns it can
  prove, never overwrites a code or edits the text, and lists the ids that still
  need the customer. Safe to re-run.
- A coordinate pin names no barangay, so the check uses the province,
  municipality and barangay chosen (the map picker fills them in); a customer
  who declines location sharing is checked on the same fields.
- **Order Console:** Order Detail shows the delivery area. If an area is
  switched off after an order was placed, Approve is held until the Owner or
  that branch's Manager overrides it on the order with a required reason, which
  is kept on the order and in the Audit Log. Staff can see the flag and the
  override but cannot make one. Orders that never recorded an area are not held.

---

## Day-to-day: `./run.sh`

After the one-time setup above, this is all you need. It activates the virtualenv,
frees the port if a server is already running, and prints the addresses + logins.

```bash
./run.sh          # start (this is the usual one)
./run.sh fresh    # wipe + reload demo data, then start
./run.sh stop     # stop the server
./run.sh test     # run the acceptance tests
```

Stop it with **Ctrl+C**, or `./run.sh stop` from another terminal.

There is **no build step** — no npm, no bundler. Edit a template or CSS file and just
refresh the browser; Django reloads Python changes automatically.

---

## Demo accounts

All seeded accounts use the password **`demo1234`**.

| Username | Role | Scope |
|---|---|---|
| `gerlyn` | Owner | All branches (also a Django superuser) |
| `carlo` | Branch Manager | Tarcan |
| `edu` | Branch Manager | Tiaong Baliwag Mini Mart |
| `ana` | Staff | Tarcan |
| `ben` | Staff | Makinabang |
| `rose` | Staff (inactive) | Tiaong — demonstrates deactivation |
| `juan`, `maria`, `pedro`, `anav` | Customer | Storefront |
| `century`, `monde`, `nestle`, … | Supplier | Their own purchase orders |

`python manage.py seed_demo --reset` rebuilds this at any time (3 branches,
15 products, per-branch stock with 6 low/critical alerts, 8 suppliers, 7 POs, 10 orders).

### Store records

`python manage.py rebuild_store_records --yes` replaces the product master list,
stock, sales, billing and procurement records with a day-by-day trading history
for the four live branches (`Mini Mart`, `Savers Mini Mart`, `Tiaong 1`,
`Variety Store` — their names are never touched) from 1 August 2026 up to the
moment it runs (or to `--end "2026-10-14 09:00"` to build up to a chosen moment).
The 104-product master list is the live site's own export
(`docs/gerlyn-products-2026-10-05.xlsx`, held in `core/store_data/master_list.py`);
each product gets a white-background picture. It builds opening
balances, supplier purchase orders and deliveries, warehouse-to-floor restocks,
walk-in and online sales (with senior/PWD discounts, refunds and write-offs),
all replayed through the same code the screens use with the clock set to each
moment, so floor and warehouse quantities always equal the sum of their
batches and every dashboard figure traces back to a record. It prints a
consistency report when it finishes.

It deletes existing transactional records, so it requires `--yes` and refuses a
non-local database unless `--allow-remote` is also given. `--seed` makes a
rebuild reproducible; `--scale 0.3 --start 2026-09-20` gives a quick small run.
By default every product picture is deleted and regenerated; `--no-images`
leaves the existing pictures (files and rows) untouched and generates none (the
consistency report then skips its "every product has a primary image" check), and
`--images-only` regenerates just the pictures without touching any other record.
Note that `seed_demo --reset` (above) is a different, older dataset with three
differently named branches — running it afterwards replaces these records.

---

## Tech stack

- **Backend:** Python · Django 6 · Django REST Framework
- **Database:** MySQL (single centralized database — every branch reads/writes the same data)
- **Frontend:** Django templates + a self-contained design system
  (`static/css/gerlyn.css`, `static/css/storefront.css`). **No CDN, no external
  fonts or scripts** — the system runs fully offline, with no build step.

### Why server-rendered templates, not the React SPA

A React/Vite SPA was built in parallel and both were tested side by side before
choosing. Django templates won on evidence, not preference:

| | React SPA | Django templates |
|---|---|---|
| Withdrawal Slips (a core process) | missing | implemented |
| CSV export | missing | 4 reports, branch-scoped |
| Order filters (search / status tabs / branch / channel) | missing | implemented |
| Automated tests | 0 | 13, passing |
| Correctness | showed staff names in the Customer column on walk-in sales | correct |
| To run | `npm install` + 2 servers + CORS | one command |

The SPA is preserved in full on the **`frontend-react`** branch
(`git checkout frontend-react`) — nothing was lost. Its one genuinely better idea,
**Category management**, was ported into the Django UI.

The **DRF API (`/api/`) is kept** — it works, enforces the same branch scoping and
stock guards, and is defensible as API-ready architecture. Its branch scoping now
imports `core.scoping` rather than keeping its own copy, so the API and the template
views can never drift apart.

### Apps

| App | Responsibility |
|---|---|
| `core` | Shared RBAC (`permissions.py`) and multi-branch scoping (`scoping.py`) |
| `accounts` | Custom `User` + role profiles, auth, User Management |
| `branches` | Branch Management |
| `inventory` | Products, category management, stock, batches, withdrawal slips, alerts engine |
| `sales` | Order Console, POS, order lifecycle |
| `billing` | Billing records, payment verification |
| `procurement` | Suppliers, purchase orders, deliveries |
| `orders` | Customer storefront (catalog, cart, checkout, "Report a problem" on a received order) |
| `damage_reports` | Damaged Product Reports: photo evidence, manager review, stock / refund / supplier-return follow-through |
| `expenses` | Operating expenses (Owner and Branch Manager only) feeding the Profitability report |
| `reports` | Dashboard, reports, CSV / PDF / Excel export (`exports.py`, `export_views.py`), Profitability (`profitability.py`) |
| `audit` | Append-only Audit Log of every data change (System & Monitoring) |
| `backups` | Encrypted off-site backups to Backblaze, restore check, status page (System & Monitoring) |
| `offline_sync` | Server ledger + endpoints behind the offline queue (see "Offline mode") |

---

## How the key rules are enforced

- **Per-user module actions** — Owner accounts configure View, Add, Edit and
  Delete per module for each Manager or Staff account. View is a mandatory
  parent permission, and the same centralized gate protects template pages
  and `/api/` routes before their existing role and branch checks run.

- **Damaged Product Reports** — staff file a report from Floor Stock, Warehouse Stock or a
  received purchase order (product, batch, quantity up to what is on hand, type, description,
  up to 5 JPG/PNG photos of 5 MB each); a customer files one from a received order inside the
  7-day refund window and must attach a photo. Filing moves nothing. A Branch Manager (own
  branch) or the Owner reviews it under *Inventory → Damage Reports* and approves or
  rejects it; approval is the one place anything changes, in one transaction, once:
  *write-off* deducts the units from the floor or warehouse by batch and generates a
  withdrawal slip linked to the report; a customer *refund* goes through the existing refund
  workflow (judged at the day the customer filed, not the day it was decided);
  *return to supplier* only flags the goods against the purchase order (the supplier return
  takes them out of the warehouse when it ships). A rejected report keeps its photos. Photos
  are private (never under `/media/`) and are streamed through role/branch-checked views.
  Every status change is in the Audit Log and notifies the reporter; new reports notify the
  Owner and branch Manager. The Inventory Report has a *Damaged Goods* section (units and
  estimated loss per product and branch).
- **Profit and expenses** — *Reports → Profitability* shows, for a date range and branch,
  gross sales less approved refunds (net sales), less cost of goods sold, less expenses =
  net profit, with a green/red verdict, a per-branch comparison (Owner), a monthly trend and
  the ten most and least profitable products; procurement spend is shown for information and
  never subtracted. Cost of goods uses `SalesItem.unit_cost`, frozen when the stock left the
  shelf from the batch's purchase-order price (else the product's latest `cost_price`), so a
  later supplier price change never rewrites past profit; a product with no cost is listed,
  not counted as free. Expenses are entered under *System & Monitoring → Expenses* by the
  Owner or a Branch Manager (own branch); Staff cannot open either. After deploying run
  `python manage.py migrate`, then optionally `python manage.py backfill_sale_costs` to
  freeze costs on sales recorded before this existed (`--dry-run` first). Definitions:
  `docs/METRICS.md` → Profitability.
- **Branch scoping** — `core/scoping.py` filters every queryset server-side. The UI's
  branch dropdown is never the only guard.
- **Stock moves exactly once** — walk-in sales deduct at the point of sale; online
  orders deduct when *Completed* (by confirming the delivery or pickup, see below).
  Deliveries add stock only on receipt validation
  (business rule 13), and re-receiving is blocked.
- **ZANK POS daily-sales import** — authorized Sales / Order Console users can upload
  a ZANK CSV or `.xlsx` export, select an accessible branch, and review valid rows,
  invalid rows, unmatched SKUs/barcodes, duplicate invoice/branch pairs, and totals
  before confirming. Confirmation atomically creates completed walk-in sales, paid
  billing records, and normal floor-stock movements. Imported sales retain the source
  invoice, terminal, cashier, tax, discount, and line values, and every confirmed file
  appears in branch-scoped import history. The import is file-based; no ZANK service or
  payment gateway is contacted.
- **Audit trail** — every stock change writes a `StockMovement` row recording the
  before/after quantities, the reason, and who did it.
- **Audit Log** — `audit/` records every create, edit and delete on the models listed in
  `audit/registry.py` (actor, action, record type and ID, timestamp, before/after values),
  viewable by Owner accounts under *System & Monitoring → Audit Log*. See
  [Audit log](#audit-log) below.
- **Payments are recorded, not processed** — no gateway is integrated, per the
  documented scope limitation. Staff confirm cash or a GCash reference.
- **Delivery / pickup confirmation** — an online order's hand-over leaves proof. The
  lifecycle has no separate "Out for delivery" / "Ready for pickup" status; both are the
  **Preparing** stage (Delivery vs Pickup fulfilment). On a Preparing order, Staff / Branch
  Managers / the Owner open **Confirm Delivery** (or **Confirm Receipt** for a pickup) on
  Order Detail and record who received it, when, and optionally a photo and a signature drawn
  on a canvas. Saving issues a receipt number (`GP-RCPT-YYYYMMDD-NNNNN`) and completes the
  order through the same rules as the status control (payment must be Paid, stock deducted
  once) — if payment is not Paid nothing is recorded. **This is the only way an online
  order is completed:** the Order Detail status control no longer offers *Completed* for
  one (a walk-in still has it), and the status endpoint refuses it. An online order that
  was completed before that rule, with no hand-over on record, can still be recorded
  afterwards from the same button: it issues the receipt and leaves the order's status,
  stock and refund window untouched. The customer can add their own
  **Confirm I received my order** on Track Order, before or after the store; once both have
  confirmed the order is *fully confirmed*. Each side confirms once; cancelled or refunded
  orders cannot be confirmed. The receipt is viewable and downloadable as a PDF by the
  customer and by branch staff, the photo and signature sit on private storage served only
  through authorization-checked views, and the Order Console has a Confirmation column and
  filter. Code: `sales/receipts.py` (rules and service), `sales/receipt_views.py`,
  `orders/receipt_views.py`.
- **Refunds** — an approval workflow on a Completed + Paid order, within
  `SalesTransaction.REFUND_WINDOW` (3 days) of completion. Anyone with access to the
  order's branch (Staff included) can *request* a refund; it stays **Pending** until an
  Owner or that branch's Manager **approves or rejects** it (a rejection needs a reason).
  An Owner/Manager's own request is approved as it is submitted. Every request carries a
  reason code (defective, wrong item, expired, not as described, pricing error, customer
  changed mind, other — "other" needs a note), a refund method, and per-line quantities
  and condition; whether it is a **Full** or **Partial** refund is decided by the server
  from what it covers, and an order can be refunded more than once. A Pending request
  reserves its units so they can't be requested twice; a rejection releases them.
  **Approval** is the one moment anything moves, in a single locked transaction:
  Sellable lines restock the branch floor (a fresh, no-expiry batch, with a
  `Customer Return` stock movement), `SalesTransaction.refunded_amount` grows
  (`net_amount` = `total_amount` − `refunded_amount`; `total_amount` is never edited),
  and the billing record steps to Partially Refunded / Refunded. Damaged lines are
  written off with no stock effect. An order with an approved refund can't be voided
  (that would restock the same units twice); voiding an order closes its Pending
  requests. Like billing, it only ever *records* that money was returned. Every step is
  in the append-only `RefundEvent` log, and the order's lifecycle changes are in
  `OrderStatusHistory` (orders that predate it have none) — both shown on the order's own
  page (Sales & Orders) and served, to internal roles only, by `GET /api/sales/<id>/`.
  The API mirrors the pages: `GET`/`POST /api/sales/<id>/refunds/` and
  `POST /api/sales/refunds/<id>/approve|reject/`. The rules live in `sales/refunds.py`.
- **PWD discount requests (online pickup)** — a customer checking out for *Pickup* (which
  is also the only fulfilment *Pay at Store* allows) can tick "request the PWD discount"
  and enter the ID number and name on the card. This is a *request*, not a claim: the
  order is still priced in full until a staff member sees the ID at pick-up and
  approves it on the order's page (Sales & Orders), which re-prices the order through
  the same VAT-first, 20% engine as the walk-in discount (`sales/pwd_requests.py`) and
  updates the amount due. Rejecting needs a reason and leaves the price alone. Billing
  refuses to mark the order *Paid* while a request is pending, and an order can only be
  *Completed* once Paid, so the ID is always checked before payment is confirmed. A
  delivery order can never carry a request. The REST API mirrors all of it
  (`fulfillmentType`, `pwdDiscountRequest` on `POST /api/sales/`;
  `POST /api/sales/<id>/pwd-request/` to approve/reject). No ID image is stored — only
  the typed details, and the reviewer's name and time on the decision.
- **Suppliers & purchase orders** —
  *Categories:* a supplier has one category from a managed list (Procurement →
  Suppliers → Supplier Categories, Owner-edited). *Offers:* a supplier's own "add a
  product" is a **Proposed** offer with its cost; it becomes a preferred PO suggestion
  after Owner approval, and a product not yet on the master list is requested and only
  created on approval. Supplier-product links preserve known costs and rank suggestions,
  but they do not restrict PO creation: authorized users can manually choose any product
  from the master list on both the web form and API. Suppliers can remove their own
  product links, but not while one of their open orders needs one. *Funding:* every PO
  records where the money comes from
  (Cash on Hand, Bank Transfer, GCash, Owner's Funds, or Other + note); editable until
  the terms are agreed. *Optional supplier portal:* approval saves the PO in the system.
  A supplier may negotiate and acknowledge it through their account, but their login is
  not required: Owner/Manager/Staff with delivery-edit access can record agreed terms
  after a phone call, message, or email and mark it **Ordered**. That state
  is immediately eligible for store-verified receiving; a later portal acknowledgement
  is informational. *Receiving:* each shipment records who received it and,
  optionally, the courier (`DeliveryReceipt`); stock is only added at that validated
  receipt. *Returns:* goods can be sent back against a received PO — supplier
  acknowledges, the store ships (units leave the **warehouse** exactly once, as
  `Supplier Return` stock movements from that order's own lots), then it is settled by a
  credit note or a replacement received into the warehouse. *Overdue:* see
  [Notification operations](#notification-operations). The rules live in
  `procurement/services.py`.

---

### Input limits and pagination

**Limits.** Every numeric input has an allowed range, defined once in
[`core/limits.py`](core/limits.py): a unit price (retail, bulk, supplier cost)
is ₱0.01 – ₱250,000.00; quantities are 0 – 100,000 units; a single sale, cash
tendered or credit note is at most ₱10,000,000.00; a supplier lead time is at
most 365 days; product names are at most 150 characters, barcodes 64,
descriptions 1,000. The same numbers are enforced at four layers so they cannot
drift: the HTML `min`/`max`/`maxlength` attributes (via the `limits` template
variable), the server-side view (`parse_money` / `parse_quantity` /
`parse_text`, which return a plain-English message such as "Retail price cannot
be more than ₱250,000.00."), the model validators, and the DRF serializers.
`static/js/input-limits.js` replaces the browser's generic wording with the same
messages. To change a limit, edit `core/limits.py` and add a migration if a
model validator moved (`makemigrations` — it is SQL-free).

**Pagination.** Long lists show 15 rows a page (10 for customer-facing pages,
12 cards in the storefront catalog, 25 for the movement log) with
Previous / numbered pages / Next and a "Showing 1–15 of 40 products" line.
Views call `core.pagination.paginate(request, queryset)`; templates call
`{% pager page_obj noun="product" %}`, which keeps the page's current search and
filters in every link. Counts, tab totals and report totals always describe the
whole filtered list — only the table is cut into pages, and CSV exports are not
paged. Search boxes submit to the server (after a short pause) so they search
every page, not just the one on screen. The Notification Center and Alerts
Center filter in the browser, so they page in the browser too
(`static/js/client-pager.js`).

## Security notes

- Secrets live in `.env` (git-ignored). `.env.example` documents what's needed.
- The app **refuses to start** with `DEBUG=False` and the default `SECRET_KEY`.
- ⚠️ **The original MySQL password was committed to git history.** It has been removed
  from the working tree, but it still exists in past commits — **rotate the MySQL
  password** and treat the old one as compromised.

---

## Audit log

Every create, update and delete on the models in `audit/registry.py` writes one
`AuditLog` row: who (user ID and role, or *System*), what (action, record type, record ID),
when, from where (IP), and only the fields that changed (before/after). Rows from one
request share a `request_id`. Viewable by Owner accounts and Django superusers at
*System & Monitoring → Audit Log*.

- **Append-only.** Rows can only be inserted: the model and queryset refuse edits and
  deletes, the Django admin is read-only, and MySQL triggers (`audit/triggers.py`,
  migration `0003`) refuse them at the database level. Triggers need the `TRIGGER`
  privilege; if the database user lacks it, `migrate` prints a warning and the
  application-level protection remains. Check with
  `SHOW TRIGGERS LIKE 'audit_auditlog';`. `AUDIT_DB_TRIGGERS=False` skips them (the test
  suite does this automatically).
- **Never recorded:** passwords/hashes, tokens, and uploaded file contents or names
  (recorded as `[file]`). Personal fields (names, email, contact numbers, addresses,
  delivery addresses, PWD/Senior ID numbers) are recorded as *changed* with the value
  shown as `[personal data]`, so an erasure request (`hard_delete_personal_data`) is not
  undone by the log. Records are named by non-personal fields only (order number, product
  name); users read as `User account #<id>`.
- **Adding a model:** add it to `REGISTRY` in `audit/registry.py`, naming its `mask`,
  `exclude` and `repr_field`. `python manage.py test audit` checks the entry against the model.
- **Not captured:** `QuerySet.update()` and `bulk_create()` emit no Django signals, so
  they bypass the log — save row by row where history matters (price tiers do).
  Stock quantities are excluded from generic tracking; the `StockMovement` ledger owns them.
  Logins and failed logins are deliberately not logged. History starts from the day the
  feature was deployed.
- **Fail-open.** A failure while writing an entry is logged (and reaches Sentry) but never
  blocks the business action it was recording.
- **Bulk loading.** `seed_demo` and `rebuild_store_records` run with capture suppressed
  (`audit.context.suppressed`). `AUDIT_ENABLED=False` switches capture off entirely.

## Backups

The database and uploaded files (payment proofs) are backed up daily, **encrypted before
they leave the server**, to a private Backblaze B2 bucket. The full runbook — schedule,
retention (14 daily / 8 weekly / 12 monthly), access controls, Railway setup, restore
procedure and the restore-test log — is [`docs/backup-and-restore.md`](docs/backup-and-restore.md).
Owners see status, run a backup on demand and record restore tests at
*System & Monitoring → Backups*.

- `python manage.py run_backup` — one full backup. The web service starts it itself every
  day at 03:00 Manila time (`backups/scheduler.py`); no separate cron service is needed.
- `python manage.py verify_backup` — the restore test: downloads the newest backup,
  restores it into a scratch database and proves it matches. Never touches the live database.
- `python manage.py reapply_erasures` — after a restore, re-erase accounts whose personal
  data was erased after that backup was taken.
- `python manage.py scrub_deleted_account_names` — blanks the person-name columns
  (`owner_name`, `manager_name`, `staff_name`, supplier `contact_person`) that hard deletes
  made before October 2026 left behind; `--dry-run` lists what it would touch without
  changing anything. Safe to re-run. Run it once per database (local, Railway), and again
  after restoring a backup taken before that fix, since `reapply_erasures` skips accounts
  already marked deleted.
- Needs `mysqldump`/`mysql` (installed on Railway by `nixpacks.toml`; set
  `BACKUP_MYSQLDUMP` / `BACKUP_MYSQL` locally if they aren't on PATH) and the `B2_*`,
  `BACKUP_ENCRYPTION_KEY` variables from `.env.example`.
  `python manage.py test backups` runs without any of it (a real dump/restore test runs
  automatically when the MySQL tools are found).

## Offline mode

Staff, Branch Managers and the Owner can keep working through a dropped
connection. This is a *queue*, not a second copy of the system: nothing is
decided offline, and nothing counts as saved until the server says so.

**What works offline**

| Area | Viewing (saved pages) | Entering (queued, synced later) |
|---|---|---|
| Sales | Walk-in Sale page, sale list | Walk-in sale — Cash, GCash, Card (incl. the proof screenshot) |
| Inventory | Floor stock, warehouse stock, alerts, withdrawals | Floor refill request; floor stock count correction; write-off request; new product (with photos); new product category |
| Procurement | PO list/detail, incoming deliveries, suppliers | Purchase order request (Branch Manager); delivery receipt; new supplier, supplier category, or product linked to a supplier (Owner) |

Data entry is limited to **creating** a record or a request. The same rules as
online apply on sync (unique names and barcodes, valid prices, per-user Add
grants), so a duplicate or a missing grant comes back as Sync Failed with the
reason and nothing is saved. **Editing** an existing record is not queued: the
form was filled in against a snapshot, and replaying it later would overwrite
whatever changed in the meantime.

"Saved pages" are copies of pages you opened while online (and the sidebar
pages, warmed in the background every 30 minutes). They are served only when the
network fails, with a *Saved copy* notice that lists which of the queueable
entries above are actually on that page (and says so when there are none). Online,
you always get the live page. Any other form submitted while offline is blocked with
a message naming what *can* be saved offline.

**What stays online-only** (the server refuses these even if a browser tried):
approvals (POs, withdrawals, refunds), payment verification, refunds and
voids, order status changes, anything an Owner raises that authorizes spend
(an Owner's PO), editing or deleting existing products, categories, stock
details or suppliers, user/permission/settings changes, imports, reports,
login/2FA/password, and the customer storefront.

**Status** — one pill in the top bar:

| Pill | Meaning |
|---|---|
| Online | Reachable (a ping, not just `navigator.onLine`), nothing waiting |
| Offline | Server unreachable; changes are saved on this device |
| Pending Sync | Saved on the device, not yet confirmed by the server |
| Synced | The server confirmed the change (shown briefly, then Online) |
| Sync Failed | The server refused it, the data changed underneath it, or syncing hit an error. The reason is in the panel; Retry or Discard |

**How it stays correct**

- Each queued change gets a random operation id. `offline_sync.OfflineOperation`
  records applied ids per user, so a retry, a lost response or two tabs can never
  apply a change twice. Only *Applied* is sticky: a rejected change changed nothing
  and may be sent again.
- The server **replays the real view** for each change (`offline_sync/replay.py`),
  as the same user, so validation, pricing, branch scoping, per-user module
  permissions and stock rules are exactly the online ones. `registry.py` is the
  allow-list.
- A change is *Synced* only when the server answers `applied` for that operation
  **and** the effect was read back from the database (`registry.verify`). A redirect
  alone is never trusted. A network failure leaves it Pending.
- Anything refused is rolled back whole — no half-applied sale.
- Conflicts: a stock count carries the system quantity it was counted against;
  if stock moved while offline, it is refused ("count again") instead of
  overwriting. A sale that would oversell is rejected with the shortage, kept in
  the panel, and can be retried once stock is corrected.
- A queue is only synced by the account that made it. Saved pages are dropped
  at login/logout and when the signed-in user changes.
- The POS subtracts what the device has already sold offline from the stock
  it shows, so the till cannot sell the same last unit twice.

**Limits to know**

- Syncing happens while the app is open (no background sync); it starts
  automatically on reconnect.
- The 30-minute idle logout still applies. Offline, that sends you to a page that
  cannot sign in until the connection returns; saved changes stay on the device and
  sync after you sign in as the same user.
- A change is held in the browser's IndexedDB until synced. A shared PC should be
  signed out and its pending changes synced or discarded.
- An offline sale gets its order number only when it syncs, so there is no
  official receipt until then.
- The sidebar icons come from a CDN script; it is cached after first load, but a
  device that has never loaded it online shows text labels only.

Tests: `python manage.py test offline_sync` (server) and
`node --test offline_sync/js_tests/offline_sync.test.mjs` (client + service
worker logic against fakes; needs Node 20+, no packages). After deploying,
run `python manage.py migrate` — `offline_sync` adds one table.

### Windows desktop app

[`desktop/`](desktop/) packages the live site as an installable Windows app
(Electron): the same pages, sign-in and permissions, the offline queue above
running unchanged inside it, and the front-end files bundled locally so it opens
without internet. It adds no server endpoints and does not affect the browser
version. The Node toolchain lives only in `desktop/`; the Django app has no
Node build step.

Build and test: see [`desktop/README.md`](desktop/README.md). To show staff a
download link, host the installer anywhere with an https URL and set
`DESKTOP_APP_DOWNLOAD_URL` (the Download page then shows a *Download for
Windows* card to internal roles), or keep it in a private Backblaze bucket
with the `DESKTOP_APP_B2_*` variables so only signed-in staff can fetch it via
`/download/desktop/` (see `desktop/README.md`). The desktop app's sessions appear as
"GerlynPLUS Desktop" on the Sessions page.

## Notification operations

The event, recipient-role, timing, priority, and reference rules are defined
in [`docs/notification-matrix.md`](docs/notification-matrix.md). Run
Run `python manage.py dispatch_notifications` once per day. It atomically
writes off batches whose expiry date has arrived from floor/warehouse stock,
records one batch-linked `Expiry Write-off` movement per lot, and then sends
near-expiry alerts. The command is idempotent. For an expiry-only manual run,
use `python manage.py process_expired_stock`.

The same job also sweeps overdue purchase-order deliveries
(`python manage.py process_overdue_deliveries` to run just that): an Incoming or
Partially Received order is flagged and the buyer and supplier notified once it
is `PO_OVERDUE_GRACE_DAYS` (default 1) past its delivery date, and escalated to
the Owner at `PO_OVERDUE_ESCALATE_DAYS` (default 3). Orders are never
auto-cancelled; the Owner reschedules or cancels from the PO detail page.

## Scope limitations (per Chapter 1)

The following are intentionally **out of scope**:

1. No online payment gateway — payment is recorded only; funds move outside the system.
2. No delivery/logistics or courier integration.
3. No HR functions (payroll, attendance, evaluation) — staff IDs exist for the audit trail.
4. No integration with external accounting or third-party systems.
5. Does not replace the existing POS hardware — this is the back-end IS layer.

## Account validation

Account field limits, the completed audit, and reproducible validation commands
are documented in [docs/account-validation.md](docs/account-validation.md).

# Private uploads and copied links

Payment proofs and profile avatars are served by authenticated Django views.
The `/media/` route serves only public product, category, and store images;
it must not be replaced by a web-server rule that exposes the whole media
directory. Mount `MEDIA_ROOT` on a persistent **private** Railway volume before
deploying: both proof images and new avatars are stored there. Keep that volume
outside any static hosting or public bucket.

After deploying the avatar storage migration, copy any avatars previously
uploaded to Cloudinary into `MEDIA_ROOT/avatars/<user id>/` using the existing
file names, then remove the old public Cloudinary assets. Until copied, those
older avatars will not render; until removed, their old public URLs remain
accessible independently of Django. Audit any historic payment-proof files
that were placed on public storage and remove those public copies after the
private originals are confirmed. Run `python manage.py migrate` as part of the
deploy.

Back-office pages, sensitive API results, CSV/XLSX/PDF exports, payment proofs, avatars,
damage-report photos and expense receipts (`media/damage_reports/`, `media/expense_receipts/`;
never served from `/media/`)
require a current session and the applicable role, branch, or record ownership.
Authenticated responses carry `Cache-Control: no-store`. Test copied links in
a signed-out browser and under a second role after deployment, using test data.
Public storefront APIs still expose branch locations, catalog prices, and
product availability; they omit account owner identity and exact stock counts.
