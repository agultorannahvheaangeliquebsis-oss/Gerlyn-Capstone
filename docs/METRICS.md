# GerlynPLUS — Metrics & DSS Rule Definitions

Formal definitions for computed metrics/rules that are not self-evident from
a raw model field — the kind a capstone panel or an auditor will ask "how is
that number actually calculated?" about. Each entry states the formula, the
window/thresholds, the data source, and why those particular numbers were
picked, so the rule can be defended rather than just pointed at.

Where a rule is implemented once and read from several screens, that single
implementation is named — nothing here should ever drift out of sync with
the code, because the code is the only place the logic lives.

---

## Non-performing products

**What it answers:** "Which products, sitting on the shelf right now, are
not actually selling?" — a decision-support flag for markdown, return-to-
supplier, or delisting decisions, not an operational stock-level alert (that
is `low_stock_alerts()`, a separate rule below the reorder point).

**Rule:** A product is **Non-Performing** at a branch when, as of today:

```
quantity_on_hand > 0
  AND
SUM(SalesItem.quantity_sold)
    WHERE product, branch match
      AND SalesTransaction.order_status = 'Completed'
      AND SalesTransaction.transaction_date >= today - 30 days
  == 0
```

- **Stock source:** `Inventory.quantity_on_hand` (floor stock only — the
  customer-facing shelf, which is what "not selling" is actually about;
  warehouse-only stock has never been offered for sale yet, so it can't be
  non-performing).
- **Sales source:** `SalesItem.quantity_sold`, joined to its
  `SalesTransaction`, counting **units**, not orders — one large order still
  reads as real velocity. Only `order_status = Completed` counts, the exact
  same `REVENUE_ORDER_STATUSES` filter every other realised-revenue figure
  on the Reports/Dashboard pages uses (`reports/views.py::revenue_sales`) —
  a still-Pending or a voided Cancelled order is not evidence the product
  actually moved.
- **Window:** trailing **30 days** (`NON_PERFORMING_SALES_WINDOW_DAYS` in
  `reports/views.py`). Chosen to match
  `inventory.alerts.EXPIRING_SOON_DAYS`'s existing 30-day precedent — the
  store's working definition of "needs attention soon" — rather than
  introduce a second, unrelated time horizon for staff to learn. A shorter
  window would flag normal slow-but-real sellers (e.g. specialty items that
  turn over monthly); a longer one would delay the flag past the point a
  markdown decision is still useful.
- **Zero-stock exclusion:** a product with `quantity_on_hand == 0` is never
  flagged here even if it hasn't sold in 30 days — it isn't failing to
  sell, it's out of stock, which is `low_stock_alerts()`'s job. Flagging
  both would tell staff to do nothing about a shelf that actually needs
  restocking.

**Implementation:** `reports/views.py::_non_performing_products()`, surfaced
on the Inventory Report (`templates/reports/inventory_report.html`, "Non-
Performing & Near-Expiry Products" table). Branch-scoped via the same
`branch_ids_for_user()` convention every report/alert function follows, so
a Branch Manager/Staff account only ever sees its own branch.

---

## Near-expiry (as used on the Non-Performing Products report)

**What it answers:** which of a branch's floor stock is ageing toward
expiry, reusing the store's one existing expiry rule rather than inventing
a second — a product report and the Alerts Center must never disagree about
what "near-expiry" means.

**Rule:** the nearest **FLOOR** `Batch.expiry_date` for that product/branch
falls into one of `inventory.alerts.expiry_alerts()`'s existing tiers
(`inventory.alerts._expiry_tier()`, thresholds defined by
`EXPIRING_SOON_DAYS = 30` / `APPROACHING_EXPIRY_DAYS = 90`):

| Tier | Days until expiry |
|---|---|
| Expiring Soon | 1–30 days |
| Approaching Expiry | 31–90 days |

- **Data source:** `Batch` (per-lot expiry — the authoritative source; see
  the `Batch` model's own docstring in `inventory/models.py` for why the
  legacy aggregate `Inventory.expiry_date` is not used). Only
  `location = FLOOR` batches count, matching the report's floor-stock scope.
  A product/branch with several batches uses the **nearest** expiry date
  (`MIN(expiry_date)`), since that is the one that forces the next decision.
- **Expired is deliberately excluded** from this report's Near-Expiry flag
  (unlike the Alerts Center, which shows Expired too). An already-expired
  lot's correct next step is the daily write-off sweep — by the time stock
  is expired, "should we still be stocking this" is moot; this report exists
  to catch problems *before* that point.
- A product with no batch-tracked expiry date at all is never flagged
  Near-Expiry (there is nothing to measure).

**Combined status:** a row on the Non-Performing Products report can be
Non-Performing only, Near-Expiry only, or both — a product that is neither
slow-moving nor ageing is not listed at all (same "only show what needs
attention" convention as `low_stock_alerts()`/`expiry_alerts()`).

| Status | Pill |
|---|---|
| Non-Performing + Near-Expiry | `.pill-danger` (both risk factors at once — the strongest signal to act) |
| Non-Performing only | `.pill-warn` |
| Near-Expiry only | `.pill-warn` |

**Implementation:** same function as above,
`reports/views.py::_non_performing_products()`.
