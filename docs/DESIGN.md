# GerlynPLUS — Design Document

> **How to use this doc with Claude Code:**
> Save this file at `docs/DESIGN.md` in your repo root. Reference it in prompts like:
> *"Read docs/DESIGN.md. [Specific task here]."*
> Tackle one section at a time — see suggested task order at the bottom.

---

## Section 1: Information Architecture

Status: settled — adapted to the actual Django codebase (not the Figma prototype).

---

### Dashboard

- KPI tiles: Total Products, Low/Critical Stock, Total Sales, Avg. Transaction Value, Procurement Cost, Total Transactions
- Sales by Branch donut chart (also anchors the Reports multi-panel row, see §2c)
- Top-Selling Products list
- Low Stock preview table (compact, read-only) — links to Alerts Center via "View all alerts →" instead of being its own independent implementation
- Recent Activity feed — latest Sale/PurchaseOrder/BillingRecord postings, unscoped by the date-range control (see §2f)
- Open Orders table *(reframed from "Recent Orders")* — non-terminal orders only, oldest-first, so it answers "what needs action" rather than repeating §2f's recent sales (see §2g)
- Branch Status panel — today's pace against each branch's own 7-day baseline plus stock on hand; a live reading on a different axis from Needs Attention, not another checklist (see §2h)

---

### Inventory

- **Products** *(currently "Product Master List")* — product catalog. Categories folds into this page as a filter/tab instead of its own sidebar item
- **Categories** — to be folded into Products as above; remains a separate nav item only until the merge is implemented
- **Stock per Branch** — real-time inventory levels across branches
- **Stock Adjustments** *(currently "Withdrawal Slips")* — inter-branch transfers and write-offs. Renamed for clarity.

---

### Procurement

- **Suppliers** *(Owner/Manager only)*
- **Purchase Orders** — includes "Create PO" action; low-stock items in Alerts Center link here via per-row "Create PO" buttons
- **Deliveries** — receiving and receiving history

---

### Financials

- **Sales & Orders** — unified transaction list (online + walk-in), filterable by branch and channel
- **Billing** — audit trail / system of record for financial transactions

**Sales & Orders vs. Billing:**
Sales & Orders is a *workflow* view — transactions in progress (accept, fulfill, mark paid).
Billing is an immutable *ledger* — every completed transaction as a financial record for audit.
Same underlying data, different lens: Sales is "what still needs my action," Billing is "what already happened, for the books."

Billing improvements still needed:
- **Outstanding Balances** view for wholesale/credit customers
- Records are created automatically at sale and updated only via payment-confirmation — no manual editing, to preserve audit integrity
- Export CSV/PDF matching the pattern already used in Reports

---

### System & Monitoring

- **Reports** — currently uses in-page tabs (Sales, Inventory, Procurement, Billing). Consider converting to sidebar sub-items for consistency with Inventory/Procurement, but not required — the tab pattern is already well-built with its own `.report-tabs` CSS component.
- **Alerts** — keeps both tabs:
  - **Low Stock Alerts** — the full actionable view with Qty, Reorder, Suggested, Risk, and per-row "Create PO." This is the canonical restock surface.
  - **Expiry Alerts** — items approaching expiration

The Dashboard's low stock table is a compact preview that should link here via "View all alerts →" rather than being independent.

---

### Administration *(Owner only)*

- Branch Management
- User Management

---

### Footer nav items (pinned below sidebar scroll)

- **View Store** — opens customer-facing storefront in new tab
- **Logout**

---

### Cross-cutting concerns

**Role-based nav visibility (already partially implemented):**

| Role | Visible sections |
|---|---|
| Staff (cashier) | Dashboard, Financials → Sales & Orders, Inventory → Stock per Branch only |
| Manager | Everything except Administration |
| Owner | Everything including Administration |

Current code already gates: Suppliers (Owner/Manager only), Administration (Owner only). Staff visibility for remaining sections needs tightening.

