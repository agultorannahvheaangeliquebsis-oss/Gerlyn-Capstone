# 12 — Suggested Answers to Likely Panel Questions

Short, speakable answers (about 20–40 seconds each). Facts were checked against
the code and the docs in this folder. For file-level evidence and the hostile
questions, see [`06-panel-questions-and-answers.md`](06-panel-questions-and-answers.md).

**Items marked `[FILL IN]`** are things the code cannot tell us (interviews, UAT
results, who did what). Replace them with your real answers before the defense.

---

## 1. Problem, objectives, and scope

**1. What problem does GerlynPLUS solve, and what did the business do before?**
> GERLYN is a multi-branch retail business in Baliuag, Bulacan. Before the system,
> records were kept separately per branch, so the owner had no single, current view
> of sales, stock, payments, and supplier deliveries. GerlynPLUS centralizes the five
> core processes we found in the process analysis: sales and orders, billing,
> inventory, procurement, and reporting.

**2. Who are the users and what can each do?**
> There are five roles. The **Owner** sees all branches and has full control.
> **Branch Managers** and **Staff** are restricted to their own branch; managers
> approve and decide, staff operate the POS and fulfil orders. **Suppliers** work
> only on purchase orders addressed to them. **Customers** use the storefront to
> browse, order, and track their own orders. Access is enforced on the server by role,
> by branch, and by per-account module grants.

**3. Which objectives did you fully meet, and which did you not?**
> `[FILL IN against your Chapter 1 objectives.]` Be honest about the gaps: there is
> no payment gateway (payments are recorded and verified, not processed), order
> notifications are in-app only, and Filipino language support is partial.

**4. What is out of scope, and why?**
> Payment processing is out of scope and documented as a limitation. Integrating a
> gateway needs merchant accounts and regulatory steps outside a capstone. Courier
> dispatch and routing are also out of scope: we record that an order is for delivery
> and its address, but we don't assign couriers or charge delivery fees.

**5. How is this different from an off-the-shelf POS?**
> Three things. It is built around this business's process: a single order table
> serves both walk-in and online orders, branch-level isolation, and supplier
> negotiation of price and delivery date. It ties sales to billing, stock, and
> procurement in one place, so figures can't disagree. And it handles Philippine
> rules directly: VAT-inclusive pricing and the PWD/Senior discount computed the
> statutory way.

**6. How did you validate the business need?**
> `[FILL IN: interviews, observations, documents reviewed, who you spoke to.]`
> The process analysis in Chapters 1–3 and the user evaluation are the evidence.

---

## 2. Architecture and technology choices

**7. Why Django and MySQL?**
> Django gives us built-in authentication, an ORM with parameterized queries, CSRF
> and XSS protection, and a mature migration system. Those matter for a system that
> handles money and stock. MySQL is a reliable relational database with the
> transactions and row-level locking we need for exactly-once stock handling, and it
> is what the host (Railway) offers in one click.

**8. Why server-rendered templates? Why also a REST API?**
> Server-rendered pages are simpler to secure and to run on weak connections, and
> they let us avoid a Node build step. The same-origin DRF API under `/api/` is kept
> for programmatic access, and it **mirrors** the template views. Both go through the
> same shared permission, scoping, and state-transition code, so they can't drift apart.

**9. How are the apps divided and why?**
> By business area: `accounts`, `branches`, `inventory`, `sales`, `orders`,
> `billing`, `procurement`, `reports`, and `core` for shared permissions, scoping,
> and notifications. Support apps cover `audit`, `backups`, and `offline_sync`.
> Notably `orders` (the storefront) has **no models of its own**. It writes the same
> `SalesTransaction` rows as the POS, so there is no sync step between online and
> in-store sales.

**10. Why a custom User model?**
> Django recommends it at project start. We need five roles, role profiles, a branch
> assignment, per-user module restrictions, and two-factor settings on the user
> itself. Changing the user model later is very hard, so we did it first.

**11. Why no CDN or external fonts?**
> Stores can lose connectivity. All CSS, JS, and fonts are served from the app, so
> pages render without third-party services. Layered on that, a service worker keeps
> saved copies of key pages and queues a limited set of changes while offline, and
> the Windows desktop shell bundles the front-end resources.

