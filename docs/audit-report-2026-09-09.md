# Audit Report — 9 September 2026

**For the team.** This records the code audit run on 8–9 September, what it
found, what was fixed and merged into `main`, and what is still open.

`main` after this cycle: **`a6fb956`**.

---

## 1. What we audited

A line-by-line correctness and security pass over the code paths that touch
money, stock and access control:

| Area | What we looked at |
|---|---|
| Sales / POS | Walk-in sale creation, the DRF write serializer, stock deduction, order numbering |
| Order Console | Status transitions, void, discount application, branch scoping |
| Billing | Confirm (web + API), payment-method and proof handling, status pills, list totals |
| Storefront / checkout | Cart, branch selection, checkout writes, delivery vs pickup, GCash reference, on-page copy |
| Reporting | Dashboard and Sales/Billing revenue figures |
| Platform | Login lockout backend, `/media/` exposure, module-permission gate vs the API, unvalidated request input |

The A/B/C/D findings (maker/checker on payments, online pricing parity, PO
authorization, procurement transitions, billing state machine) were fixed in
the **earlier** cycle and were already in `main` before this pass
(`432f6f9`, `d9c4a8c`, `034874e`, `b24d04e`, `19b8811`, `0e354f4`).

---

## 2. Findings fixed and merged today

### Security / correctness — the F series (F-01 … F-15)

| # | Finding | Fix | Commit |
|---|---|---|---|
| F-01 | Online orders lost stock twice — serializer deducted at creation, Console deducted again at Completed | Serializer deduction scoped to Physical channel only; Physical creation now writes a `StockMovement` audit row | `6d6b2a7` |
| F-02 | `BillingConfirmAPIView.patch` checked role but not branch — Staff could PATCH another branch's bill to Paid | Calls `user_can_access_billing_record`, 404 on mismatch, `select_for_update` around the update | `1b01bdf` |
| F-03 | Dashboard/Reports revenue summed **every** sale, including Cancelled and Pending — contradicted the Billing Report | Single `revenue_sales()` filter (Completed only) at all shared entry points; added a separate "committed but unfulfilled" pipeline figure | `0c17596` |
| F-04 | Checkout applied the PWD/Senior discount on any request with non-empty ID strings — no card ever seen | Checkout never reads/stores discount fields; new staff-only, branch-scoped `transaction_apply_discount` (walk-in or in-store pickup only, refuses Delivery); re-prices through `sales.pricing`; records `discount_applied_by/at` | `c854f4c` |
| F-05 | `mode_of_payment` written straight from POST, unvalidated — junk broke the Billing breakdown and skipped the POS cash-tendered check | Both views validate against `ModeOfPayment.choices`, fall back to the safe default (Cash / COD) like the other choice fields | `f77d568` |
| F-06 | GCash payment-proof screenshots served by `static.serve` with **no auth** — customer name, reference, amount readable by anyone | New `@login_required` `billing_proof(pk)` with the same scoping as the billing API, streams via `FileResponse`; `/media/` route narrowed to exclude `payment_proofs/` | `53fdf79` |
| F-07 | Module-permission gate only knew template URL names — a Staff with Billing revoked could still hit `/api/billing/.../confirm/` | Registered the `api-*` URL names under their modules in `MODULE_REGISTRY` (Billing, Sales, Procurement, Reports, stock, Withdrawals) | `71963cc` |
| F-08 / F-09 | Three request values reached a DB write / `int()` with no validation (500s); a fourth (negative cart qty) parsed fine and shrank the line | `billing_create` rejects a missing method before save; POS resolves products up front and re-renders with the basket intact; `add_to_cart` falls back to 1 and rejects `< 1` | `60384ee` |
| F-10 | Voiding a completed sale overwrote `billing.confirmed_by` with the voider while keeping the original `date_confirmed` — a pairing that never happened | Void writes its own `voided_by` / `date_voided`; never touches `confirmed_by` / `date_confirmed`; detail page shows the new pair | `f6e0677` |
| F-11 | `delivery_address` written straight from POST; a real pickup and a delivery with a lost address looked identical | New `SalesTransaction.fulfillment_type` (Delivery / Pickup), validated: Delivery needs a non-blank address, Pickup clears it, missing value is refused; migration `0008` backfills from existing addresses | `221ace7` |
| F-12 | `billing_list` outstanding total was aggregated **after** the tab filter — showed ₱0.00 on the Paid/Rejected tabs | Computed from the pre-filter queryset, same as the tab counts above it | `d1cc37f` |
| F-13 | `_pos_user_branch` returned `None` for both the Owner and a branchless Staff/Manager — the latter got the every-branch POS with no scope check on the posted branch | `POS_BRANCH_UNASSIGNED` sentinel; `transaction_add` refuses an unassigned account on GET and POST; `user_can_access_branch` runs for every role | `6a1162f` |
| F-14 | Order number `NNN` was the global pk — first order of 2027 came out `ORD-2027-104`; create-then-patch on `order_number=''` collided under load | New `OrderNumberSequence` (one row per year, bumped under `select_for_update`); number set on the INSERT itself; data migration seeds from existing numbers | `fd17771` |
| F-15 | Login lockout counted in the default `LocMemCache` — per-process, so the real cap was 5 × workers, and every restart reset it | `CACHES` → `DatabaseCache`; `createcachetable` added to the deploy start command and documented for dev | `b39acca` |