**Global branch scope:** The topbar's branch chip is currently read-only. Making it a real dropdown that filters the whole app at once would replace the per-page branch dropdowns. Not a v1 requirement but a strong future enhancement.

**Unified notifications:** The topbar bell (if added) should read from the same data source as Alerts Center — not hardcoded values.

**Future enhancement — global search:** `⌘K`-style search spanning products, POs, orders, suppliers. Not in current scope.

---

### Cleanup tasks (Inventory)

- [ ] Rename "Withdrawal Slips" → "Stock Adjustments" in nav and templates
- [ ] Fold Categories into Products page as a filter/tab (when ready)
- [ ] Add "View all alerts →" link from Dashboard low stock table to Alerts Center
- [ ] Migrate Owner Dashboard from Bootstrap utility classes to `gerlyn.css` tokens and components

---

## Section 2: Visual Design System

Status: settled. The codebase already has a proper design token system in `gerlyn.css` — Section 2 documents what exists, confirms what's correct, and specifies improvements.

---

### Design tokens (confirmed — do not change)

```css
/* Green family */
--brand:      #2E9E6B
--action:     #1E7A4F
--action-700: #14603C
--green-50:   #EAF6F0
--green-100:  #E3F3EB
--green-300:  #9CCDB4

/* Text */
--ink-900: #17251E   /* headings, data */
--ink-700: #3E4E45
--ink-600: #56655D   /* secondary text */
--ink-400: #8A968F   /* labels, muted */

/* Surfaces */
--bg:         #F5F7F6
--surface:    #FFFFFF
--surface-2:  #FAFCFB
--line:       #E3E9E5
--line-soft:  #EFF3F0
--input-line: #C9D4CE

/* Semantic status */
--ok-bg:     #E3F3EB   --ok-fg:     #1E7A4F   --ok-dot:     #2E9E6B
--warn-bg:   #FCF0D4   --warn-fg:   #7C4A05   --warn-dot:   #B45309
--danger-bg: #FDE8E6   --danger-fg: #9A1C12   --danger-dot: #B42318
--info-bg:   #EAF6F0   --info-fg:   #14603C   --info-dot:   #2E9E6B

/* Spacing */
--s1: 4px   --s2: 8px   --s3: 12px  --s4: 16px
--s6: 24px  --s8: 32px  --s12: 48px

/* Radius */
--r-input: 8px
--r-card:  12px
--r-pill:  999px

/* Touch & shadow */
--touch:       44px
--shadow-card: 0 1px 3px rgba(23,37,30,.06)
--shadow-hero: 0 2px 10px rgba(23,37,30,.08)
```

---

### Existing components (confirmed — do not rebuild)

| Component | Description |
|---|---|
| `.btn` / `.btn-primary` / `.btn-quiet` / `.btn-danger` / `.btn-danger-solid` / `.btn-sm` | Full button system, already uses tokens |
| `.pill-ok` / `.pill-warn` / `.pill-danger` / `.pill-info` / `.pill-neutral` | Status pills with dot+label, accessible for color-blind users |
| `.gv-table` | Sticky headers, zebra rows, right-aligned `.num` columns, responsive stacking at ≤860px |
| `.stat` / `.stat-hero` | Two-tier KPI cards (quiet flat vs. elevated hero) |
| `.kpi-card` / `.kpi-card.highlight` / `.kpi-card.danger` / `.kpi-card.warn` | Report KPI cards with icon, value, label, and status-tinted variants |
| `.toolbar` / `.toolbar-card` | Filter bars with search + dropdowns + export actions |
| `.tabs` / `.report-tabs` | Two distinct tab styles (pill tabs for filtering, underline tabs for report sections) |
| `.banner-ok` / `.banner-warning` / `.banner-error` / `.banner-info` | Toast/alert banners |
| `.empty` | Empty state with glyph, heading, description |
| `.meter` | Progress/proportion bars |
| Sidebar | Collapsible nav with icon-only collapsed state, role-based gating, mobile bottom nav |
| Responsive | ≤860px breakpoint, table-to-card stacking, bottom nav, avatar admin menu |
| Print | `@media print` hides nav/toolbar, cleans tables for PDF export |

