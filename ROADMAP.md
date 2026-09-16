# GerlynPLUS — Roadmap: Now → Final Defense

**Written:** 17 July 2026 · **Final Defense:** November 2026 · **Runway: ~4 months**

This plan follows the Gantt chart in your Chapter 3, and fills in the one phase
it's missing.

---

## ⚠️ First: your Gantt has no Deployment task

Your Chapter 3 timeline runs:

> System Development → System Testing & Refinement → **User Evaluation & Data
> Analysis** → Final Documentation → Final Defense

**Nothing in there says "deploy".** But Chapter 3 says the system will be evaluated
by **3 IT experts** and by **the store owner and staff from different branches**.
Those people cannot evaluate a system that only runs on your laptop.

**So deployment isn't the last step — it's the gate before User Evaluation.**
It has to be live by **early September**, or the evaluation phase can't start on
time. This is the single biggest scheduling risk in the project, and it's the main
thing this roadmap fixes.

---

## Where we actually are

**Working and verified**
- All 5 core processes end-to-end, plus the customer storefront and Administration.
- 38 internal pages × 5 roles, 7 storefront pages, 5 guest pages — all load.
- 13 automated tests passing (`./run.sh test`).
- Redesigned UI, no build step, runs offline.
- Secrets secured, MySQL password rotated, app runs as a limited DB user.

**Honest gaps (all verified against the code, not guessed)**

| # | Gap | Why it matters |
|---|---|---|
| 1 | **56 files uncommitted** | One bad `rm` and days of work are gone. Highest-risk item today. |
| 2 | **Supplier portal is a raw, unstyled page** | Supplier is 1 of your 5 documented roles. A panelist *will* log in as one. |
| 3 | **Customers can't see past orders or track one** | The storefront header links "Track Order" — it goes nowhere. Chapter 2 (Context Diagram) says the Customer receives *order status*. |
| 4 | **Wholesale pricing is never used** | Chapter 1 says *"retail and wholesale business"*. `order_type` (Retail/Wholesale) exists in the model but nothing ever sets it. Every sale is retail. |
| 5 | **No charts** | Your mockups show a sales trend line and inventory value per branch. Real, but cosmetic. |
| 6 | **Bundle Management** | In the mockups, absent from Chapter 1–3 and the ERD. Needs a decision, not code. |

Gaps 2–4 aren't polish — they're **things your own documentation promises**. Fix
those before anything cosmetic.

---

## Phase 0 — Lock it down (this week, ~1 hour)

Nothing else starts until the work is safe.

- [ ] Commit the 56 files in a few reviewable commits.
- [ ] Push to your remote so the whole team has it.
- [ ] Tell your teammate: the React SPA lives on the `frontend-react` branch, and
      their DRF API is kept and is genuinely good work.
- [ ] Everyone else: `git pull`, then `./run.sh` and confirm it runs on their machine.

**Done when:** every member can run the system and `./run.sh test` says 13/13.

---

## Phase 1 — Close the promised gaps ✅ COMPLETE (17 July 2026)

*Aligns with "System Development" on your Gantt.*

