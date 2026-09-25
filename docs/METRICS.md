# GerlynPLUS — Metrics & DSS Rule Definitions

Formal definitions for computed metrics/rules that are not self-evident from
a raw model field, and for every percentage rendered anywhere in the app —
the kind a capstone panel or an auditor will ask "how is that number
actually calculated?" about.

Panel note on percentages specifically: *"Every percentage must have a
defined formula, denominator, comparison period, and data source. Avoid
decorative or arbitrary percentages."* The percentage entries below (§1–10)
were built by grepping every template for a literal `%` render and every
view/filter for a ratio computation, then tracing each one to its source.
**Finding: no arbitrary or undefined percentage was found** — every
percentage already guards its denominator (returns `0`, an empty segment
list, or a worded fallback instead of dividing by zero) and is backed by a
real, traceable formula. This doc makes those formulas explicit and
auditable; see DESIGN.md §2a for the product reasoning behind the
delta-pill rules specifically (the "Absent comparisons" table entry 1
implements).

Percentages are grouped by surface. "Source" names the queryset/model the
numerator and denominator are drawn from; "Code" gives the function that
computes it.

Where a rule is implemented once and read from several screens, that single
implementation is named — nothing here should ever drift out of sync with
the code, because the code is the only place the logic lives.

---

## 1. Period-over-period delta pills (KPI cards)

**Where shown:** Dashboard KPI rail (Total Sales, Avg. Transaction Value, Procurement
Cost, Total Transactions) and Reports → Sales tab (Total Sales, Total Orders, Avg.
Order Value).

**Formula:** `((current − previous) / previous) × 100`, rounded to 1 decimal place.

**Denominator:** the same metric's total over the *previous* window. Can be zero (a
metric posted nothing last window even though the window itself has other activity)
— handled explicitly, see below.

**Comparison period:** the equal-length window immediately preceding the selected
date range (e.g. range = last 30 days → compare against the 30 days before that).
Length is computed by `_previous_window`.