---

### Improvements needed

**1. Owner Dashboard — migrate to design system**
Currently uses Bootstrap classes (`row`, `col`, `fs-4`, `fw-bold`, `stat-card`, `table-sm`) instead of `gerlyn.css` components used everywhere else.

Target components:
- `.stat-strip` for the KPI row
- `.stat-hero` + `.stat` for cards
- `.gv-table` for data tables
- `.kpi-card` for enhanced metrics

**2. Dashboard KPI cards — upgrade top row to rich cards**
The current dashboard cards are basic (icon + label + number). For the top 3–4 hero metrics only, add inline mini-visualizations:

| Metric type | Mini-visual |
|---|---|
| Trend over time (sales, revenue) | Sparkline with end-point dot + delta callout |
| Distribution / outlier (low-stock spread) | Bar mini-chart |
| Two-way split (in-stock vs. low) | Horizontal proportion bar |

All other KPI surfaces (Reports, Alerts stat strips) keep the existing simple `.kpi-card` / `.stat` pattern. Rich cards are Dashboard-only.

**Superseded for the four top-row hero metrics (Total Sales, Avg. Transaction Value, Procurement Cost, Total Transactions):** §2a's shared single-graph panel replaces per-card mini-visualizations for these four. No sparkline is embedded in the cards themselves — each card carries its figure, a delta pill, and a one-line context note, and the trend lives in the one panel below that the cards tab into. This follows the same reasoning as §2a's "cards act as tabs into it, not four separate charts": a sparkline on every card plus the shared graph would be five renderings of the same series.

The mini-visual table above still governs any KPI surface *outside* the click-to-expand pattern — a card that has no shared panel to open still earns its own inline sparkline, bar, or proportion bar by metric type.

**2a. KPI → own-graph pattern (Dashboard, click-to-expand)**
Reference: stakeholder-liked pattern where a KPI strip sits above a large graph, and selecting a different KPI swaps the graph below it to match.

Adapt as:
- Row of `.stat-hero` cards (Total Sales, Avg. Transaction Value, Procurement Cost, Total Transactions)
- Clicking a card sets it `.active` (green-tinted border/background using `--green-50` / `--brand`, not a dark highlight) and renders its trend in a single graph panel beneath the row
- Only one graph panel — cards act as tabs into it, not four separate charts
- Reuses `.card` container + sparkline SVG already planned for #2; no new charting library needed if a simple line/area SVG covers it
- Avg. Transaction Value's line plots only days with actual transactions — no interpolation, no zero-fill for no-transaction days, no rolling average. The KPI card's value and the chart's displayed total must agree exactly, since both are the same unsmoothed range total.

**2b. "Needs attention" list — formalize existing pattern**
This already exists conceptually as the Dashboard's low-stock preview and the Alerts Center. Reference inspo showed it as a clean icon + label + count-badge + chevron list.

Adapt as:
- Dashboard keeps a compact `.card` version, max 5 rows: Low Stock, Expiring Soon, Pending POs, Unfulfilled/Pending Orders, Overdue Billing — each with a `.pill` count badge and trailing chevron
- Zero-count checks collapse into a compact "All clear" chip instead of a full row — only checks with a nonzero count get a full row with icon, label, count badge, chevron, and outbound link
- Every actionable row links out: Low Stock/Expiring → Alerts Center tabs, Pending POs → Purchase Orders, Pending Orders → Sales & Orders, Overdue Billing → Billing's Outstanding Balances view
- Retains "View all alerts →" link already specced in Cleanup tasks — this list is that preview, formalized
- Alerts Center's own Low Stock Alerts tab is the full, non-compact version of this same row pattern (icon, label, count) but with the existing Qty/Reorder/Suggested/Risk columns intact — no change to that table