| | Task | Outcome |
|---|---|---|
| ✅ | **Supplier portal** | Built. Exposed and fixed **3 data leaks** (a supplier could see all branches' stock, and rival suppliers' POs; a manager could open another branch's PO/billing by URL). |
| ✅ | **Customer order history + Track Order** | Built. The dead header link now works; status tracker in plain language. |
| ✅ | **Wholesale pricing** | Reworked 2026-09 into **quantity-tiered bulk pricing**: each product carries its own ladder of price breaks (buy N or more → pay ₱X per unit), set on Add/Edit Product and importable as three column-pairs. Price comes from the quantity bought on every path — POS, storefront, API. The manual Retail/Wholesale toggle, its Order Console filter and the per-account wholesale flag were removed. |
| ✅ | **Dashboard charts** | Built (inline SVG). Exposed 3 more bugs: seed data rotting, UTC timezone, and a price-loss bug on products over ₱1,000. |
| ✅ | **Bundle Management** | **Out of scope** — team decision. See below. |
| ✅ | **More automated tests** | 13 → **30**, all passing. |

**Done when:** every role has a real screen, every promise in Chapters 1–3 has
working code, and nothing in the UI leads to a dead end.

### Decision: Bundle Management is out of scope

**Decided 17 July 2026 — agreed by the team.**

Bundle Management appears in the UI mockups but **not** in Chapter 1's Scope, the
ERD, the DFD, or the 14 business rules. Building it would mean inventing a
`Bundle` / `BundleItem` schema the approved design never described — and then
defending a feature the documentation doesn't explain.

The mockups are a design exploration. **Chapters 1–3 are the specification.**
Where they disagree, the specification wins.

**So:** the feature is not built, and it is recorded in Chapter 5 as a
recommendation for future work (text ready to paste below).

**Reopen this only if the client actually asked for it.** If Gerlyn requested
bundles, it isn't scope creep — it's a requirement the documentation missed, and
the correct fix is to amend Chapter 3 + the ERD *first*, then build it.

**Chapter 5 — Recommendations (paste-ready):**

> **Product bundling.** The system currently prices and sells products
> individually. During design, a product-bundling feature was explored — allowing
> the store to group items into promotional packs (for example, a rice-and-canned-
> goods combination) sold at a single discounted price. This was excluded from the
> present scope because the study's data model and process analysis were defined
> around individual product transactions, and introducing bundles would require
> extending the entity-relationship model and the sales workflow beyond the
> documented scope. Since the store already promotes informal package deals, future
> researchers may extend the system with a bundling module, together with the
> corresponding reporting on bundle performance.

---

## Phase 2 — System Testing & Refinement (August → early September)

*Matches your Gantt.*

- [ ] **Bug bash.** Each member takes one role for an hour and tries to break it.
      Write down: what you clicked, what you expected, what happened.
- [ ] Run through all 8 tests in `PROJECT-LOG.md` on a **fresh** machine.
- [ ] Test on a real phone, not just a resized browser.
- [ ] **Data integrity check:** does stock ever go negative? Do the reports add up
      to the same totals as the Dashboard?
- [ ] Fix what you find. Add a test for each real bug so it can't come back.
- [ ] Freeze features. **After this phase, no new features** — only fixes.

**Done when:** a week passes with no new bugs found, and the tests still pass.

### Where we actually are with DESIGN.md (6 Aug)

Checked every item in `docs/DESIGN.md` — the 11-item task order, Section 1's
other named pieces (Alerts Center tabs, Sales & Orders vs. Billing, Admin
gating), and Section 2's improvements 1 and 3–6 — against the real files, not
memory. Evidence is file:line so it can be re-checked in ten seconds if it
drifts.

**Done**
- **#1 Rename "Withdrawal Slips" → "Stock Adjustments".** `templates/internal_base.html:67`; zero remaining occurrences of "Withdrawal Slips" anywhere under `templates/`.
- **#2 "View all alerts →" link.** `templates/reports/dashboard.html:166` (branch-status card) and `:212` (restock suggestions card), both → `alerts_center`.
- **#3 Owner Dashboard migrated off Bootstrap.** Zero Bootstrap utility classes (`row`, `col-*`, `fs-*`, `fw-bold`, `stat-card`, `table-sm`) anywhere in `templates/reports/dashboard.html`; built entirely from `.stat-hero`, `.kpi-row`, `.stat-strip`, `.gv-table`, `.kpi-card`, `.card`. (Same item as Section 2 improvement #1.)
- **#4 Staff role nav visibility tightened.** `templates/internal_base.html` gates Products/Categories/Stock Adjustments (lines 61, 66), Procurement (74), Billing (84), System & Monitoring (105, 113), Administration to Owner-only (135), and the bottom mobile nav's Alerts/Reports (222). Matches DESIGN.md's role matrix exactly (Staff: Dashboard + Sales & Orders + Stock per Branch only; Manager: everything but Administration; Owner: everything) — confirmed by rendering the sidebar for both an Owner and a Staff login.
- **#5 Rich KPI cards with sparklines.** `reports/views.py` — `_sparkline_points()` (line 96), `_sales_period_delta_pct()`; `templates/reports/dashboard.html:19-81` — all four hero cards (Total Sales, Recent Sales, Procurement Cost, Total Transactions) have an inline SVG sparkline with end-dot plus a delta callout.
- **#6 Click-to-expand (2a).** `reports/views.py` — `_kpi_graph_series()` (162), `_delta_display()` (198); `templates/reports/dashboard.html:85-104` (shared graph panel) and its `activate()` JS (318) — one shared panel, cards act as tabs, keyboard-accessible, exactly one `.active` at a time. Verified end-to-end earlier this session.
- **Section 2 #3, status color rule.** `.btn-primary` uses `var(--action)` (green) exclusively — `static/css/gerlyn.css:100-101`. No amber/warn-colored action-button class exists anywhere.
- **Alerts Center's two tabs.** Low Stock Alerts and Expiry Alerts both present — `templates/inventory/alerts_center.html:29-140`. The Low Stock tab has exactly the Qty/Reorder/Suggested/Risk columns plus per-row "Create PO" DESIGN.md specifies (lines 39-87).
- **Sales & Orders vs. Billing distinction.** Genuinely separate, matching the workflow-vs-ledger split: Sales & Orders drives `order_status` transitions (`sales/views.py`, `transaction_set_status`); Billing records are created automatically (`billing/signals.py:15`) and only change via `billing_confirm` (`billing/views.py:139`) — no manual-edit path found.
- **Administration gating (Owner only).** `templates/internal_base.html:135`.

**Partially done**
- **Dashboard's "Sales by Branch."** Exists, but as a row of per-branch `.kpi-card`s (today's sales, trend, share %, low-stock count) rather than a literal `.gv-table` — `reports/views.py:584` (`_branch_status_rows`), `templates/reports/dashboard.html:127-170`. Functionally equivalent to what Section 1 asks for; flagging only because the doc says "table."
- **#8 / 2e Standardized date-range control.** A raw two-date-input `.date-range` pair exists, but only in 2 of the 4 Reports tabs (Billing Report, Procurement Report — `static/css/gerlyn.css:727-729`); Sales Report, Inventory Report, and Alerts Center have no date filter at all. Not the preset-based component (Today / Last 7 days / Last 30 days / This month / Custom) DESIGN.md specifies.
- **Billing's "Outstanding Balances" view.** An aggregate total is shown (`billing/views.py:45`, `templates/billing/billing_list.html:9`) and there's a "Pending" status tab, but no view scoped specifically to wholesale/credit customers as DESIGN.md's Financials section calls for.

