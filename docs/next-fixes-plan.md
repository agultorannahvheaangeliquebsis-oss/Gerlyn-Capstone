# Next-Fixes Plan

Follows [`audit-report-2026-09-09.md`](audit-report-2026-09-09.md). Baseline
`main` = `a6fb956`.

Priorities: **P0** ship before the next demo · **P1** queued work, needs
reconciliation · **P2** test health · **P3** housekeeping.

## Progress (as of `25a0bda`)

| Item | State |
|---|---|
| **P0** fail-closed branch scoping | ✅ done — `977ef77`, +13 regression tests |
| **P2.3** two stale test failures | ✅ done — `6bc947a` |
| **P1.1** hot-query indexes | ✅ done — `25a0bda` (refdata needed no change — F-15's shared cache covers it) |
| **P1.2** `security/*` remainder | ✅ folded into P0 + P1.1; nothing left on that branch |
| **P1.2** `owner-requested-updates` | ⛔ blocked — diverged from `main` (modify/delete conflicts on inventory templates), and bundles unreviewed *customer returns*. Needs a rebase by the author, or a scope decision + go-ahead to resolve here |
| **P2.4** widen the regression run | ⬜ not started |
| **P3.5** stale branches / worktrees | ⬜ not started — destructive, needs go-ahead |
| **P3.6** mobile hero | ⬜ not started — larger than a copy fix; wire it to `HomeHeroSlide` |
| **P3.7** refresh `PROJECT-LOG.md` | ✅ done |

---

## P0 — Branch scoping still fails *open* (not covered by F-01…F-15)

**This is a live data-leak on `main`.** `core/scoping.py` already has the
fail-*closed* helpers (`scope_queryset`, `branch_ids_for_user` → `[]` for an
unassigned account), but ~17 call sites still use the old idiom:

```python
user_branch = branch_for_user(u)      # None for Owner AND for an unassigned Manager/Staff
if user_branch:
    qs = qs.filter(branch=user_branch) # unassigned account falls through UNFILTERED
```

`branch` is `NULL`-able in normal operation — `BranchManagerProfile.branch`
and `StaffProfile.branch` are `on_delete=SET_NULL`, so deleting a branch
drops its manager/staff into the unassigned state.

Confirmed still live on `main`:

| Site | Impact |
|---|---|
| `reports/views.py` `export_csv` (~L1947–2004) | Unassigned Manager/Staff downloads **every branch's** sales, inventory, procurement and billing in one request |
| `reports/api_views.py:48,112,131,149` | Same leak on the report API |
| `reports/views.py:1230, 2106` | Dashboard / report feeds render all-branch data |
| `inventory/views.py:1101,1334,1352,1613`, `inventory/api_views.py:227…381` | All-branch stock reads |
| `procurement/views.py:204` | All-branch procurement |
| `sales/api_views.py:43`, `billing/api_views.py:53` | Already `.none()` on the else — safe, leave as reference pattern |

**Action**

1. Re-derive the fix from `origin/security/branch-scoping-cache-indexes`
   commit `c67f0fa`, **dropping the parts already merged**:
   - billing API branch check → **done** (F-02)
   - `CACHES` → `DatabaseCache` → **done** (F-15)
   - `transaction_add` fail-open on write → **done** (F-13, `POS_BRANCH_UNASSIGNED`)
2. Convert each remaining `if user_branch:` read to `scope_queryset(...)` or
   an explicit `branch_ids_for_user()` check that filters to `[]` when the
   list is empty. Owner (`None`) stays unfiltered.
3. Collapse the four near-duplicate branch helpers (`inventory/views.py`,
   `reports/views.py`, `sales/views.py._pos_user_branch`, API aliases) onto
   `core/scoping.py`. Two of them wrap the profile lookup in
   `except Exception: return None` — any error becomes "show everything".
4. Regression test both directions: an unassigned Manager/Staff sees
   **nothing**; an assigned one still sees its **own** branch. Revert-verify
   each assertion (it must fail with the fix backed out).

**Effort** ~1 day. **Risk** touches many views — gate on the *full* suite
plus a manual smoke as a branch-less Manager. **Do this first.**

---

## P1 — Queued branches (reconcile, don't fast-forward)

### 1. `origin/security/branch-scoping-cache-indexes` (2 commits)

After P0 lifts the scoping fix, what's left is worth landing on its own:

- **DB indexes** — `SalesTransaction (branch, -transaction_date)` (the pair
  every scoped report filters on) and three on `StockMovement` (fastest-
  growing table; `Meta.ordering` currently forces a filesort on every read).
  Low risk, take as-is. Migration `0026` / the inventory index migration.
- **`core.refdata` cross-worker cache invalidation** — same class of bug as
  F-15. Now that `CACHES` is `DatabaseCache`, **verify whether this already
  works**; if so, drop the change, otherwise cherry-pick just that hunk.
- **`.scopebar` inventory UI** (commit `920d294`) — a segmented per-branch
  bar replacing the Branch `<select>` on `inventory_list`. Feature, not a
  fix. Defer to the UI pass; don't let it block the indexes.

**Recommendation:** split the branch — land indexes (+ refdata if needed)
this cycle, park the scopebar.

### 2. `origin/feature/owner-requested-updates` (4 commits)

Two independent pieces, both branched off an **older `main`** (the branch's
own merge commit lists 10 conflicted files). Rebase onto `a6fb956` first.

- **Stock-request UI rename + dead-end fixes** (`1c83fc3`, `7a325fe`, Opus) —
  renames three actions all called "Stock Adjustment" to what they do,
  repoints CTAs that sent users to a form that can't do the job, opens
  `transfer_list` to STAFF (they currently hit a 403 on the success
  redirect), adds a URL redirect for old bookmarks. This is a **bug-fix
  batch**, low schema risk. Rebase and merge first.
- **Customer returns** (`ace3b54`) — new `SalesReturn` / `SalesReturnItem`
  models + migrations, a new module-permission entry, a `CUSTOMER_RETURN`
  `StockMovement` type, ~1200 LOC. Treat as a **scoped feature review**:
  confirm returns are in the approved Chapter 1 scope / ERD / DFD / business
  rules before merging — the same test the Bundle Management decision
  applied. If in scope, review the state machine (Pending → Approved →
  Refunded / Rejected) and the restock-on-Resellable path, then merge.

---

## P2 — Test health

### 3. Fix the two pre-existing failures

- **`core/tests.py` `test_short_delivery_flags_discrepancy_and_adds_only_delivered`**
  — the `security/*` branch author diagnosed this as a **stale test**: it
  asserts a PO receipt raises *floor `Inventory`*, but the receive handler
  credits *`WarehouseStock`* only, which is what `Inventory`'s own docstring
  specifies. Confirm the handler is correct, then fix the assertion to check
  `WarehouseStock`. Its other assertions already pass. Low effort.
- **`accounts/tests.py:42` `test_creating_a_manager_sets_their_branch`** —
  POSTs to `user_add` and expects `assertRedirects(..., user_list)`. The
  modal-based user form now re-renders (200) or redirects with a query
  string on some paths. Diff the test against the current `user_add` view;
  decide which drifted and align. Low effort.

### 4. Widen the regression run

`reports`, `inventory`, `procurement` sit outside the standard command
(`accounts core sales orders billing`). F-03 added `reports/tests.py`; the
other two lean on `core/tests.py` acceptance tests + manual checks.

- Add all eight apps to `run.sh test` and CI as the merge gate.
- New coverage: the P0 fail-closed scoping (per app), procurement
  receipt → `WarehouseStock`, inventory count-correction / write-off.

---

## P3 — Housekeeping

### 5. Delete stale branches and worktrees

Superseded — their fixes are on `main` under different SHAs
(`b24d04e`, `19b8811`, `0e354f4`):

```
git worktree remove ../gerlyn-fix-a1  ../gerlyn-fix-b2  ../gerlyn-fix-c1 \
  ../gerlyn-fix-c3-c6  ../gerlyn-fix-d1  ../gerlyn-fix-f03  ../gerlyn-fix-f05 \
  ../gerlyn-fix-f06  ../gerlyn-fix-f08f09  ../gerlyn-fix-fulfillment-display \
  ../gerlyn-fix-storefront-branchcart  ../gerlyn-fix-storefront-loose-ends
git branch -D fix/c1-po-authorization-gate \
  fix/c3-c6-procurement-transition-and-branch-scope \
  fix/d1-billing-confirm-state-machine   # + the merged fix/f0* and fix/storefront-* locals
```

Check each worktree for uncommitted changes first (`git -C <path> status`).
Per the team-git note: never `git stash` from a worktree.

### 6. Mobile hero copy (U1 follow-up)

U1 made the **desktop** banner count/name branches off `nav_branches`. The
mobile hero still hardcodes them. Point it at the same source; add a
fallback string for a store with no branch rows. Small.

### 7. Refresh `PROJECT-LOG.md`

The "Bugs found and fixed" table stops at the first cycle and "Ran 13 tests"
is now ≈296. Add an F/L/U cycle section (or link the audit report) and
update the count. The `createcachetable` step is already documented (F-15).

---

## Recommendations (process)

1. **Keep a findings register** — `docs/audit-findings.md`, one row per
   ID (severity · status · commit). Right now F/L/U IDs live only in commit
   messages, so the next audit can't diff against this one.
2. **Full suite as the merge gate**, not the 5-app subset — three of the
   findings this cycle were "the API is weaker than the page it mirrors"
   (F-02, F-07) or "an app with no tests" (`orders` until F-08/09). A
   coverage gap is where the next one hides.
3. **Reconcile every pre-today branch before merging.** `fix/c1`,
   `fix/c3-c6`, `fix/d1` already duplicated merged work; `security/*` and
   `owner-requested-updates` were cut from an older `main`. Rule: rebase
   onto current `main`, re-run tests, diff intent against what already
   landed.
4. **Fix the root cause of the fail-open class** — `on_delete=SET_NULL` on
   the profile→branch FKs is *why* "unassigned Manager/Staff" is silently
   reachable. Consider `PROTECT` (block deleting a branch that still has
   staff) or a startup/admin integrity check, so P0's guard is defence in
   depth rather than the only line.
5. **Revert-verify security regression tests** — assert they fail with the
   fix backed out, so they can't pass both ways. The `security/*` author
   did this; make it the standard.
6. **One focused `/api/` re-audit after P0** — walk every DRF view against
   its template twin (scope, module gate, validation) and record the diff.

---

## Suggested sequence

| Cycle | Work |
|---|---|
| 1 | **P0** fail-open scoping + **P2.3** two test fixes → land together behind the full suite |
| 2 | **P1.1** indexes (+ refdata), **P1.2** stock-request UI rename (rebase + merge) |
| 3 | **P1.2** customer returns feature review, **P2.4** widen coverage for the 3 apps |
| any time | **P3** branch cleanup, mobile hero, `PROJECT-LOG.md` |