### Display and data-integrity fixes

| Finding | Fix | Commit |
|---|---|---|
| `BillingRecord` `CANCELLED` fell through to "Pending" (warn) in all six status-pill ladders — read as money still owed, and contradicted the void rows on the same page | "Voided" pill (neutral) on internal surfaces, "Cancelled" on `track_order`; Cancelled filter tab + count on the list. Display only | `a110382` |
| Storefront Branch checklist did two jobs — ticking another branch to browse it **silently emptied the cart**; `cart_count` (unconditional) disagreed with the branch-filtered cart | `?branch=` is now a pure browse filter; shopping branch set only through `_start_shopping_at()`; `cart_count` is 0 with no branch; deleted the GET-mutating `set_branch`/`clear_branch` | `6a42482`, `9613561` |
| `price_line`'s discount branch rounded the line total last while freezing an already-rounded unit price — `SalesItem.save()` recompute then diverged by a centavo and `totals_disagree` flagged sound sales | Round the per-unit net price once, multiply the rounded unit by quantity (same order as `SalesItem.save()`); POS preview JS mirrors it | `b01b17c` |

### Storefront truthfulness — the L and U series

`17b632f` — four silent / unchecked paths in `orders.customer_views`:

- **L8** — checkout accepted a **GCash order with a blank reference number**, and wrote the POSTed reference onto any billing row regardless of method. Reference is now validated up front against the *stored* method; GCash with a blank/missing reference is refused with the cart intact, every other method clears it.
- **L6** — checkout on GET, and `update_cart` decrease (including at qty 1), changed state or bounced **in silence**. All paths now emit a message.
- **L7** — `undo_add` redirected to the raw `HTTP_REFERER`; added `_safe_referer()` and routed it through the same validation as every other redirect.
- **L4** — `my_orders` ran five `.count()` queries per load into a `counts` key the template never reads. Removed.

`ba11e65` — eleven places the public shop **stated something the code does not do**, or stated it two different ways:

- **U1** — home banner "Three branches / Tarcan, Makinabang and Tiaong" vs `about.html` "has become four". Banner now counts and names off `nav_branches` (the DB list the footer already uses); `about.html` stops counting.
- **U2** — "Baliwag" vs "Baliuag" across six files. Settled on **Baliuag**.
- **U3** — three false claims on `about.html` (payment methods ran counter + online together; "same discount online or in person"; "no delivery surprises"). Copy corrected to match F-04 and the fee reality.
- **U4** — cart promised "Calculated at delivery" for a fee that **nothing computes**. Now "Arranged with the branch"; ₱500 threshold printed from `free_delivery_over`.
- **U5** — `product_detail` stock chip and Add-to-Cart button used an **all-branch** stock sum while the cart only accepts the session branch's stock. Now branch-scoped and labelled by branch.
- **U6** — catalog ignored the fewer-than-two-categories threshold every other surface honours. Rule moved into `storefront_categories()` context processor.
- **U7 / U9** — `confirmation.html` printed `order_status` raw ("Pending") vs "Waiting for the store" one click later. Both use `_order_status_pill.html`; added a breadcrumb back to My Orders.
- **U8** — catalog and home grids held drifted copies of the product card. One `product_card.html` now.
- **U10 / U11** — dead boolean (`total >= 500 and total > 0`); `toast.js` grabbed the first submit button instead of `e.submitter` (product page has two).