**12. How does it scale to more branches or transactions?**
> Branch is a first-class field on the data, so adding a branch is a record, not a
> code change. Queries are scoped and paginated. Stateless Gunicorn workers can be
> scaled horizontally against one MySQL database. Honest limit: there is no task
> queue, so everything runs synchronously inside the request. That is fine at this
> business's scale, and a queue would be the first change for large growth.

---

## 3. Security and access control

**13. How do you stop a manager from seeing another branch's data?**
> Branch scoping is applied on the server in shared helpers (`core.scoping`), not in
> the UI. Every query for a manager or staff member is filtered to their assigned
> branch. Hiding a menu item or a dropdown is never treated as security. If a manager
> guesses another branch's order ID, the server doesn't return it.

**14. What if a user has no branch assigned?**
> It fails closed. A restricted user with no branch gets no data rather than all
> data. Only the Owner is unrestricted.

**15. What if a user edits the price, branch ID, or status in the request?**
> The server ignores it. Prices are looked up server-side from the product and its
> quantity tiers, and totals are computed by one pricing module shared by the POS
> preview, the saved sale, the receipt, and the online cart. Branch comes from the
> user's assignment, and statuses only change through guarded transition code.

**16. SQL injection, XSS, CSRF?**
> The Django ORM parameterizes all queries. Templates auto-escape output. Every form
> POST carries Django's CSRF token. Uploaded payment proofs are opened with Pillow
> and verified as real JPEG, PNG, or WEBP images, and they are served only through
> authorization-checked views, not the public media route.

**17. Passwords and sessions?**
> Passwords are hashed by Django's password framework and never stored in plain text.
> Login is rate-limited: 5 failed attempts locks the account for 5 minutes. Password
> reset is throttled and gives the same response whether or not the account exists.
> Two-factor login via emailed one-time code is available. `[Confirm who it applies
> to before saying "required".]`

**18. How do template views and the API enforce the same permissions?**
> They call the same shared modules: `core.permissions`, `core.module_permissions`,
> and `core.scoping`, plus the same transition functions. The API also carries a
> `HasModuleAccess` check that re-applies per-account module grants by route name and
> HTTP method.

**19. Is there an audit trail?**
> Yes. The `audit` app captures changes and the actor, using database triggers, and
> the Owner can review the Audit Log. Orders also keep status history, and every
> stock change is a `StockMovement` with who, why, and before/after quantities.

---

## 4. Inventory and stock integrity

**20. How do you prevent stock being deducted twice?**
> Each flow has exactly one deduction point. Walk-in sales deduct at the moment of
> sale. Online orders deduct only when marked Completed. Deliveries add stock only
> after validated receipt. All of it happens inside `transaction.atomic()` with row
> locks, so a double-click or retry can't repeat it. Voiding an already-refunded
> order is refused so the same units can't be restocked twice.

**21. What is a StockMovement?**
> It is the ledger entry for every stock change: the product, location (floor or
> warehouse), movement type (Sale, Procurement Received, Transfer, Manual In/Out,
> Adjustment, Write-off, Expiry Write-off, Customer Return, Supplier Return, and so
> on), quantity change, **quantity before and after**, the reference record, the
> batch, notes, and the user who did it. You can reconstruct any stock level from it.

**22. Two cashiers sell the last item at the same time?**
> The inventory row is locked with `select_for_update()` inside the transaction. The
> second request waits, re-reads the real quantity, and is refused if nothing is left.
> The worst case is a clear "insufficient stock" message, never an oversell.

**23. How do stock transfers and disputed quantities work?**
> A branch requests a withdrawal slip, a manager approves it, and it is released with
> the quantity picked. The receiver acknowledges receipt. If the counted quantity
> differs, the slip becomes a **Discrepancy** and a Transfer Discrepancy movement
> records the difference, so the gap is visible instead of silently absorbed.

**24. Batches and expiry?**
> Stock is tracked in batches with expiry dates and consumed in FEFO order (first
> expired, first out). Expired stock can't be sold even before the daily sweep runs,
> because available quantity nets out anything past expiry. The sweep then writes
> it off with an Expiry Write-off movement.