**Not started**
- **#7 / 2b "Needs attention" list.** No formalized `.card` component linking out to Alerts/POs/Orders/Billing anywhere on the Dashboard.
- **#9 / Section 2 #6 Breadcrumbs (internal system).** None found under the internal topbar. There *is* an unrelated `.breadcrumb` component, but it's on the customer-facing Settings pages (`templates/accounts/profile.html:15`, extends `public_base.html`) — a different, already-shipped feature for a different area, not what DESIGN.md asks for here.
- **#11 / 2c+2d Reports multi-panel row and Sales & Orders funnel.** No donut/funnel markup anywhere. DESIGN.md itself flags both as lower-priority/optional.
- **Dashboard's "Top-Selling Products" list.** Named in Section 1's Dashboard bullet list; not found anywhere in `reports/views.py` or `templates/reports/dashboard.html`.

**Not applicable / deliberately deferred**
- **#10 Fold Categories into Products.** DESIGN.md itself says "when team is ready" — Categories is still its own nav item (`templates/internal_base.html:63`). Correctly deferred, not a gap.
- **Section 2 #4, Typography (Inter).** DESIGN.md: "not a priority — only change if actively wanted." Still `system-ui`. No action needed.
- **Section 2 #5, sidebar flyout-on-hover.** DESIGN.md: "not a v1 requirement." The team deliberately built a different pattern instead — a collapsed group expands the sidebar and opens just that group, rather than a hover flyout — documented in code comments at `static/js/sidebar.js:37-40` and `static/css/gerlyn.css:410`.