**2c. Sales-by-Branch donut (Dashboard) + multi-panel "informative" row (Reports)**
Reference: donut chart + sparkline + spend-vs-return proportion bar shown together in one dense row.

Adapt as:
- The Sales-by-Branch donut lives on the Dashboard (see Section 1), replacing the old "Sales by Branch table" — it's the one chart exception on an otherwise chart-light landing view, justified because a donut reads faster than a table at a glance
- The same donut also anchors the Reports multi-panel row below, so it isn't a one-off component
- Reports' Sales and Procurement tabs keep the full three-panel row, not just the donut: three `.card` panels side by side on desktop, stacking at ≤860px per existing responsive rules — the donut/breakdown (sales by branch or category), a daily trend sparkline, and a spend-vs-return `.meter` bar
- Built from existing primitives (`.card`, `.meter`, sparkline SVG) — no new dependency

**2d. Funnel visualization — Sales & Orders only, optional**
Reference: views → cart → checkout → purchase funnel with percentage retention at each stage.

Adapt as:
- Optional panel on Sales & Orders showing the order lifecycle funnel: Created → Fulfilled → Paid (or Created → Accepted → Fulfilled → Paid, depending on what stages the workflow tracks)
- Light-mode treatment: stacked horizontal bars in `--green-100` → `--brand` gradient steps rather than dark-mode grayscale, with `--ink-900` percentage labels
- Lower priority than 2a/2b — flag as nice-to-have, not part of the initial task order below

**2e. Date range control — standardize across Dashboard and Reports**
Reference: calendar dropdown with preset ("Last 7 days") + custom range + "Customize" affordance.

Adapt as:
- Single date-range component (`templates/_daterange.html`) added to `.page-head` or `.toolbar-card`, used by Dashboard and the Reports tabs that have a date axis (already flagged in Component rules: "All date filtering should use the same date-range control pattern" — this is that component, now specified)
- Presets: Today, Last 7 days, Last 30 days, This month, All time, Custom range
- "All time" resolves to an open-ended lower bound (`start = None`) and callers drop their `__gte` term entirely — deliberately not a `MIN()` across the tables, since nothing needs the earliest record just to show unbounded history. It is offered on the Reports tabs but **not** on the Dashboard, which buckets one entry per day across the range and compares it against a previous window of equal length; neither is defined without a lower bound. The Dashboard enforces that server-side (`range_from_request(allow_all=False)`), not by hiding the chip, so a hand-typed `?range=all` cannot reach the day-bucketing. A clamp there does not overwrite the stored selection, so Reports → Dashboard → Reports returns to All time rather than being quietly downgraded
- Built on `<details>`/`<summary>` like the topbar avatar menu, so it needs no JavaScript — disclosure, keyboard handling and screen-reader semantics come from the element, and the control still works if JS fails. Presets are submit buttons in one GET form, so presets and the custom Apply share one mechanism
- Custom range uses native `<input type="date">` rather than a hand-built calendar grid. The inspo's styled month grid would be ~200 lines of new JS and a new component with no precedent in this codebase; the native control brings its own calendar, keyboard and locale handling, and already appears in the Procurement/Billing toolbars. This is a deliberate deviation from the reference
- Selection persists per session (`session['date_range']`), mirroring the existing `session['language']` / `session['branch_id']` precedent. URL params always win over the stored value, so a copied link reproduces exactly what the sender saw
- Legacy `?month=YYYY-MM` links are aliased to that month's equivalent custom range rather than falling through to the default window — a silent wrong-figures failure otherwise

