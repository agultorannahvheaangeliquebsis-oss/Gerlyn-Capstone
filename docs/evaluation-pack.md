# GerlynPLUS — Evaluation Pack

For the ISO/IEC 25010 evaluation (3 IT experts) and the user-acceptance
evaluation (store owner + selected staff), as specified in Chapter 3.

---

## 1. Demo walkthrough script (~12 minutes)

Reset first so the numbers match this script:

```bash
python manage.py seed_demo --reset && python manage.py runserver
```

### A. The problem being solved (1 min)
Open the **storefront** (`/`) and the **internal system** side by side. State the
As-Is problem: each branch kept its own records, so the Owner had no consolidated view.

### B. Centralized visibility — Owner (2 min)
Log in as **`gerlyn` / `demo1234`** → lands on the **Dashboard**.
- One hero metric (today's sales) + a quiet strip of secondary stats.
- **Restock Suggestions** — 6 items below reorder level, with suggested quantities.
- Sales by Branch consolidates all 3 branches — *this is the view that did not exist before.*

### C. Information governance — branch scoping (2 min)
Log in as **`carlo` / `demo1234`** (Tarcan manager) in a private window.
- Same Dashboard, but only Tarcan data.
- Order Console: Carlo sees 5 orders; the Owner sees 10.
- Point out: this is enforced **server-side**, not by hiding UI.

### D. Sales loop — walk-in POS (2 min)
As **`ana`** (Tarcan staff) → **Sales & Orders → New Walk-in Sale**.
- Add an item; the total updates live. Record the sale.
- Show: stock dropped, a billing record was auto-created and marked Paid,
  and the sale is attributed to `ana` for the audit trail.

### E. Online order loop (2 min)
As **`juan`** (customer) on the storefront: choose a branch → add to cart →
note the **free-delivery meter** → checkout with Cash on Delivery.
- Switch to `ana`: the order is **Pending** in the Order Console.
- Approve → Preparing → **Completed** → *only now* does stock leave the shelf.

### F. Procurement loop (2 min)
As **`gerlyn`**: **Alerts** → a critical item → **Create PO**.
Then **Deliveries** → receive an existing "Ordered" PO.
- Enter a quantity **lower** than ordered → the PO is flagged **Discrepancy** and only
  the delivered quantity is added. This is business rule 13 in action.

### G. Reporting + governance (1 min)
**Reports → Sales Report → Export CSV** (a real, branch-scoped file) and
**Export PDF** (print dialog → Save as PDF).
**Administration → User Management**: show role + branch assignment, the per-module
View/Add/Edit/Delete matrix, and deactivation. Disable View for one module and show
that its direct page/API write URLs are rejected as well as disappearing from navigation.

---

## 2. ISO/IEC 25010 — self-assessment

Evidence the evaluators can verify during the walkthrough.

| Characteristic | Evidence in the system | Self-rating |
|---|---|---|
| **Functional suitability** | All 5 documented processes implemented end-to-end; the 5 acceptance scenarios pass | Strong |
| **Performance efficiency** | Querysets use `select_related`/`prefetch_related`; alerts computed by one shared engine; dataset is SME-scale | Adequate |
| **Compatibility** | Runs in any modern browser; no CDN, no plugins; one MySQL database shared by all branches | Strong |
| **Usability** | Redesigned UI: one hero metric, 16px minimum text, 44px targets, status pills with dot + full label (colour-blind safe), empty states that teach, mobile responsive | Strong |
| **Reliability** | Stock changes are atomic (`transaction.atomic`, `select_for_update`); illegal state transitions rejected; double-receive blocked | Strong |
| **Security** | RBAC on every view; server-side branch scoping; secrets in `.env`; refuses to boot with `DEBUG=False` + default key; CSRF on every form | Adequate — see gaps |
| **Maintainability** | Shared `core/permissions.py` + `core/scoping.py`; one alerts engine; one design system; one seed command | Strong |
| **Portability** | Standard Django + MySQL; `.env` config; runs on any OS with Python 3.12; fully offline | Strong |

### Automated verification
The five acceptance scenarios are covered by an automated test suite:

```bash
python manage.py test core        # 13 tests, all passing
```

This is the strongest single piece of evidence for Functional Suitability and
Reliability — the evaluators can run it themselves in under 5 seconds.

### Known gaps to disclose honestly
- The original MySQL password exists in **git history** and must be rotated.
- PDF export uses the browser's print-to-PDF rather than a server-generated file
  (a deliberate trade-off to avoid an extra dependency and keep the system offline-capable).
- **Bundle Management** is **deliberately out of scope** (team decision, 17 July
  2026) — it appears in the UI mockups but not in the Chapter 1–3 scope or the ERD,
  so no schema was invented for it. It is documented in Chapter 5 as future work.
  Present this as a scope decision, not an omission.
- Test coverage targets the acceptance scenarios and RBAC, not every view.

---

## 3. Acceptance scenarios (all currently passing)

| # | Scenario | Expected result |
|---|---|---|
| 1 | Staff records a walk-in sale | Stock drops · billing auto-created and Paid · attributed to that staff member |
| 2 | Customer orders online → staff confirms the delivery or pickup | Order Pending → Completed · stock deducts **once**, only on that confirmation · a receipt number is issued |
| 3 | Low-stock alert → PO → receive delivery | Stock rises by the **delivered** quantity · discrepancy flagged if short · audit logged |
| 4 | Withdrawal slip released → destination confirms / flags | Confirm sets Acknowledged; Flag corrects stock + writes an Adjustment movement |
| 5 | Governance | Tarcan manager cannot see Makinabang data or the consolidated report; staff cannot manage users; every action is attributable |

---

## 4. Evaluator questionnaire (ISO 25010 — 5-point Likert)

For each item: **1 = Strongly disagree … 5 = Strongly agree**

**Functional suitability**
1. The system covers the sales, inventory, procurement, billing, and reporting processes.
2. Each function produces correct and complete results.

**Performance efficiency**
3. Pages and reports load within an acceptable time.

**Compatibility**
4. The system works on the browsers/devices the store actually uses.

**Usability**
5. The interface is easy to understand without training.
6. Labels, statuses, and figures are readable and unambiguous.
7. Errors are easy to recognise and recover from.

**Reliability**
8. The system behaves consistently and does not lose or duplicate data.

**Security**
9. Users can only access information appropriate to their role and branch.
10. Transactions are traceable to the person who performed them.

**Maintainability**
11. The system appears organised and modifiable.

**Portability**
12. The system can be installed and run in the store's environment.

*Open items:* What worked well? What would you change before deployment?

---

## 5. User-acceptance questionnaire (owner + staff)

1. Is this faster than the current manual process?
2. Can you find what you need without asking for help?
3. Does the branch information match what you see on the shelf?
4. Do the reports answer the questions you actually have?
5. Would you use this daily? What is missing?

---

## 6. BPR framework — As-Is vs To-Be

| Manual step (As-Is) | System-supported path (To-Be) |
|---|---|
| Staff checks stock in a logbook / physical count | Stock per Branch shows live quantities with status |
| Verbal coordination when stock runs out | Alerts Center flags it automatically with a suggested quantity |
| Manager writes a restock request, Owner approves by phone/chat | Purchase Order raised in-system, statuses tracked |
| Deliveries recorded on paper | Deliveries screen records actual quantities and updates stock |
| Sales written down, consolidated manually by branch managers | Every sale recorded once, consolidated instantly |
| Reports compiled by hand, delayed | Dashboard + reports generated on demand, exportable |
| No audit trail | Every stock movement records who, when, before → after, and why |