**Recommended next session** (priority order, weighing both DESIGN.md's own order and this roadmap's Phase 2 priorities)
1. **Bug bash** — Phase 2's own first task, and it's already next in this roadmap. Do this before more feature work, per the "freeze features after this phase" rule below.
2. **Style the Supplier portal** — this roadmap's gap #2 (still open; today's session only touched it for a URL/crash fix, not its styling). It's a documented promise (Chapter 3 names Supplier as 1 of 5 roles), so it outranks any remaining DESIGN.md polish item.
3. **#7 Needs-attention list (2b)** — cheapest of the three remaining task-order items and the one most directly tied to the Dashboard's actual purpose; reuses existing `.card` and alert-data patterns.
4. **#8 date-range control, then #9 breadcrumbs** — real but lower-value polish; DESIGN.md itself ranks breadcrumbs last, and the date-range control already has partial ad hoc coverage.
5. **Decide explicitly whether #7–#9 happen before or after this Phase 2's feature freeze** — don't let that default happen silently; the bug-bash checklist below says no new features once it's done.

### Found during the sidebar-redesign merge (6 Aug)

- [x] `accounts/views.py` — `verify_email`, invalid-token branch. Missing `return`
      after a `redirect(...)` (same class of bug as the `user_list` fix from this
      merge, but this one could raise `UnboundLocalError` since a variable used
      later in that branch is never assigned). Not exercised by current tests, so
      it slipped through review — fixed independently by a teammate the same day
      (449cf63) and merged in.
- [x] `accounts/views.py` — two more bare `redirect(...)` missing `return`, lower
      severity than `verify_email` (don't crash, just don't stop execution as
      intended): duplicate-username check in `user_form` (didn't stop account
      creation from proceeding) and post-save in `user_form` (skipped the
      redirect back to the list). Also fixed in 449cf63.
- [ ] Seeded Staff users have no branch assigned (`piladmin01`, `storestaffmisty`,
      `mistystaff01`). Harmless today, but it means Staff Dashboard's "pending
      deliveries" list is silently empty for every real Staff login — the
      branch-scoped query never matches. Assign real branches once real branch
      data exists.

### Placeholder/demo data currently in the dev DB

Checked directly against the database, not assumed. The dev DB right now has
exactly **1 branch** ("Main Branch") and **3 products** (`DEMO-001` Rice Sack,
`DEMO-002` Cooking Oil, `DEMO-003` Canned Sardines) — none of it matches the
tracked `accounts/management/commands/seed_demo.py` command's data (real
Baliuag branches: Tarcan, Makinabang, Tiaong Baliwag Mini Mart; a `SKU-011`-style
product catalog), so that command has apparently never been run against this
DB. On top of that generic base sits 30 `ORD-DEMO-*` SalesTransaction rows
from the untracked `seed_dashboard_demo.py` script at the repo root — a
one-off tool written this session to eyeball dashboard sparklines against
non-empty data (its own docstring has the cleanup queries).

- [ ] Before deploying, run `seed_demo --reset` (already in Phase 3's
      deployment checklist below) on the deploy target — `_reset()` wipes
      Branch/Product/etc. unconditionally, so this *does* clear "Main Branch"
      and the `DEMO-00X` products, replacing them with the real-branch-named
      catalog. Don't skip straight to production without it, and don't copy
      this local dev DB's current state over as if it were ready to ship.
- [ ] Decide what to do with `seed_dashboard_demo.py` before it's committed —
      delete it (cleanup queries are in its own header comment) or move it
      somewhere clearly marked as a dev-only scratch tool, so it can't get
      mistaken for part of the real seed pipeline or accidentally run
      against a live database.