**Surfaces exempt, and why.** §2e originally said "Dashboard, Reports and Alerts". Two of those have no date axis to control, so the control is deliberately absent rather than inert:
- **Alerts Center** — low stock is *current* quantity vs. reorder level and expiry is a fixed `EXPIRY_WINDOW_DAYS` lookahead. Neither is a historical range, so a Today/7/30 control would either do nothing or silently redefine what "low stock" means
- **Reports → Inventory** — point-in-time stock levels, same reasoning
- **Branch Status (§2h)** and **Recent Activity (§2f)** on the Dashboard are also unscoped by it, per their own sections: both answer "right now", not "over the selected window"

**2f. Recent Activity feed (Dashboard, new)**
Introduced by the approved mockup, not originally specced. A live feed of the latest postings across Sale, PurchaseOrder, and BillingRecord — icon by type, branch, amount, relative timestamp, staff attribution where available. Directly answers Chapter 1's stated problem of absent digital audit trails.

Adapt as:
- Compact `.card` panel, ~5 most recent postings, newest first
- NOT scoped to the Dashboard date-range control — always shows the latest activity regardless of the selected range, since its purpose is "what just happened," not historical reporting
- Reuses `.tx-row`-equivalent structure built from existing primitives; no new component needed if a simple flex row covers it

**2g. Open Orders (Dashboard, reframed from Recent Orders)**
Originally a plain "last 6 orders" table, which duplicated Recent Activity's (§2f) coverage of recent sales without adding anything beyond order status. Reframed to filter to non-terminal statuses and sort oldest-first, so it answers "what needs action" rather than "what happened recently" — those are now two distinct, non-overlapping panels instead of one redundant with the other.

Adapt as:
- Filter to non-terminal statuses only: Pending, Approved, Preparing — never Completed or Cancelled
- Sort oldest-first: the longest-waiting order is the most actionable signal in a workflow view, so it leads
- Columns and both links unchanged from the table it replaces — customer, branch, Channel pill, total, Status pill; header "View all →" to Sales & Orders, order number to the order detail
- Capped at 8 rows — a dashboard preview, not the full list
- Zero open orders is a real success state, not an empty table: use the `.empty` component (glyph, heading, description)

**2h. Branch Status (Dashboard)**
Two-mode panel (tinted alert cards / plain neutral list) driven by `_branch_status_rows()` — today's sales vs. a trailing 7-day baseline, plus low-stock counts reused from the restock query. Deliberately disjoint from Needs Attention (different signals, no shared query) and from the branch donut (today+baseline vs. the selected date range, matching the reasoning already in reports/views.py:954). Its tinted state is a live pace-or-stock reading, not an actionable checklist item — labeled as such to avoid reading as a 6th unlisted Needs Attention check.

Adapt as:
- Both modes carry the same "Branch Status" heading and a subtitle naming the axis, so the panel is identifiable in either state
- Each tinted card names the signal that actually tripped it beneath the status pill. This is **not one fixed caption**: `watch` has two independent triggers (`branch_alerts or sales_slow`), so a branch can go amber on stock alone with its pace perfectly healthy, and a blanket "pace" caption would mislabel every one of those cards. `_branch_status_rows()` resolves `signal_label` to one of five, in priority order:

| Condition | Status | Label |
|---|---|---|
| `critical_count` | `critical` | Critical stock on hand |
| `sales_slow` **and** low stock | `watch` | Behind pace · low stock |
| `sales_slow` only | `watch` | Pace vs. 7-day baseline |
| low stock only | `watch` | Low stock on hand |
| neither | `ok` | Keeping pace |

- The sales comparison is pro-rated by how much of the local day has elapsed and suppressed before ~6am, so a branch is never called slow at 8am for not having taken a full day's sales. That suppression is also the only way the "low stock only" and "Keeping pace" arms are reachable for a branch whose trailing baseline exceeds its takings so far

**3. Status color rule — enforce consistently**
The tokens are correct but need a written rule:

> **Amber/warning means "warning status" only — never use it for action buttons.**
> A "Create PO" button is always `--action` (green primary), never orange/amber.
> Status color and action color are separate systems.

**4. Typography — consider Inter**
Current: `system-ui`. Works fine and avoids font-loading latency. If the team wants a more distinctive look, switch to Inter (open-source, humanist, wide weight range). Not a priority — only change if actively wanted.

**5. Sidebar icon-rail improvements**
The collapsed state already works as an icon-only rail. Potential enhancement: add flyout-on-hover to reveal sub-items without expanding the full sidebar. Not a v1 requirement.

*(Note: stakeholder inspo referenced a hover-to-reveal icon rail as a liked pattern — Gerlyn's sidebar already implements the collapsed icon-only rail described in "Existing components." The flyout-on-hover sub-item behavior above is the only piece not yet built; no other change needed here.)*

**6. Breadcrumbs**
Add a compact breadcrumb bar under the topbar for routes deeper than one level (e.g. Purchase Orders → PO-2026-003). Not currently implemented anywhere.

---

### Component rules (for new development)

**Buttons**
- One `.btn-primary` per screen (the primary action)
- Secondary actions → default `.btn`
- Tertiary/cancel → `.btn-quiet`
- Destructive → `.btn-danger` (outlined) until final confirm, then `.btn-danger-solid`

**Cards**
- Metrics → `.kpi-card`
- Content panels → `.card`
- Never hand-roll a card with raw `bg-white rounded-xl shadow-sm` — use the component classes

**Tables**
- Always `.gv-table` with `.num` on number columns
- Include `data-label` on every `<td>` for mobile stacking
- Right-align all monetary and quantity values

**Status pills**

| State | Pill class |
|---|---|
| In Stock / Paid / Received | `.pill-ok` |
| Low Stock / Pending / Partial | `.pill-warn` |
| Critical / Overdue / Cancelled | `.pill-danger` |
| Ordered / In Transit | `.pill-info` |
| Draft | `.pill-neutral` |

**Filters**
Use `.toolbar` or `.toolbar-card` with consistent control patterns. All date filtering should use the same date-range control pattern.

**Formatting standards**

| Type | Format |
|---|---|
| Currency | `₱` prefix, comma-separated thousands, right-aligned |
| Dates | `YYYY-MM-DD` (matches Django default, consistent across system) |
| Numbers | Right-aligned, tabular-nums (`.num` class) |

---

## Suggested task order for Claude Code

Start small — each task is independently shippable:

1. **Rename "Withdrawal Slips" → "Stock Adjustments"** in nav and templates *(lowest risk, good smoke test)*
2. **Add "View all alerts →" link** from Dashboard low stock table to Alerts Center
3. **Migrate Owner Dashboard** from Bootstrap classes to `gerlyn.css` components
4. **Tighten Staff role nav visibility** (hide Inventory sections except Stock per Branch, hide Procurement, hide Administration)
5. **Upgrade top-row Dashboard KPI cards** to rich cards with mini-visualizations
6. **Add click-to-expand behavior** to Dashboard KPI cards (2a) so the graph panel swaps per selected card
7. **Formalize "Needs attention" list** (2b) on Dashboard as a distinct `.card` component, linking out to Alerts/POs/Orders/Billing
8. **Add Recent Activity feed** (2f) to Dashboard — can run in parallel with tasks 5/6, since it's unscoped by the date-range control and has no dependency on the date-resolver work those tasks need
9. **Reframe Recent Orders as Open Orders** (2g) — depends on task 8, since the reframe is what stops the two panels overlapping
10. **Build standardized date-range control** (2e) and wire into Dashboard + Reports + Alerts toolbars
11. **Add breadcrumbs** under topbar for deep routes
12. **Fold Categories into Products** as a filter/tab (when team is ready)
13. *(Lower priority, optional)* **Reports multi-panel row** (2c) and **Sales & Orders funnel** (2d)
