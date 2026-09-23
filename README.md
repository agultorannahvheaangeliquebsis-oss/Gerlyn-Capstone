# GerlynPLUS

**Process-Linked Unified Systems Enterprise** — an integrated information system for
Gerlyn Variety Store, a multi-branch retail enterprise in Baliuag, Bulacan.

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
| **Inventory** | Product master list, per-branch stock, FEFO batches/expiry, withdrawal slips with receipt acknowledgment |
| **Procurement** | Suppliers, purchase orders, and deliveries that update stock only after receipt validation |
| **Reporting** | Dashboard + Sales/Inventory/Procurement/Billing reports, CSV & PDF export, consolidated or per-branch |

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

# 7. Start it
python manage.py runserver
```

Open <http://127.0.0.1:8000/> — the storefront. Log in at `/login/` for the internal system.

**To let other branches connect during the demo,** add the server's LAN IP to
`DJANGO_ALLOWED_HOSTS` in `.env`, then run
`python manage.py runserver 0.0.0.0:8000`.

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
| `orders` | Customer storefront (catalog, cart, checkout) |
| `reports` | Dashboard, reports, CSV export |
| `audit` | Append-only Audit Log of every data change (System & Monitoring) |
| `backups` | Encrypted off-site backups to Backblaze, restore check, status page (System & Monitoring) |

---

## How the key rules are enforced

- **Branch scoping** — `core/scoping.py` filters every queryset server-side. The UI's
  branch dropdown is never the only guard.
- **Stock moves exactly once** — walk-in sales deduct at the point of sale; online
  orders deduct when marked *Completed*. Deliveries add stock only on receipt validation
  (business rule 13), and re-receiving is blocked.
- **Audit trail** — every stock change writes a `StockMovement` row recording the
  before/after quantities, the reason, and who did it.
- **Audit Log** — `audit/` records every create, edit and delete on the models listed in
  `audit/registry.py` (actor, action, record type and ID, timestamp, before/after values),
  viewable by Owner accounts under *System & Monitoring → Audit Log*. See
  [Audit log](#audit-log) below.
- **Payments are recorded, not processed** — no gateway is integrated, per the
  documented scope limitation. Staff confirm cash or a GCash reference.
- **Refunds** — Owner/Branch Manager only, on a Completed + Paid order, within
  `SalesTransaction.REFUND_WINDOW` (3 days) of completion. A refund targets specific
  items and quantities (an order can be refunded more than once), records who
  authorized it, how the money was given back, and each item's condition — Sellable
  restocks the branch floor (a fresh, no-expiry batch, since a returned unit has no
  traceable original lot), Damaged does not. Like billing, it only ever *records* that
  money was returned; nothing moves. Order status and the billing record's Paid status
  are left untouched — the `Refund`/`RefundItem` rows (`sales/models.py`) are the source
  of truth. Action and history live on the order's own page (Sales & Orders).

---

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
- **Seeding.** `seed_demo` runs with capture suppressed (`audit.context.suppressed`).
  `AUDIT_ENABLED=False` switches capture off entirely.

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
- Needs `mysqldump`/`mysql` (installed on Railway by `nixpacks.toml`; set
  `BACKUP_MYSQLDUMP` / `BACKUP_MYSQL` locally if they aren't on PATH) and the `B2_*`,
  `BACKUP_ENCRYPTION_KEY` variables from `.env.example`.
  `python manage.py test backups` runs without any of it (a real dump/restore test runs
  automatically when the MySQL tools are found).

## Notification operations

The event, recipient-role, timing, priority, and reference rules are defined
in [`docs/notification-matrix.md`](docs/notification-matrix.md). Run
Run `python manage.py dispatch_notifications` once per day. It atomically
writes off batches whose expiry date has arrived from floor/warehouse stock,
records one batch-linked `Expiry Write-off` movement per lot, and then sends
near-expiry alerts. The command is idempotent. For an expiry-only manual run,
use `python manage.py process_expired_stock`.

## Scope limitations (per Chapter 1)

The following are intentionally **out of scope**:

1. No online payment gateway — payment is recorded only; funds move outside the system.
2. No delivery/logistics or courier integration.
3. No HR functions (payroll, attendance, evaluation) — staff IDs exist for the audit trail.
4. No integration with external accounting or third-party systems.
5. Does not replace the existing POS hardware — this is the back-end IS layer.