- [ ] Worth a team check: is `seed_demo.py`'s Tarcan/Makinabang/Tiaong Baliwag
      Mini Mart catalog the store's actual real branch/product/pricing data,
      or is it also a nicer-looking placeholder meant only to carry the
      system through the evaluation phase? This roadmap's Phase 3 treats
      running it as the final pre-launch data step, so it's worth confirming
      that's actually the intent before evaluators log in.

---

## Phase 3 — Deploy (early September) ← the gate

The system must be reachable by evaluators before Phase 4 can start.

### Hosting research (done — and the answer changed recently)

| Option | MySQL? | Cost | Verdict |
|---|---|---|---|
| **Railway** | ✅ one-click | **$5/mo** Hobby (free trial = $5 credit, 30 days only) | **Recommended.** Push to GitHub → live URL. Auto-detects Django. |
| **PythonAnywhere** | ✅ but **paid tier only now** | **$5/mo** Hacker | Easiest for beginners, no Docker. Good backup choice. |
| Render | ❌ Postgres only | Free tier | Free tier also **sleeps after 15 min** — bad for a defense. |
| Local LAN server | ✅ | Free | Contradicts your Chapter 3 "centralized web-based platform across branches". |

> ⚠️ **Important, recently changed:** PythonAnywhere moved **MySQL to a paid tier for
> new accounts from January 2026**. Old tutorials and blog posts still say the free
> plan includes MySQL — **they're out of date.** Don't plan around it.

**Cost reality:** ~$5/month ≈ ₱280. For Sept–Nov that's about **₱850 total, or
₱140 each** across 6 members. Don't burn a week trying to avoid it.

### Deployment checklist

**The code is already prepared and tested** — see **[docs/deployment.md](docs/deployment.md)**
for the step-by-step. gunicorn, WhiteNoise, `collectstatic`, the media route,
the security settings, `Procfile` and `railway.json` are all done and were
verified by running the app in production mode locally. Django's
`check --deploy` reports **0 warnings**.

What's left is the account work:
- [ ] Create the Railway project + add MySQL (one click).
- [ ] Set the env vars (table in the deployment doc). **Don't forget
      `DJANGO_CSRF_TRUSTED`** — without it, login itself fails.
- [ ] **Mount a volume at `/app/media`** — Railway's filesystem is ephemeral, so
      uploaded payment proofs vanish on every redeploy without it.
- [ ] Run `seed_demo --reset` on the server.
- [ ] **Test the live URL from a phone on mobile data** — not your wifi.
- [ ] Write the URL + logins into `README.md`.

**Done when:** a panelist can open the URL on their own phone and log in.

> Keep `./run.sh` working locally too. If the internet dies during your defense,
> you demo from a laptop and carry on. **Never depend on one path.**

---

## Phase 4 — User Evaluation & Data Analysis (September → October)

*Matches your Gantt. Everything you need is already written in
`docs/evaluation-pack.md`.*

- [ ] **3 IT experts** — ISO 25010 questionnaire (already drafted in the pack).
- [ ] **Store owner + staff from different branches** — the user-acceptance
      questionnaire (also drafted).
- [ ] Use the **12-minute demo script** in the pack so every session is consistent.
- [ ] Let them **touch it themselves** — watching where they get stuck is your real
      Usability data.
- [ ] Tally the results: mean per ISO characteristic, plus the written comments.
- [ ] Make small fixes from the feedback — and **record that you did**. "We changed
      X because an evaluator said Y" is exactly what Agile promises in Chapter 3.

**Done when:** you have scored questionnaires from 3 experts + the owner/staff, and
a table of means ready to paste into Chapter 4.

---

## Phase 5 — Chapters 4 & 5 (October → November)

*Matches your Gantt.*

**Chapter 4 — Results**
- [ ] System screenshots (take them from the **deployed** system).
- [ ] ISO 25010 results table + interpretation.
- [ ] BPR results: the **As-Is vs To-Be table** is already written in the pack.
- [ ] Evidence of process improvement — e.g. *"consolidating branch reports went from
      manual collation to instant"*. Quote the owner.