**25. Low stock and reorder alerts?**
> The system raises low-stock and expiry alerts through the notification system for
> the relevant branch's managers and the Owner, and alert links pre-fill the refill
> or reorder form so action takes one click.

**26. What happens to stock when an online order is cancelled or refunded?**
> An order cancelled before completion never deducted stock, so there is nothing to
> restore. Voiding a **completed** sale reverses the stock (Owner and Branch Manager
> only). A refund within 7 days restocks only items marked Sellable, as a Customer
> Return movement. Damaged items are not restocked.

---

## 5. Sales, orders, and billing

**27. Walk through a POS sale.**
> Staff add products to the cart. The server prices them (tiers and VAT, plus a
> PWD/Senior discount if a card is shown). The sale, items, and billing record are
> created atomically, stock is deducted by FEFO batch with `StockMovement` rows, and a
> receipt is produced. A cash walk-in is created Completed and Paid in one step
> because the money has already changed hands.

**28. POS vs. Order Console?**
> The POS records walk-in (Physical) sales at the counter. The Order Console is the
> internal view over online orders, which customers placed on the storefront, where
> staff approve, prepare, and complete them. Both read and write the same
> `SalesTransaction` table, distinguished by `transaction_type`.

**29. How does the customer storefront work?**
> Anyone can browse. An account is needed at Add to Cart. The cart lives in the
> session, nothing is written to the database until checkout, and the shopper picks
> pickup or delivery and a payment method. Checkout creates the order and billing
> record, and the customer tracks it under My Orders.

**30. How is price computed, and why can't a customer tamper with it?**
> The browser never sends prices. The server computes every line from the product
> and its bulk tier and passes it through one pricing module. Online checkout always
> writes full price, and the PWD/Senior discount can only be applied by staff after
> seeing the ID.

**31. Payments without a gateway, and fraud prevention?**
> Payments are recorded then verified. For GCash or bank transfer, the reference
> number and screenshot are required before the order exists. Staff compare them to
> the real account and mark the payment Paid or Rejected with a reason. The staff
> member who rang up a walk-in cannot verify their own payment. An order can't be
> Completed unless its payment is Paid, and a Paid payment can't be silently
> un-verified.

**32. Why Decimal for money and how do you round?**
> Floating point can't represent peso amounts exactly. We use `Decimal` and round to
> centavos with ROUND_HALF_UP in one shared module, so the preview, the saved sale,
> and the receipt always match.

**33. Valid order transitions and who triggers them?**
> **Pending → Approved → Preparing → Completed**, with **Cancelled** allowed from
> unfulfilled states. A single table of legal moves is re-checked inside a row lock,
> so two staff can't both advance the same order. Staff cancel with a required
> reason. A customer can cancel their own order only while Pending or Approved.
> Voiding a completed sale is limited to Owner and Branch Manager.

---

## 6. Procurement

**34. PO to delivery to stock?**
> A PO is raised and Approved. The supplier and Owner then negotiate price and
> delivery date (Pending Owner Review ↔ Pending Supplier Review → Terms Agreed), the
> supplier confirms it, and it becomes Incoming. On delivery, the receiver checks
> quantities and condition, and only then does stock enter the warehouse (a Procurement
> Received movement). Statuses go Partially Received → Received, and Received and
> Cancelled are terminal.

**35. Short or damaged deliveries?**
> A PO can be Partially Received, so only validated quantities enter stock. Goods can
> be sent back to the supplier through a supplier return, which records a Supplier
> Return stock-out movement.

**36. Supplier performance or price changes?**
> `[Verify before claiming a supplier scorecard.]` What is certain: the negotiation
> history is kept on the PO, and suppliers see only their own orders.

---

## 7. Reports and dashboards

**37. Owner vs. Manager reports?**
> The Owner sees consolidated figures across all branches or can filter by branch.
> Managers see only their own branch. Staff do not have Reports access. Reports
> cover sales, inventory, procurement, and billing.

**38. Exports?**
> CSV and PDF export, and the export uses the same filters and the same queries as
> the screen.

**39. How do you know the dashboard matches the records?**
> Reports are computed directly from the transaction tables, with no separate summary
> tables to drift. Revenue counts completed, paid sales and subtracts refunds
> separately, because a refund doesn't change the original total.

