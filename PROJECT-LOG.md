# GerlynPLUS — Project Log & Testing Guide

**For the team.** This explains what was done to the system, why, and — most
importantly — **how you can test it yourself** (jump to
[Part 3](#part-3--how-to-test-it-yourself)).

---

## Part 1 — Where we started

The project looked finished. It wasn't.

- The UI mockups were polished and complete.
- **The database was completely empty** — 0 users, 0 branches, 0 products.
  The screens people were showing around were a front-end mock; nothing was
  behind them.
- Roughly 60–70% of the Django code existed (inventory, procurement, sales,
  billing), but big parts were never connected:
  - The **Withdrawal Slip** views were written but had **no URLs and no
    templates** — they could never open in a browser.
  - **Customer checkout crashed 100% of the time.** No online order could ever
    be placed.
  - Three templates (`cart.html`, `confirmation.html`, `select_branch.html`)
    were **empty 0-byte files**.
- Nothing was verified. There were no tests.

So the work was: make it real, make it correct, make it provable.

---

## Part 2 — What was done

### The foundation
- **Demo data on demand.** One command (`python manage.py seed_demo --reset`)
  builds the whole store: 3 branches, 18 users across all 5 roles, the 15-product
  catalog, stock levels, 8 suppliers, 7 purchase orders, 10 orders. It matches the
  mockups exactly — including the 6 low-stock alerts.
- **Branch scoping in one place** (`core/scoping.py`). A manager or staff member
  only ever sees their own branch. This is enforced **on the server**, not by
  hiding buttons.
- **Audit trail.** Every stock change records what moved, before → after, why,
  and who did it.

### The five core processes (all working end-to-end)
| Process | What it does now |
|---|---|
| Sales & Orders | Walk-in POS + Order Console with the full lifecycle (Pending → Approved → Preparing → Completed / Cancelled) |
| Billing | A record per sale; payment method + status, GCash reference, proof upload |
| Inventory | Products, **categories**, per-branch stock, expiry batches, withdrawal slips with receipt acknowledgment, Alerts Center |
| Procurement | Suppliers, purchase orders, and deliveries that update stock **only after receipt validation** |
| Reporting | Dashboard + 4 reports, CSV export, PDF via print, consolidated or per-branch |

### The redesign
The UI was rebuilt to the design system: one hero metric instead of eight
competing cards, 16px minimum text, 44px tap targets, status pills that pair a
**dot + full label** (readable for colour-blind staff, and they can't be
truncated), tables that become cards on mobile, and a sidebar that becomes a
bottom nav on a phone.

It uses **no CDN, no external fonts, no build step** — so it works offline and
starts with one command.

### Security
- Secrets moved out of the code into `.env` (which is git-ignored).
- **The MySQL password was rotated** — the old one had been committed to git.
- The app no longer runs as MySQL `root`; it uses a limited `gerlyn_app` user.
- The app refuses to start in production mode with an insecure key.

---

## Bugs found and fixed

These were all real. Most would have broken a live demo.

| # | Bug | Why it mattered |
|---|---|---|
| 1 | Auto-numbering crashed on **every** create (`Batch`, `StockTransfer`, `Procurement`, `SalesTransaction`) — it tried to INSERT the same row twice | Nothing could be created through the ORM |
| 2 | **Customer checkout crashed every time** — it created a billing record the system already creates automatically | No online order could ever be placed |
| 3 | `cart.html`, `confirmation.html`, `select_branch.html` were **empty files** | Those pages could not render |
| 4 | Checkout deducted stock, and completing the order deducted it **again** | Stock would leave the shelf twice |
| 5 | Receiving a purchase order twice **added the stock twice** | Inventory silently inflates |
| 6 | Purchase orders weren't scoped for staff | Staff could see **every branch's** POs |
| 7 | The Dashboard and order detail **crashed on any walk-in sale** | The main screen broke on real data |
| 8 | The Procurement Report pointed at a template that doesn't exist | That page always returned a 500 error |
| 9 | The product form only offered Sack/Kilo/Piece | Editing a product silently corrupted its unit (Can → Sack) |
| 10 | Manager and staff dashboards were **static pages with no data** | Their landing page showed nothing |
| 11 | Withdrawal, warehouse and delete views had **no URL routes or templates** | Written but unreachable |
| 12 | `requirements.txt` was saved as **UTF-16** | `pip install -r requirements.txt` fails on many machines |
| 13 | `.gitignore` said `venv/` but the folder is `.venv/` | One `git add .` from committing the whole virtualenv |
| 14 | User admin URLs sat under `/admin/`, which Django owns | Those pages 404'd |
| 15 | `make_random_password` was removed in Django 5 | Would crash when adding a supplier |

A second, larger review followed (8–9 Sept 2026): 15 security/correctness
findings (F-01…F-15), 15 storefront-truthfulness fixes, and the fail-open
branch-scoping fix — an unassigned Manager/Staff could read every branch's
data. All are on `main`. Details and what's still open:
[`docs/audit-report-2026-09-09.md`](docs/audit-report-2026-09-09.md) and
[`docs/next-fixes-plan.md`](docs/next-fixes-plan.md).

---

## The React question (settled with evidence)

A teammate had also built a **React/Vite SPA** — real work: 28 files, ~5,500
lines, and it runs. Rather than argue about it, both were tested side by side in
a real browser.

| | React SPA | Django templates |
|---|---|---|
| Withdrawal Slips (a core process) | **missing** | implemented |
| CSV export | missing | 4 reports |
| Order search / status tabs / filters | missing | implemented |
| Automated tests | 0 | full suite, passing |
| Correctness | showed **staff names in the Customer column** on walk-in sales | correct |
| To run | `npm install` + 2 servers + CORS | one command |
| Category management | **had it** ✅ | *(was missing — now ported over)* |

**Decision: Django templates.** More complete, verified, no build step, and one
thing to start during a defense.

**Nothing was lost.** The React app is preserved in full:

```bash
git checkout frontend-react     # the complete SPA, exactly as it works
```

And the teammate's **DRF API (`/api/`) is kept** — it's good work, it enforces the
same branch scoping and stock rules, and it's defensible as "API-ready
architecture" in the paper. Its scoping now imports `core/scoping.py` instead of
keeping its own copy, so the API and the website can't drift apart.

---

# Part 3 — How to test it yourself

You don't need to know Django. Follow these steps.

## First time only (once per computer)

```bash
cd Gerlyn
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then open .env and put in your MySQL password
python manage.py migrate
python manage.py createcachetable  # login lockout needs this; migrate doesn't do it
```

> `createcachetable` is a one-time step, separate from `migrate`. The login
> lockout (5 wrong passwords) counts attempts in a `DatabaseCache` table so all
> server workers share one count; without the table, `./run.sh test` errors and
> the lockout can't record anything. Safe to re-run any time.

## Every time you want to test

```bash
./run.sh
```

That's it. It prints the links and the logins. Stop it with **Ctrl+C**.

| Command | What it does |
|---|---|
| `./run.sh` | Start the system |
| `./run.sh fresh` | Reset the demo data back to normal, then start |
| `./run.sh stop` | Stop it |
| `./run.sh test` | Run the core acceptance tests (fast) |
| `./run.sh test-all` | Run the full suite, every app (a few minutes) — do this before a merge |

> **If you break the data** (deleted stock, cancelled everything) — don't panic.
> Run `./run.sh fresh` and it's back to normal.

## The logins

Open **http://127.0.0.1:8000/** — the password for everyone is **`demo1234`**

| Login | Who they are | What they should see |
|---|---|---|
| `gerlyn` | Owner | Everything, all 3 branches |
| `carlo` | Manager | **Tarcan only** |
| `ana` | Staff | Tarcan only, can use the POS |
| `juan` | Customer | The shop at `/shop/` |

---

## Test 1 — The Owner sees the whole business (1 min)

1. Log in as **`gerlyn`**.
2. You land on the **Dashboard**.

**You should see:** one big "today's sales" number, a row of smaller stats, and
**Restock Suggestions** listing 6 products that are low.

---

## Test 2 — A manager can only see their own branch (2 min)

This is the most important thing to check. It's what the whole thesis is about.

1. Keep `gerlyn` open.
2. Open a **private / incognito window** and log in as **`carlo`**.
3. In both windows, go to **Sales & Orders**.

**You should see:** `gerlyn` has **more orders** than `carlo`. Carlo only sees
**Tarcan**. He has no Administration menu at all.

4. As `carlo`, type this in the address bar: `127.0.0.1:8000/manage/users/`

**You should see:** he gets bounced out. He cannot manage users even by typing
the address directly.

---

## Test 3 — Sell something at the counter (2 min)

1. Log in as **`ana`** → **Sales & Orders** → **New Walk-in Sale**.
2. Pick a product, set quantity **4**, and watch the total update as you type.
3. Click **Record Sale**.

**You should see:**
- The sale appears with a new order number.
- **Stock per Branch** → that product went **down by 4**.
- **Billing** → a record was created automatically, already marked **Paid**.

---

## Test 4 — A customer orders online (3 min)

This is the loop that was completely broken before.

1. Private window → log in as **`juan`** → go to **`/shop/`**.
2. Choose a branch → add something to the cart.
3. Open the **cart** — notice the bar showing how much more you need for free
   delivery.
4. Checkout with **Cash on Delivery**.

**You should see:** an order confirmation with an order number.

5. Now switch to **`ana`** → **Sales & Orders**. The order is there as **Pending**.
6. Click **View**, then move it: **Approve → Preparing**, then click **Confirm
   Delivery** (the order is Cash on Delivery, so verify its payment first) and
   fill in who received it.

**You should see:** the stock only goes down **when the order is completed by
that confirmation** — not before. That's deliberate: the item hasn't left the
store until then. The order gets a receipt number (`GP-RCPT-…`).

---

## Test 5 — Receive a delivery (2 min)

1. Log in as **`gerlyn`** → **Deliveries**.
2. Click **Receive** on a purchase order.
3. **Type in fewer than what was ordered** (e.g. ordered 80, type 75).
4. Confirm.

**You should see:** the order is flagged **Discrepancy**, and only the **75 that
actually arrived** were added to stock — not 80. This is the rule that digital
stock must match the physical goods.

---

## Test 6 — Reports (1 min)

1. As `gerlyn` → **Reports → Sales Report**.
2. Click **Export CSV** — a real spreadsheet file downloads.
3. Click **Export PDF** — the print box opens; choose "Save as PDF".

**Bonus:** log in as `carlo` and export the same report — **his file has fewer
rows**, because he only gets Tarcan. The scoping applies to exports too.

---

## Test 7 — On your phone (2 min)

1. Make sure your phone is on the **same wifi**.
2. Run `./run.sh` — it prints an address like `http://192.168.x.x:8000/`.
3. Open that on your phone.

**You should see:** the sidebar becomes a **bottom menu**, and the tables become
**cards** instead of squashed columns.

> If the phone shows an error, add that IP address to `DJANGO_ALLOWED_HOSTS`
> in your `.env` file, then restart.

**This test is worth doing before the defense** — placing an order on your phone
and watching it appear on the laptop is the clearest proof that all branches
share one system.

---

## Test 8 — Let the computer check it for you (5 seconds)

```bash
./run.sh test
```

**You should see:** it ends with `OK` (not `FAILED`).

This automatically checks the five business rules above. `./run.sh test` runs
the `core` acceptance suite; the full check is
`python manage.py test accounts core sales orders billing inventory
procurement reports` (pass your own `DB_NAME`). **Run it after anyone
changes code.** If it says `FAILED`, something broke — tell the person who
changed it.

---

## What to do if something looks wrong

1. Run `./run.sh fresh` — most "bugs" are just messed-up test data.
2. Run `./run.sh test` — if that fails, it's a real bug.
3. Write down: **what you clicked**, **what you expected**, **what happened**.

---

## Known gaps (be honest about these)

- **Bundle Management is deliberately out of scope** — team decision, 17 July 2026.
  It appears in the UI mockups but not in the Chapter 1 Scope, the ERD, the DFD, or
  the 14 business rules. Building it would mean inventing a data model the approved
  design never described. It's recorded in Chapter 5 as future work.
  **If asked at the defense:** "The mockups were a design exploration; Chapters 1–3
  are the specification. We kept the build inside the documented scope."
- **PDF export** uses the browser's "Save as PDF" rather than generating the file
  on the server. This was deliberate — it avoids an extra dependency and keeps
  the system working offline.
- The automatic tests cover the five main business rules and permissions, not
  every single page.

---

## Where the other documents are

| File | What's in it |
|---|---|
| **`ROADMAP.md`** | **What's left: now → Final Defense (Nov 2026), including deployment** |
| `docs/audit-report-2026-09-09.md` | The 8–9 Sept code audit: every finding, the fix, the commit |
| `docs/next-fixes-plan.md` | What to fix next, prioritised, with recommendations |
| `README.md` | Setup, the tech stack, and how each business rule is enforced |
| `docs/evaluation-pack.md` | The 12-minute demo script, ISO 25010 self-assessment, and the evaluator questionnaires |
| `gerlyn-one-week-build-plan.md` | The original build plan and its status |
| `gerlyn-ui-redesign-brief.md` | The UI redesign brief |