**Chapter 5 — Conclusion & Recommendations**
- [ ] Conclusions against each Specific Objective in Chapter 1.
- [ ] **Be honest about limitations** — panelists respect this and always probe it:
      payment is recorded not processed, no courier integration, no HR, doesn't
      replace the POS hardware.
- [ ] Recommendations = your known gaps (Bundle Management, server-generated PDFs,
      wider test coverage, barcode scanning).

- [ ] Update the ERD/DFD diagrams if the code drifted from them. **Check this** — the
      diagrams must match what you built, or a panelist will catch it.

---

## Phase 6 — Final Defense (November)

- [ ] Rehearse the 12-minute demo **at least 3 times**, timed.
- [ ] Seed fresh data right before (`./run.sh fresh`) so numbers match your slides.
- [ ] **Have a backup:** the live URL, a local laptop copy, *and* a screen recording.
- [ ] Prepare for the questions you know are coming:
  - *"How do you stop one branch seeing another's data?"* → open `core/scoping.py`,
    show the server-side filter, then log in as Carlo.
  - *"How do you prevent selling expired stock?"* → batches + FEFO + Expiry Alerts.
  - *"How do you know it works?"* → run `./run.sh test` live. 13/13 in 5 seconds.
    Very few undergrad projects can do this. **Use it.**
  - *"Why not React?"* → the comparison table in `PROJECT-LOG.md`. Evidence, not taste.
  - *"What's not done?"* → Bundle Management, and why (it's not in the ERD).

---

## Timeline at a glance

| When | Phase | Must finish |
|---|---|---|
| **This week** | Phase 0 — Lock down | Everything committed + pushed |
| **Late Jul → Aug** | Phase 1 — Close gaps | Supplier portal, order tracking, wholesale |
| **Aug → early Sep** | Phase 2 — Testing | Feature freeze |
| **Early Sep** | **Phase 3 — Deploy** | **Live URL (gate for evaluation)** |
| **Sep → Oct** | Phase 4 — Evaluation | Questionnaires from 3 experts + users |
| **Oct → Nov** | Phase 5 — Chapters 4–5 | Documentation |
| **Nov** | Phase 6 — Defense | Rehearsed demo + backups |

---

## Risks, ranked

| Risk | Likelihood | What to do |
|---|---|---|
| **Work lost / not committed** | High today | Phase 0. Do it this week. |
| **Deployment slips → evaluation slips** | **High** — it's not even on your Gantt | Deploy early Sept. Budget the $5/mo now. |
| Evaluators are hard to schedule | High | Book the 3 IT experts **in August**, not September. Academics have no free time in October. |
| Diagrams don't match the built system | Medium | Check the ERD/DFD in Phase 5. Panelists compare them. |
| Feature creep | Medium | Freeze after Phase 2. New ideas → Chapter 5 "Recommendations". |
| Live demo fails on the day | Medium | Three fallbacks: URL, laptop, recording. |

---

## What I'd do first, today

1. **Commit and push.** Nothing else matters if the work isn't safe.
2. **Decide Bundle Management** — it's a 30-second decision blocking a checkbox.
3. **Book your 3 IT evaluators for September, now**, while their calendars are open.

Then Phase 1: the Supplier portal, because it's an entire documented role that
currently opens to an unstyled page.

---

## Sources for the hosting research

- [Railway Pricing Plans](https://docs.railway.com/pricing/plans) · [Railway Free Trial](https://docs.railway.com/pricing/free-trial)
- [PythonAnywhere Free Account Features](https://help.pythonanywhere.com/pages/FreeAccountsFeatures/) · [Changes to Free Accounts](https://blog.pythonanywhere.com/221/) — the Jan 2026 MySQL change
- [Django Hosting & Deployment Options — LearnDjango](https://learndjango.com/tutorials/django-hosting-deployment-options)
- [Best Free Django Hosting in 2026](https://appliku.com/post/free-django-hosting/)
