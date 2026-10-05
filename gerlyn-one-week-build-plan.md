# GerlynPLUS — One-Week Build & Completion Plan

> **STATUS: Days 1–7 complete.** All five core processes, both the internal system and
> the customer storefront, the redesigned UI, RBAC + branch scoping, the audit trail,
> CSV/PDF export, and Administration are built and verified.
> `python manage.py test core` → **13/13 passing** (the 5 acceptance scenarios).
> See [README.md](README.md) to run it and [docs/evaluation-pack.md](docs/evaluation-pack.md)
> for the demo script + ISO 25010 pack.
>
> **Deferred by decision:** Bundle Management (in the mockups, but absent from the
> Chapter 1–3 scope and ERD — no schema was invented for it).
> **Action required:** rotate the MySQL password — the original is in git history.

**System:** GerlynPLUS (Process-Linked Unified Systems Enterprise) — integrated IS for Gerlyn Variety Store (multi-branch retail, Baliuag, Bulacan)
**Stack (locked, per Chapter 3):** Django 6 + Django REST Framework · MySQL · HTML/CSS/JS templates · custom `accounts.User` (RBAC)
**Method:** Agile/SDLC iterations · Kanban (Jira) · evaluated vs **ISO 25010** + **BPR** framework
**Goal of the week:** Take the existing ~60–70% build to a demo-ready, evaluation-ready system covering all 5 core processes and both the internal system + customer storefront.

---

## 0. Where the build stands today (grounded assessment)

| App | State | What's left |
|-----|-------|-------------|
| `accounts` | User model + role dashboards + login/register ✔ | RBAC enforcement everywhere, audit fields, user CRUD (Admin) |
| `inventory` | Most complete (products, categories, stock/branch, withdrawal slips) ✔ | Reorder/alert logic, withdrawal confirm/flag actions, expiry batches |
| `procurement` | Suppliers + PO + Deliveries coded ✔ | Receive-delivery → inventory update wiring, PO status flow |
| `sales` | Transactions coded ✔ | POS finalize, link to billing, online/physical channel |
| `billing` | Coded ✔ | Payment-record (upload ref / mark Paid), tie to orders |
| `reports` | Views exist, models thin | CSV/PDF export, consolidated multi-branch, dashboard KPIs |
| `branches` | Thin (models 13 lines) | Branch CRUD (Admin), branch scoping helper |
| `orders` | **Empty models/views** — templates only (catalog, cart, checkout) | **Customer storefront end-to-end** — biggest gap |
| `core` | Minimal | Shared mixins: RBAC, branch scope, audit trail |

**Bottom line:** the heavy inventory/procurement work is largely done. The week's risk concentrates on (a) the customer `orders` flow, (b) reports/exports, (c) RBAC + audit consistency, and (d) polish + evaluation.

---

## 1. Scope lock (build exactly this — no more, no less)

### IN — 5 core processes (from the DFD) + supporting modules
1. **Sales & Orders** — Walk-in POS, Order Console (approve/prepare/complete/cancel), online + in-store channels
2. **Billing** — invoice/receipt records, payment method + status (record-only), outstanding balances
3. **Inventory** — Product Master List, Categories, Stock per Branch (qty on hand, reorder level, expiry batches), Withdrawal Slips (release/transfer/write-off, confirm receipt / flag discrepancy)
4. **Procurement** — Suppliers, Purchase Orders (Draft→Ordered→Received/Cancelled), Deliveries (receive → auto-update inventory)
5. **Reports** — Sales / Inventory / Procurement / Billing, per-branch + consolidated, CSV + PDF export
- **Alerts** — low/critical stock + expiring soon (computed from inventory)
- **Administration** — Branch Mgmt, User Mgmt (RBAC), Bundle Mgmt
- **Customer storefront** — browse, cart, checkout, order tracking

