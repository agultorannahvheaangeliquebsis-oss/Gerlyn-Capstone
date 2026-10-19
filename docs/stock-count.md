# Stock Count (cycle count) sessions

Inventory → **Stock Counts** (`/inventory/counts/`). A Stock Count session compares
what is physically on the shelf or in the warehouse with what GerlynPLUS says is
there, and applies the differences only after a second person reviews them.

Code: `inventory/stock_count.py` (every rule), `inventory/stock_count_views.py`
(thin views), models `StockCount`, `StockCountLine`, `StockCountEvent`,
`StockCountScanLog` in `inventory/models.py`, templates
`templates/inventory/stock_count_*.html`, tests `inventory/tests_stock_count.py`.

## Flow

```
Draft ──► Counting ──► Submitted ──► Approved (variances applied)
  │          │             │    └──► Rejected  (no stock change)
  │          │             └──► Counting (approver sends items back for recount)
  └──────────┴─────────────┴──► Cancelled (no stock change)
```

1. **Start.** Choose the location (Floor *or* Warehouse) and, optionally, a
   category to count one section at a time. The Owner also picks the branch;
   Managers and Staff are pinned to their own. Starting locks that location's
   stock rows and copies each product's `quantity_on_hand` into the session
   (`StockCountLine.expected_qty`). That **snapshot** is what every variance is
   measured against, never the live quantity.
   - Only **one open session per branch + location**. A second Start is refused
     with the open session's number. (`StockCount.open_slot` is unique while
     open, so the database refuses it too.)
   - At most 400 products per session (`core.limits.MAX_STOCK_COUNT_LINES`):
     the review form posts two fields per line and Django refuses posts with
     more than 1,000 fields. Count a big branch by category.
2. **Count.** On a phone: tap **Scan with Camera**, or use a hardware scanner
   in the text box. Each scan looks the code up on the server (barcode,
   case-insensitive, then SKU, the same order as Receive Delivery), writes a
   `StockCountScanLog` row (mis-scans included), and adds **1** to that line
   *in the form*. Products without a barcode are counted with the − / + stepper
   or by typing. **Save Progress** stores the counts. The screen is a *blind*
   count: it does not show the system quantity.
   - **Unknown barcode**: a clear message, and the "Found something not on this
     list?" panel is highlighted so the product can be picked and added.
   - **A product that is not in the session** (no stock row there, or outside the
     category): the panel opens with it preselected. **Add to Count** creates an
     *unexpected* line whose snapshot is taken at that moment.
   - **Repeat reads**: the camera closes after its first decode, so a held code
     cannot count twice from there. A hardware scanner in continuous mode that
     re-sends the same code within 0.7 s is ignored, with a message
     (`GerlynBarcodeScanner.attach(..., {ignoreRepeatMs})`).
   - Two phones can count the same session: a save only writes the lines that
     were changed on that phone.
3. **Submit.** The counter chooses what happens to lines nobody counted:
   **Leave uncounted items untouched** (the default) or **Treat uncounted items
   as zero** ("we looked and there were none"). Stock is never zeroed silently.
   Submitting notifies the approvers (see Notifications).
4. **Review / approve.** The review screen shows, for every line: the snapshot,
   the count, the variance (+/− with its value at cost), and the quantity on
   hand now → after. The approver ticks which variances to apply and gives a
   reason, either per line or one for all. Approval re-locks the session and
   the stock rows, recomputes everything from the database, and applies each
   ticked variance through `inventory.services.correct_stock_count()`: the same
   function the single Stock Adjustment form uses. Each one writes a
   `StockMovement` of type **Stock Count Adjustment** (`reference_id` = the
   session) and corrects the batches (FEFO for a shortfall, a correction batch
   for an overage).
5. **Reject** (reason required) or **Cancel** (reason optional) closes the
   session with no stock change.

## Who can do what

| | Owner | Branch Manager | Staff | Supplier / Customer |
|---|---|---|---|---|
| See sessions | All branches | Own branch | Own branch | 403 |
| Start, scan, count, submit | ✓ | ✓ | ✓ | 403 |
| Cancel | ✓ | ✓ | Only a session they started | 403 |
| Approve / reject / send back for recount | ✓, including their own count | ✓, but **not** a count they took part in | ✗ | 403 |