**Denominator-zero / edge-case handling** (`_delta_pill`, `reports/views.py:434`):
- **Previous window has no postings at all** (true of the whole range, so true of
  every metric at once): no pill is rendered on any card. Instead, one line appears
  once under the KPI rail ("Nothing was recorded in the previous N days, so there is
  no change to show yet."). Rendering this as a per-card pill was the original,
  rejected design — it repeated an identical sentence four times.
- **Previous window has data, but this one metric was zero:** that card's pill reads
  "None in the previous N days" (`pill-neutral`) — percentage change from zero is
  left undefined rather than invented as "+100%" or similar.
- **Otherwise:** the real percentage is shown, signed (`+`/`-`), with `pill-ok` for a
  rise and `pill-danger` for a decline. Direction is reported uniformly (up = ok)
  across all four dashboard KPIs, including Procurement Cost, where a rise is a
  business-negative fact rendered with a positive-polarity pill by design (see
  DESIGN.md §2a).

**Source:**
- Dashboard: `_dashboard_metrics` → `_dashboard_range_totals` over
  `revenue_sales(SalesTransaction)` (Completed orders only) and `Procurement`
  filtered to `reports.services.PROCUREMENT_COST_STATUSES` (every status
  except Cancelled, by `order_date`) — see "Reporting metrics" below. This
  used to sum every `Procurement` status including Cancelled; fixed as part
  of the Unify Reporting/Net Revenue pass.
- Reports → Sales: `reports_dashboard` computes the same ratio directly against
  `revenue_sales(sales_qs)`, scoped to the signed-in user's visible branches.

**Card tint (not itself a percentage, but graded off one):** `_delta_tint` recolors
the whole `.kpi-card` `warn` at ≤ −2.0% and `danger` at ≤ −10.0% decline
(`KPI_TINT_WARN_PCT` / `KPI_TINT_DANGER_PCT`, `reports/views.py:493-494`). These are a
documented UI-severity threshold, not a computed metric — recorded here for
completeness since they key off the delta pill's `pct`.

---

## 2. Donut chart segment share (Sales by Branch, Top Products, Sales by Category)

**Where shown:** Dashboard's Sales-by-Branch donut and Top Products donut; Reports →
Sales tab, same panels plus the Sales by Category donut (same underlying `_donut`
helper).

**Formula:** `segment_value / total × 100`, rounded to 1 decimal place.

**Denominator:** the sum of every row feeding the donut (all branches' sales in
range, all products' revenue in range, or all categories' revenue in range — before
ramp-length folding).

**Comparison period:** none — this is a share of the *current* selected range only,
not a period-over-period comparison.

**Denominator-zero handling** (`_donut`, `reports/views.py:1573`): if the total is
zero, the function returns `segments: []` before any division runs, and the template
renders the panel's empty state instead of a ring.

**Source:**
- Sales by Branch: `_sales_by_branch_chart` — `revenue_sales` sales grouped by
  `branch__branch_name`.
- Top Products: `_top_products` — `SalesItem` rows joined off the same
  revenue-scoped `SalesTransaction` queryset, grouped by product (rows with a
  deleted product are kept and labelled "Deleted product" rather than dropped, so
  the ring's total still matches the Total Sales card).
- Sales by Category: `_sales_by_category_chart` — the same revenue-scoped
  `SalesItem` rows, grouped by `product__category`. A line item whose product has no
  category, or whose product was deleted (`SET_NULL`), folds into one "Uncategorized"
  bucket rather than being dropped or split, so the ring's total still matches Total
  Sales. The caption states the exact date range and "% of total sales in that range"
  basis directly on the panel, per the panel note's ask that the period and
  percentage basis be stated, not just implied.

---

## 3. Branch legend status pill (not a percentage itself)

The per-branch "On track" / "Down" / "No prior data" pill next to each donut legend
row (`_branch_legend_rows`, `reports/views.py:710`) compares that branch's sales
total in range against the *same branch* over the previous equal-length window — a
real trend, not a percentage. Listed here only because an earlier, since-removed
version of this rule used a fixed "share below 25% = Watch" threshold — exactly the
kind of decorative/arbitrary percentage this doc's panel note warns against. It no
longer exists in the code; the "Other branches" fold gets no status pill at all,
since which branches it contains can differ between the two windows being compared.

---

## 4. Purchase order status pie (Reports → Procurement)

**Where shown:** Reports → Procurement tab, "PO Status Breakdown" pie.

**Formula:** `status_count / total_pos × 100` per status, used to build the pie's
conic-gradient stops (not printed as on-screen text — the legend states the raw
count, not a %).

**Denominator:** total purchase orders in the filtered range.

**Comparison period:** none — a point-in-time breakdown of the currently filtered
Procurement rows.

**Denominator-zero handling** (`_procurement_status_chart`, `reports/views.py:1907`):
returns `{'total': 0, 'segments': [], 'gradient': ''}` before dividing if there are no
records; the template shows the empty state.

**Source:** `Procurement` queryset, scoped by branch filter and date range, grouped by
`status`.

---

## 5. Procurement spend-split meter (Reports → Procurement)

**Where shown:** Reports → Procurement tab, third panel of the §2c row — "% of
committed spend received".

**Formula:** `received_value / (received_value + active_value) × 100`, rounded to
the nearest whole number.

**Denominator:** `received_value` (sum of `total_cost` for POs with status
`Received`) plus `active_value` (sum of `total_cost` for POs in Draft, Approved,
Incoming, or Partially Received — i.e. committed but not yet fully received).

**Comparison period:** none — a same-range proportion between two figures already
shown as their own KPI cards above it (Active PO Value, Received Value).

**Denominator-zero handling** (`procurement_report`, `reports/views.py:2354`):
`round(...) if (received_value + active_value) else 0` — falls back to `0` when
there are no purchase orders of either kind in range, and the template only renders
the meter when `spend_split.total` is truthy.

**Source:** `Procurement` queryset for the filtered branch/date range.

---

## 6. Billing "% of Revenue" by payment method (Reports → Billing)

**Where shown:** Reports → Billing tab, payment-method breakdown table.

**Formula:** `method_total / total_revenue × 100`, rounded to 1 decimal place.

**Denominator:** `total_revenue` — the sum of `amount_due` across completed billing
records in the filtered range (the same "Completed" scope Billing uses everywhere
else, matching `revenue_sales()`'s definition on the Sales side).

**Comparison period:** none — a same-range composition breakdown.

**Denominator-zero handling** (`_billing_payment_breakdown`, `reports/views.py:1933`):
`round(...) if total_revenue else 0` per row.

**Source:** `BillingRecord` (or equivalent completed-order billing rows), grouped by
`payment_method`.

---

## 7. Inventory value bar chart (Reports → Inventory, Dashboard)

**Where shown:** "Inventory Value by Branch" horizontal bar chart.

**Formula:** `branch_value / chart_max × 100`, where `chart_max` is a "nice" rounded
axis ceiling (`_nice_step`) computed from the highest branch value, not from a
total across branches.

**Denominator:** the chart's own rounded axis maximum — a rendering constant, not a
share of a meaningful total. This value is **never printed as text**; it only sets
each bar's CSS `width`, so no on-screen "%" claim is made about it. Included here for
completeness since it is a `%`-suffixed value flowing into a template.

**Denominator-zero handling** (`_inventory_value_chart`, `reports/views.py:1868`):
returns `{'bars': [], 'ticks': []}` if the peak value is zero, before `_nice_step`
(and its `log10`) ever runs.

**Source:** `_inventory_branch_summary` — inventory valuation (`quantity_on_hand ×
unit_cost`) summed per branch.

---

## 8. Cart free-delivery progress meter (Storefront)

**Where shown:** `orders/cart.html`, the "You're ₱X away from free delivery" meter.

**Formula:** `min(int(cart_total / FREE_DELIVERY_OVER × 100), 100)`.

**Denominator:** `FREE_DELIVERY_OVER` — a fixed business constant (₱500,
`orders/customer_views.py:32`), not a queried value, so it is never zero.

**Comparison period:** none — a threshold-progress indicator, not a trend.

**Denominator-zero / edge case handling** (`orders/customer_views.py:1012`): the
whole expression is short-circuited to `0` when `total` is falsy (empty cart), and
capped at `100` so an over-threshold cart can't overflow the meter past a full bar.
Covered by `orders/tests_storefront_truthfulness.py::test_an_empty_cart_does_not_qualify`.

**Source:** the session cart total (`Decimal`), computed in `orders:cart`.

---

## 9. Bulk-tier discount badge ("X% off") — Product Detail Page

**Where shown:** `partials/_tier_ladder.html`, each bulk-price tier row on the
product detail page.

**Formula:** `(retail_price − tier_price) / retail_price × 100`, rounded to the
nearest whole number (`ROUND_HALF_UP`).

**Denominator:** the product's own retail (single-unit) price.

**Comparison period:** none — a static comparison between the product's two listed
prices (retail vs. this one bulk tier), recomputed on every render rather than
stored, so it can never drift from the prices it describes.

**Denominator-zero/edge-case handling** (`percent_off`, filter,
`inventory/templatetags/shop_extras.py:86`):
- Non-numeric/unparseable input → `0`.
- `retail_price <= 0` → `0` (no division attempted).
- A tier priced at or above retail (a data-entry mistake, not a real discount) →
  `0`, never a negative percentage.

**Source:** `ProductPriceTier.unit_price` vs. `Product.unit_price`.

---

## 10. Fixed statutory/business rates shown as "%" (not computed ratios)

**Where shown:** Walk-in Sale (POS) form — "VAT (12%, included)" and "20% off the
VAT-exclusive price" next to the PWD/Senior discount checkbox.

**Formula:** these are not ratios of two queried numbers — they are fixed rate
constants rendered as their percentage form: `int(VAT_RATE × 100)` and
`int(PWD_SENIOR_DISCOUNT_RATE × 100)` (`sales/views.py:155-156`).

**Denominator:** not applicable (not a fraction of two other displayed numbers).

**Comparison period:** not applicable — a static rate, not a trend.

**Source of the rate itself** (`sales/pricing.py:33-37`):
- `VAT_RATE = Decimal('0.12')` — the Philippine statutory VAT rate. Comment notes a
  change would be a legislative event requiring its own migration of historical
  rows, so it is deliberately not a per-deployment setting.
- `PWD_SENIOR_DISCOUNT_RATE = Decimal('0.20')` — the statutory PWD/Senior Citizen
  discount rate (both statutes set the same 20%).

These can never divide by zero because they are not divisions; they are documented
here to satisfy "every percentage on screen has a stated source," per the panel note.

---

## Summary table (percentages)

| # | Percentage | Formula | Denominator can be 0? | Zero handling | Comparison period | Source |
|---|---|---|---|---|---|---|
| 1 | KPI delta pills | (current−previous)/previous ×100 | Yes | No pill / "None in previous N days" | Previous equal-length window | `SalesTransaction`, `Procurement` |
| 2 | Donut segment share | value/total ×100 | Yes | Empty segments, panel empty state | None (current range only) | `SalesTransaction`, `SalesItem` |
| 3 | Branch legend status | n/a (trend pill, not %) | n/a | n/a | Same branch, previous window | `SalesTransaction` |
| 4 | PO status pie | count/total ×100 | Yes | Empty segments | None (current range only) | `Procurement` |
| 5 | Procurement spend split | received/(received+active) ×100 | Yes | Falls back to 0 | None (current range only) | `Procurement` |
| 6 | Billing % of revenue | method_total/total_revenue ×100 | Yes | Falls back to 0 per row | None (current range only) | `BillingRecord` |
| 7 | Inventory value bar width | value/chart_max ×100 | Yes | Empty chart if peak is 0 | None (CSS width only, not displayed as text) | Inventory valuation |
| 8 | Cart free-delivery progress | cart_total/₱500 ×100, capped at 100 | No (fixed constant) | 0 on empty cart | None (threshold, not trend) | Session cart |
| 9 | Tier discount badge | (retail−tier)/retail ×100 | Yes | 0 if retail ≤ 0 or tier ≥ retail | None (static price comparison) | `ProductPriceTier`, `Product` |
| 10 | VAT / PWD-Senior rate | fixed constant ×100 | n/a | n/a | n/a (static rate) | `sales/pricing.py` |

---

## Reporting metrics: Gross Sales, Refund Value, Net Sales, Pipeline Value, Procurement Cost

**What it answers:** the Dashboard and Reports pages, and their same-origin
DRF mirrors under `/api/reports/`, used to each keep their own copy of
"what counts as revenue" and drifted apart — the API summed every
`order_status` into revenue (Pending and Cancelled included), never
subtracted a recorded refund from a Completed order's total, read the
stored (and potentially stale) `Inventory.restock` flag for low stock
instead of a live query, and summed `Procurement.total_cost` across every
status including Cancelled. `reports/services.py` is now the one place all
five figures below are defined; `reports/views.py` and
`reports/api_views.py` both import it rather than recomputing any of this
themselves.

**Definitions** (`reports/services.py`):

| Metric | Formula | Notes |
|---|---|---|
| Gross Sales | `SUM(total_amount)` over `SalesTransaction` rows with `order_status = Completed` | A Pending/Approved/Preparing order has not been paid for or fulfilled; a Cancelled order is void. Neither counts. |
| Refund Value | `SUM(Refund.total_amount)` for refunds against orders in scope | A refund never changes `order_status` (see `sales.models.Refund`), so a refunded order is still "Completed" and still inside Gross Sales — this is what has to be subtracted back out. |
| Net Sales | Gross Sales − Refund Value | The realised-revenue figure rendered as "Total Sales" / "Total Revenue" on the Reports Sales tab (`reports_dashboard`) and the Billing Report (`billing_report`). |
| Pipeline Value | `SUM(total_amount)` over `SalesTransaction` rows with `order_status` in Pending/Approved/Preparing | Committed but unfulfilled. Shown as its own figure, never added into Gross/Net Sales. |
| Procurement Cost | `SUM(total_cost)` over `Procurement` rows excluding `status = Cancelled` | Draft counts (matches the existing "committed value" convention already used by `Procurement.OPEN_STATUSES`, the Dashboard's Pending POs count, and `procurement_report`'s `active_value`); only Cancelled — which never happened — is excluded. |
| Low Stock count | `COUNT(*)` over `Inventory` rows where `quantity_on_hand <= reorder_level` | The same live predicate `inventory.alerts.low_stock_alerts()` ranks (`inventory.alerts.low_stock_filter`), never the stored `Inventory.restock` flag — that flag is only ever recomputed inside `Inventory.save()`, so a bulk update, fixture, or raw query can leave it stale with nothing to catch it. |

**Aggregation:** every SUM above runs as a single Decimal SQL `SUM()` via
`django.db.models.functions.Coalesce` (`reports/services.py::_decimal_sum`),
not a Python-side loop — an empty/no-match queryset returns `Decimal('0.00')`
from the database.

**Branch/date scoping:** `reports/services.py::scoped_report_querysets` is
the one function that resolves which branch(es) a user may see (Owner:
all, or an explicit `?branch=` choice; Manager/Staff: their own branch
only, failing closed for an unassigned account) — both `reports_dashboard`/
`operational_dashboard` (via the `_scoped_querysets` alias) and
`DashboardSummaryAPIView` call it, so a template view and its API mirror
can never apply different branch rules to the same user. Date-range
scoping (the shared §2e control — `range`, `date_from`, `date_to` via
`core.timebounds.range_from_request`) is applied by each caller on top of
that, identically on both sides for `DashboardSummaryAPIView` and
`reports_dashboard`.

**Where rendered:**
- `reports_dashboard` (Reports → Sales tab): `total_sales` = Net Sales,
  `avg_order_value` = Gross Sales ÷ order count (deliberately gross — an
  average-order-size figure blended with refunds that don't line up
  one-to-one with the window's own orders would be misleading),
  `pipeline_total` = Pipeline Value. `gross_sales` / `refund_value` are
  also carried in context (not yet rendered by the template) for
  auditability and the parity tests below.
- `billing_report` (Reports → Billing tab): `total_revenue` = Net Sales
  for the completed billing records in range; the payment-method breakdown
  table nets each row by that record's own transaction's refunds too, so
  the rows keep summing to `total_revenue` exactly.
- `operational_dashboard` (main Dashboard): the Procurement Cost KPI/trend
  (`_dashboard_daily_series`) now filters to Procurement Cost's status set;
  the Total Sales KPI stays Gross (not net of refunds) — a deliberate scope
  boundary, not an oversight: netting the per-day trend would require
  bucketing refunds by day too, a larger change to an already
  invariant-heavy view (see DESIGN.md §2a). The reactive Low-Critical Items
  count (`_dashboard_inventory_metrics`) and `inventory_report`'s own count
  both now read the live low-stock predicate.
- `DashboardSummaryAPIView` (`/api/reports/dashboard-summary/`): exposes
  all five as `grossSales` / `refundValue` / `netSales` / `pipelineValue` /
  `totalProcurementCost`, plus `lowStockCount`. `totalSales` and
  `totalTransactions` are kept as field names for backward compatibility
  but now mean Net Sales and the Completed-order count respectively, not
  "every order_status".

**Daily Sales Report (P13):** the plain Sales Report page (`/reports/sales/`,
`sales_report`), its JSON mirror `SalesReportAPIView`
(`/api/reports/sales-report/`) and the sales CSV export all read
`reports.services.scoped_sales_in_range` for rows and
`sales_range_totals` for figures, so page, API and export cannot list or
total different rows. Filters: `range` = `today | yesterday | 7 | 30 | all |
day | custom`, `date=YYYY-MM-DD` for one day (implies `range=day`),
`date_from`/`date_to` for a range (imply `range=custom`), and `branch` for
an Owner. A day is the local (Asia/Manila) span 00:00:00 through
23:59:59.999999, judged as a half-open comparison on aware datetimes. A
range the request names but that cannot be honoured (unknown preset, missing
/ malformed / inverted date) answers **400** on both reports APIs, and shows a
warning + the 30-day fallback on the page; the CSV redirects with an error.
The CSV is narrowed only by an *explicit* range in the URL — never by the
session's last selection — so a bare `/reports/export/sales.csv` is still the
full export. Response shape: `{activeRange, rangeStart, rangeEnd, count,
totals{transactions, orders, grossSales, refundValue, netSales,
avgOrderValue, pipelineValue, pipelineCount}, results[]}` (Decimals as
strings). Tests: `reports/tests_daily_sales_filter.py`.

**Parity tests:** `reports/tests.py::WebApiReportingParityTests` builds a
Completed order that is partially refunded, a Cancelled order, a Pending
order, purchase orders in Received/Approved/Cancelled status, and one
Inventory row with a deliberately wrong stored `restock` flag — then
asserts `reports_dashboard`, `billing_report`, `procurement_report`,
`inventory_report`/`operational_dashboard`, and `DashboardSummaryAPIView`
all agree on Gross Sales, Refund Value, Net Sales, Pipeline Value,
Procurement Cost, and the Low Stock count for the same user/scope.

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

---

## Maintenance note

If a new percentage, metric, or DSS rule is added anywhere in the app, add an entry
to this document (formula/rule, denominator or inputs, zero/edge-case handling,
comparison period, data source) in the same pull request. An undocumented
percentage or rule should be treated as a review blocker, not an oversight to fix
later.