### OUT — hard limitations (from Chapter 1; do NOT build)
- ❌ No online payment gateway — record payment only (upload GCash reference # / Admin marks "Paid")
- ❌ No courier/logistics/shipment tracking integration
- ❌ No HR/payroll/attendance (staff IDs are for audit trail only)
- ❌ No external accounting/supplier/third-party system integration
- ❌ Does not replace existing POS hardware — this is the back-end IS layer

### Roles & permission matrix (RBAC — enforce on every view + API)

| Capability | Owner/Admin | Branch Manager | Staff | Supplier | Customer |
|---|:--:|:--:|:--:|:--:|:--:|
| All-branch consolidated reports | ✅ | — | — | — | — |
| Approve procurement | ✅ | request | — | — | — |
| Branch reports & monitoring | ✅ | ✅ (own branch) | — | — | — |
| Product master / pricing | ✅ | view | view | — | — |
| Process sales / POS | ✅ | ✅ | ✅ (own branch) | — | — |
| Update inventory / withdrawal | ✅ | ✅ | ✅ (own branch) | — | — |
| Receive deliveries | ✅ | ✅ | ✅ | — | — |
| Manage branches / users | ✅ | — | — | — | — |
| Receive PO / confirm delivery | ✅ | ✅ | — | ✅ (own POs) | — |
| Place order / track order | — | — | — | — | ✅ |

**Rule:** Manager/Staff are **branch-scoped** (see only their assigned branch); Owner sees All Branches. Every write records `created_by`/`updated_by` + timestamp (digital audit trail — a documented objective).

---

## 2. Team split (6 members → 4 workstreams)

| Stream | Members | Owns |
|---|---|---|
| **A. Inventory & Procurement** | 2 | inventory finish, withdrawal actions, alerts, suppliers, PO, deliveries→stock |
| **B. Sales, Orders & Billing** | 2 | POS, Order Console, billing/payment-record, customer storefront (`orders` app) |
| **C. Reports, Dashboard & Admin** | 1 | KPIs, CSV/PDF export, branch/user/bundle mgmt |
| **D. Platform & QA** | 1 (rotating) | RBAC mixin, branch-scope, audit trail, demo records, migrations, testing, deploy, ISO 25010 pack |

Daily 15-min standup, Jira Kanban (To Do → In Progress → Testing/Review → Done). Feature branches + PRs; no direct pushes to `main`.

---

## 3. The 7-day plan

### Day 1 — Foundation, data model, and RBAC skeleton (everyone)
- [ ] Freeze the ERD in code: confirm models for Owner/Branch/Manager/Staff/Inventory/Product(SKU, unit price)/SalesTransaction(Online|Physical)/SalesItem/Procurement/ProcurementItem/Supplier match the 14 business rules.
- [ ] Run/repair all migrations against MySQL; one clean `migrate` on every machine.
- [ ] **`core` app: build shared mixins** — `RoleRequiredMixin`, `BranchScopedQuerysetMixin`, `AuditModel` (created_by/updated_by/timestamps). This unblocks everyone.
- [ ] **Seed script** (`manage.py seed_demo`): 3 branches (Tarcan, Makinabang, Tiaong Baliwag Mini Mart), 5 users (Owner, 2 Managers, 2 Staff), 15 products + categories, per-branch stock + reorder levels + expiry batches, 8 suppliers, sample POs, 10 orders. → matches the screenshots exactly for a believable demo.
- [ ] CI-lite: everyone can log in as each role and land on the right dashboard.
- **DoD:** clean DB from one command; each role logs in; branch scoping proven on one list view.

### Day 2 — Inventory complete (Stream A) + storefront models (Stream B)
- Stream A:
  - [ ] Product Master List CRUD + Categories CRUD (availability per branch).
  - [ ] Stock per Branch: qty on hand, reorder level, expiry batches, status (In/Low/Critical) computed.
  - [ ] Withdrawal Slips: create (branch restock / sample-display / write-off), **Confirm Receipt** and **Flag Discrepancy** actions update stock + audit.
  - [ ] Alerts engine: low/critical (qty vs reorder) + expiring-in-14-days queries.
- Stream B:
  - [ ] **Build the missing `orders` models/views** — Cart, Order, OrderItem (or reuse SalesTransaction with channel=Online), status enum Pending→Approved→Preparing→Completed/Cancelled.
- **DoD:** editing stock reflects on Stock-per-Branch + Alerts instantly; withdrawal confirm changes qty.

### Day 3 — Sales, POS, Order Console, Billing (Stream B) + Procurement (Stream A)
- Stream B:
  - [ ] Walk-in POS: add items → totals → finalize → deduct stock → create SalesTransaction(Physical) + Billing record.
  - [ ] Order Console: filter tabs (All/Pending/Approved/Preparing/Completed/Cancelled), status transitions, per-branch scope.
  - [ ] Billing: mark Paid / record payment method (Cash, COD, Pay-at-Store, GCash ref upload). Outstanding balance list.
- Stream A:
  - [ ] Suppliers CRUD; PO create (Draft→Ordered), overdue flag on expected-delivery.
  - [ ] Deliveries: Receive against PO → **auto-update inventory** (business rule #13: only after receipt validation) + mark PO Received.
- **DoD:** a POS sale deducts stock and creates a billing record; receiving a PO raises stock.

### Day 4 — Customer storefront end-to-end (Stream B) + Alerts UI (Stream A) + Reports start (Stream C)
- Stream B:
  - [ ] Storefront: home (hero, categories, flash deals, popular), product list/detail, cart, checkout (branch select, COD / Pay-at-Store / GCash-reference), order confirmation.
  - [ ] **Order → Order Console** link: a customer online order appears in staff Order Console as Pending; staff approves → prepares → completes; stock deducts on completion.
  - [ ] Track Order (public, by order ID).
- Stream A: Alerts Center UI (low-stock + expiry tabs, suggested reorder qty, create-PO shortcut).
- Stream C: Reports data layer — sales per branch, top products, inventory value, procurement summary.
- **DoD:** a guest places an online order → it shows in Order Console → completing it moves stock. Full loop works.

### Day 5 — Reports, Dashboard, Admin, Bundles (Stream C + spare hands)
- [ ] Dashboard: KPI tiles (products, low/critical, inventory value, today's/month sales, pending online orders, pending GCash), Restock Suggestions with "Create PO", sales trend + inventory-value charts, top products, recent orders.
- [ ] Reports: Sales / Inventory / Procurement / Billing tabs; branch + month filters; **Export CSV + Export PDF**.
- [ ] Administration: Branch Mgmt CRUD, User Mgmt CRUD (assign role + branch, activate/deactivate, reset password), Bundle Mgmt (create bundle, components, bundle price, active toggle).
- **DoD:** Owner sees consolidated numbers; Manager sees own branch only; CSV/PDF download works.

### Day 6 — RBAC hardening, audit trail, UI polish (all streams)
- [ ] Sweep **every** view + DRF endpoint for role + branch-scope enforcement (deny by default). Add a permissions test per role.
- [ ] Confirm audit fields populate on all writes; add a simple activity log where the doc expects traceability (sales, procurement receipts, withdrawals).
- [ ] Apply the **UI redesign brief** quick wins: fix truncation, right-align numbers, consistent ₱/date format, status pills, table spacing, one clean storefront hero, mobile drawer nav. *(see `gerlyn-ui-redesign-brief.md`)*
- [ ] Empty states, form validation messages, loading/disabled states.
- **DoD:** no cross-branch data leak; no unauthorized action succeeds; UI reads clean on desktop + phone.

### Day 7 — Test, seed demo, evaluate, deploy, document
- [ ] **Bug bash**: run the 5 end-to-end BPR scenarios below with freshly loaded demo records.
- [ ] **ISO 25010 dry-run**: self-score Functional Suitability, Usability, Reliability, Performance, Security, Maintainability, Portability; fix quick blockers; prep the evaluator questionnaire + walkthrough script.
- [ ] Deploy to a staging host (or LAN server) reachable by browsers on all branches; verify MySQL centralization (a change in one "branch" browser shows in another).
- [ ] Freeze demo data; capture updated screenshots; write a 1-page "how to run" + role credentials sheet for Chapter 4.
- **DoD:** system runs from a clean clone in <10 steps; all 5 scenarios pass; evaluation pack ready.

---

## 4. End-to-end acceptance scenarios (must all pass on Day 7)

1. **Sales loop:** Staff POS sale → stock drops → billing "Paid" → appears in Sales Report + Dashboard today's-sales.
2. **Online order loop:** Customer orders online (GCash ref) → Order Console Pending → Owner/Admin verifies payment → Approve→Prepare→Complete → stock drops → billing recorded.
3. **Procurement loop:** Low-stock alert → Create PO → Ordered → Receive Delivery → stock rises → Procurement Report updated.
4. **Withdrawal/transfer:** Release stock Branch A→B → receiving branch Confirms Receipt (or Flags Discrepancy) → both branches' stock correct + audit trail.
5. **Governance:** Manager of Tarcan cannot see Makinabang data or all-branch consolidated report; Staff cannot manage users; every action is attributable.

---

## 5. Risk register (from Chapter 3's own risk assessment)

| Risk | Likelihood | Mitigation |
|---|---|---|
| Multi-branch integration slips the timeline | High | Branch-scope mixin on Day 1; scenarios freeze scope; cut Bundles/PDF-styling first if needed |
| `orders` storefront larger than expected | Med | Reuse SalesTransaction(channel=Online) instead of new models; Stream B protected time Days 2–4 |
| MySQL/env setup differences across 6 machines | Med | One seed command + documented `.env`; Day 1 "green build for everyone" gate |
| Staff transition manual→digital | Low (build) | Simple UI (redesign brief), empty states, and a role credentials + walkthrough sheet |
| Scope creep (payment gateway, courier) | Med | Locked OUT list in §1 — reference it in every review |

**If time runs short, cut in this order (non-core first):** PDF styling → Bundle Mgmt → Flash-deals countdown → charts polish. **Never cut:** RBAC, audit trail, the 5 loops, consolidated reports.

---

## 6. Final deliverables checklist (for Chapter 4 evaluation)
- [ ] Running system (all 5 processes + storefront) on centralized MySQL
- [ ] 5 roles with enforced RBAC + digital audit trail
- [ ] Seed/demo data matching the documented branches & catalog
- [ ] CSV + PDF report exports
- [ ] Updated screenshots + short demo script
- [ ] Role credentials sheet + "how to run" (<10 steps)
- [ ] ISO 25010 self-evaluation + evaluator questionnaire
- [ ] As-Is vs To-Be confirmed: every manual hand-off in the As-Is model now has a system-supported path