- **Maker/checker.** Anyone who started, scanned, counted or submitted a session
  is a *maker* and may not approve, reject or send it back. Another Branch
  Manager or the Owner must. **The Owner is the only exception**: nobody sits
  above the Owner, the same reasoning as an Owner's own stock request being
  auto-approved (`inventory.views._try_auto_approve`). The Owner's count is
  *not* applied silently on submit, though. Approval needs a reason per
  variance and the drift check below, so an Owner who submits is taken
  straight to the review screen.
- **Modules.** The routes form a **Stock Counts** module
  (`core.module_permissions.STOCK_COUNT`). View covers the list and detail. Add
  covers start, count, scan and submit. Edit covers review, approve, reject and
  recount. Delete covers cancel. Defaults: Manager has Add/Edit/Delete; Staff
  has Add/Delete. Migration `inventory/0034` grants these to existing accounts.
  Counting a location also needs **View on that location's own module** (Floor
  Stock or Warehouse Stock), so this grant can only narrow access. Role checks
  stay authoritative: a Staff account given Edit still cannot approve.
- **Branch scoping** goes through `core.scoping.scope_queryset`. A session from
  another branch is a 404, like a missing one.

## The drift rule (what approval applies)

Sales and transfers keep happening while people count. For each counted line
the approver ticks:

```
on hand after = on hand now + (counted − snapshot)
```

The *variance* is applied on top of the current quantity. That equals
`counted + (stock movements recorded after the line was counted)`, so a sale
rung up after the shelf was counted is **kept**, not undone by an older count.

Movements recorded **between the snapshot and the moment the line was counted**
are ambiguous: nobody can tell whether the counter saw the shelf before or
after them, and applying either way could be wrong by exactly that amount. So:

```
in-count drift = on hand now − snapshot − (movements after the line was counted)
```

If the in-count drift is not zero (`DRIFT_TOLERANCE = 0` units), the line
**cannot be applied**. It shows "Moved ±n while counting — recount". The approver
either unticks it (the rest are applied) or ticks it under **Recount** and sends
it back. A recount re-snapshots those lines to the quantity on hand now and
clears their counts, and the session returns to Counting. The drift is computed
as a residual on purpose: a stock change that somehow skipped the
`StockMovement` ledger also lands here and forces a recount instead of being
overwritten.

Other refusals at approval, which are all-or-nothing (nothing is applied if
any ticked line fails):

- a ticked variance with no reason;
- a result below zero (refused, never clamped);
- a session that is no longer Submitted, or whose status differs from the one
  the reviewer's page showed (`expected_status`, as on the order-status form).

A repeat approval (a double-click or a resubmitted form) is a no-op with the
message "already approved".

## Audit and notifications

- Every state change writes a `StockCountEvent` (shown as Count History) and goes
  through the audit layer (`inventory.StockCount` and `inventory.StockCountLine`
  are registered in `audit/registry.py`). The Audit Log action labels are:
  *Started a stock count*, *Saved stock count progress*, *Added an item to a stock
  count*, *Submitted a stock count for approval*, *Approved a stock count and
  applied its variances*, *Rejected a stock count*, *Sent a stock count back for
  recount*, *Cancelled a stock count*. Approval also writes one summary row
  listing every applied variance.
- Applied variances are `StockMovement` rows of type `Stock Count`
  (label "Stock Count Adjustment"), separate from ordinary `Adjustment`
  corrections. They show in Stock Transfers → Stock History, with
  the actor and the session number in the notes.
- Notification event `stock_count_submitted` goes to the Owner and that branch's
  Branch Manager(s) with Stock Counts access. It never goes to Staff (they cannot
  approve) or to the submitter. See `docs/notification-matrix.md`.

## Offline

Stock Counts are **online-only in this version**. None of the counting,
scanning, submit or approval forms are in the offline queue
(`offline_sync.registry.OPERATIONS`), so `offline-sync.js` blocks them while
offline with its standard explanation, and the server would refuse a replay
anyway. Approval must never be queued: it locks and re-reads live stock.

What *does* survive a dropped connection or an accidental reload mid-count:
the counting screen keeps unsaved quantities in the browser's
`sessionStorage` and restores them on that device. Saving still needs the
connection. A test pins that no `stock_count_*` route is queueable.

## Not built (v1)

- **Lot-level counts.** Lines are per product (`StockCountLine.batch` is kept for
  a later lot-level count but always NULL). A barcode identifies a product, not
  a lot. Shortfalls are taken FEFO including expired lots, and overages become
  a correction batch with no expiry, the same as a Stock Adjustment.
- A printable count sheet.
- Offline counting (see above).