`APP_VERSION` bumped (CSS + JS changed).

---

## 3. Tests added this cycle

New regression modules, all passing:

- `sales/tests_discount_rounding.py` — uneven-quantity discount lines reconcile
- `orders/tests_branch_cart_decoupling.py` (15) — browse filter vs shopping branch
- `orders/tests_checkout_and_cart_messages.py` (22) — L4/L6/L7/L8
- `orders/tests_storefront_truthfulness.py` (13) — the U findings with behaviour behind them
- plus per-finding tests in `billing/tests.py`, `sales/tests.py`, `reports/tests.py`, `orders/tests.py`, `accounts/tests.py` for F-01 … F-15
- `orders/__init__.py` added so the `orders` test package is discoverable

**Baseline after this cycle** (`accounts + core + sales + orders + billing`):
**≈296 tests, 2 failures, 0 errors.** Both failures are **pre-existing on
`main` and unrelated** to this work:

1. manager-create modal redirect
2. short-delivery receiving discrepancy

`reports`, `inventory` and `procurement` were not part of the main regression
run — see pending item 4.

---

## 4. Pending / still open

| # | Item | State |
|---|---|---|
| 1 | **`origin/feature/owner-requested-updates`** (4 commits) — customer returns (per-line-item returns against a completed sale) + stock-request UI rename and mislinked-flow fixes | Not merged. Needs review + reconcile against current `main` |
| 2 | **`origin/security/branch-scoping-cache-indexes`** (2 commits) — closes a fail-open in branch scoping, shares the cache, indexes the hot queries; also adds the `.scopebar` inventory component | Not merged. Overlaps F-15 (cache) — reconcile before merging |
| 3 | **Two known test failures** (manager-create modal, short-delivery receiving) | Pre-existing, not caused here, not yet fixed |
| 4 | **Regression run doesn't cover `reports` / `inventory` / `procurement`** | F-03 added `reports/tests.py`; the others still lean on manual testing |
| 5 | **Stale worktrees / branches** — `fix/c1-*`, `fix/c3-c6-*`, `fix/d1-*` and the `gerlyn-fix-f0*` worktrees are superseded (their fixes are in `main` under different SHAs) | Safe to `git worktree remove` and delete the branches |
| 6 | **WIP backup `54c3df6`** — 14 files that overlapped merged fixes and were never reconciled | The feature half (Store Settings / hero / receipt) landed via `feature/store-settings-hero-receipt`; confirm nothing else is stranded, then it's just history |
| 7 | **Mobile hero copy** still hardcoded (noted in the Store Settings scope) — the U1 fix only made the desktop banner database-driven | Follow-up |
| 8 | `PROJECT-LOG.md` "Bugs found and fixed" table and the test count (`Ran 13 tests`) are now stale | Update to reflect this cycle |

---

## 5. Merge order into `main` today

```
f01 → f03 → f05 → f06 → f07 → f08/f09         (early batch, 02:48)
f12 → f14 → f15 → f13 → f10 → f11 → f04        (96638f3, 13:08)
billing-cancelled-pill                          (22ea495, 14:38)
storefront-branch-cart-decoupling               (e029204, 15:25)
discount-total-rounding                         (c3ad737, 15:34)
storefront-checkout-and-cart-messages           (6671ebd, 18:49)
storefront-copy-and-scope                       (a6fb956, 18:49)
```
