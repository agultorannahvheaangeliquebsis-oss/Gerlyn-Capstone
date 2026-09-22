# GerlynPLUS — Metrics & Percentage Definitions

Panel note: *"Every percentage must have a defined formula, denominator, comparison
period, and data source. Avoid decorative or arbitrary percentages."*

This document is the answer — one entry per percentage rendered anywhere in the app,
each with its exact formula, what the denominator is (and what happens when it's
zero), what it's compared against, and which model/queryset it comes from. It was
built by grepping every template for a literal `%` render and every view/filter for a
ratio computation, then tracing each one to its source.

**Finding: no arbitrary or undefined percentage was found.** Every percentage in the
codebase already guards its denominator (returns `0`, an empty segment list, or a
worded fallback instead of dividing by zero) and is backed by a real, traceable
formula. This doc makes those formulas explicit and auditable; see DESIGN.md §2a for
the product reasoning behind the delta-pill rules specifically (the "Absent
comparisons" table this doc's entry 1 implements).

Percentages are grouped by surface. "Source" names the queryset/model the numerator
and denominator are drawn from; "Code" gives the function that computes it.

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
  `revenue_sales(SalesTransaction)` (Completed orders only) and
  `Procurement` (all statuses, by `order_date`).
- Reports → Sales: `reports_dashboard` computes the same ratio directly against
  `revenue_sales(sales_qs)`, scoped to the signed-in user's visible branches.

**Card tint (not itself a percentage, but graded off one):** `_delta_tint` recolors
the whole `.kpi-card` `warn` at ≤ −2.0% and `danger` at ≤ −10.0% decline
(`KPI_TINT_WARN_PCT` / `KPI_TINT_DANGER_PCT`, `reports/views.py:493-494`). These are a
documented UI-severity threshold, not a computed metric — recorded here for
completeness since they key off the delta pill's `pct`.

---

## 2. Donut chart segment share (Sales by Branch, Top Products)

**Where shown:** Dashboard's Sales-by-Branch donut and Top Products donut; Reports →
Sales tab, same two panels (same underlying `_donut` helper).

**Formula:** `segment_value / total × 100`, rounded to 1 decimal place.

**Denominator:** the sum of every row feeding the donut (all branches' sales in
range, or all products' revenue in range, before ramp-length folding).

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

**Denominator-zero / edge-case handling** (`percent_off`, filter,
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

## Summary table

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

## Maintenance note

If a new percentage is added anywhere in the app, add a row to this document (formula,
denominator, zero handling, comparison period, source) in the same pull request. A
percentage without an entry here should be treated as a review blocker, not an
oversight to fix later.