**40. Notifications?**
> Events such as new orders, status changes, payment decisions, low stock, and
> expiry create in-app notifications through a central policy matrix (see
> `docs/notification-matrix.md`) and go to the right role and branch. Limitation:
> in-app only for orders, with no SMS.

---

## 8. Testing, quality, and deployment

**41. How did you test?**
> A large automated suite: roughly 2,100 test functions across accounts, core,
> sales, orders, billing, inventory, procurement, reports, audit, backups, and
> offline sync. It runs with `python manage.py test`. `[Run it before the defense and
> quote the actual pass count.]`

**42. Test cases for critical rules?**
> Yes: branch-scoping regression tests (`core.tests_branch_scoping_regression`),
> security remediation tests, stock-transfer workflow tests, quantity-write guards,
> billing payment-reference and detail-state tests, and permission tests for module
> access. These target exactly-once stock, cross-branch isolation, and transitions.

**43. User acceptance testing?**
> `[FILL IN: who took part, how many, the instrument, results, and the changes you
> made because of their feedback.]` The evaluation pack is in `docs/evaluation-pack.md`.

**44. Deployment?**
> On Railway with managed MySQL, deployed from GitHub using Gunicorn and WhiteNoise.
> Secrets live in environment variables, never in the repo (`.env` is not committed).
> See `docs/deployment.md`.

**45. Backup and disaster recovery?**
> Backups are automated: a database dump, uploaded files, a list of erased accounts,
> and a manifest with checksums. Everything is **encrypted on the server (AES-256-GCM)
> before it leaves**, and stored off-site (Backblaze). A restore test uses the
> manifest to verify. Owner-facing status is under System & Monitoring → Backups.
> Know the gaps: Railway variables, including the encryption key, must be kept in a
> password manager (see `docs/backup-and-restore.md`).

**46. What does `seed_demo --reset` do?**
> It wipes local application data and reloads demo data for development and
> demonstrations. It is destructive, so it is for dev and demo databases only, never
> production.

---

## 9. Limitations and future work

**47. Biggest limitations?**
> 1) No payment gateway, only recording and verification. 2) Order notifications are
> in-app only. 3) Online stock is a soft reservation: the row lock at checkout
> prevents overselling, but an open online order can later fail at completion if the
> counter drained the stock. 4) Filipino translation is partial. 5) No delivery
> fees or courier routing.

**48. What would you add?**
> A payment gateway (GCash/Maya API) with automatic reconciliation, SMS/email order
> notifications, a hard stock hold for online orders, a task queue for background
> work, demand forecasting from the sales history, and delivery fee and courier
> management.

**49. Maintenance after turnover?**
> The repo has documentation (README, PROJECT-LOG, this folder, `docs/`), automated
> tests that protect behavior while changing code, migrations for schema changes, and a
> deployment runbook. Business rules live in named service modules, not scattered in views.

**50. Hardest technical problem?**
> Making stock and money changes exactly-once under concurrency: two cashiers, two
> staff approving the same order, a payment rejected at the same instant as completion.
> We solved it with atomic transactions, row locks taken in a consistent order, and
> re-checking state inside the lock. `[Add your own story if different.]`

---

## 10. Team and process

**51. Each member's contribution?**
> `[FILL IN]`

**52. Methodology and changing requirements?**
> `[FILL IN, e.g. iterative/Agile, with the documented plan and roadmap in ROADMAP.md
> and gerlyn-one-week-build-plan.md.]` Changes were managed through version control
> and tests, so a change that broke a business rule would be caught.

**53. Cost-benefit / ROI?**
> `[FILL IN with your numbers.]` Hosting is about $5/month on Railway (per
> `docs/deployment.md`), with open-source software (Django, MySQL) and no licence
> fees. Benefits: one real-time view of all branches, less manual reconciliation,
> fewer stock and payment errors, and customer online ordering.

---

## Rules for the Q&A
- If you don't know, say *"we'd have to check the code"* rather than guessing.
- Name the limitation before they find it. Panels reward honesty.
- Lead with **exactly-once stock**, **server-side branch scoping**, and **verified
  (not processed) payments**. Those are the strongest parts and the likeliest probes.
