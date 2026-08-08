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
--ink-500: #627069   /* muted TEXT — prefer this one */
--ink-400: #8A968F   /* NON-TEXT only: chevrons, icon glyphs, borders */

/* Surfaces */
--bg:         #F5F7F6
--surface:    #FFFFFF
--surface-2:  #FAFCFB
--line:       #E3E9E5
--line-soft:  #EFF3F0
--input-line: #C9D4CE

/* Semantic status */
--ok-bg:     #E3F3EB   --ok-fg:     #1E7A4F   --ok-dot:     #2E9E6B
--warn-bg:   #F6EFE2   --warn-fg:   #7D5A32   --warn-dot:   #B98452
--danger-bg: #FDE8E6   --danger-fg: #9A1C12   --danger-dot: #B42318
--info-bg:   #EAF6F0   --info-fg:   #14603C   --info-dot:   #2E9E6B

/* CTA on a status tint (§3) — verify numerically before adding one */
--warn-cta:   #8F5A2B          /* 5.01:1 on --warn-bg */
--danger-cta: var(--danger-dot) /* 5.59:1 on --danger-bg */

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
| `.pill-ok` / `.pill-warn` / `.pill-danger` / `.pill-info` / `.pill-neutral` | Status pills with a full text label, accessible for color-blind users (the label does that work, not colour) |
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

**Superseded for the four top-row hero metrics (Total Sales, Avg. Transaction Value, Procurement Cost, Total Transactions):** §2a's shared single-graph panel replaces per-card mini-visualizations for these four. No sparkline is embedded in the cards themselves — each card carries its figure, a one-line context note, and a delta pill *when there is a change to state* (see §2a's "Absent comparisons" — the pill is conditional, not fixed furniture), and the trend lives in the one panel below that the cards tab into. This follows the same reasoning as §2a's "cards act as tabs into it, not four separate charts": a sparkline on every card plus the shared graph would be five renderings of the same series.

The mini-visual table above still governs any KPI surface *outside* the click-to-expand pattern — a card that has no shared panel to open still earns its own inline sparkline, bar, or proportion bar by metric type.

**2a. KPI → own-graph pattern (Dashboard, click-to-expand)**
Reference: stakeholder-liked pattern where a KPI strip sits above a large graph, and selecting a different KPI swaps the graph below it to match.

Adapt as:
- Row of `.stat-hero` cards (Total Sales, Avg. Transaction Value, Procurement Cost, Total Transactions)
- Clicking a card sets it `.active` (green-tinted border/background using `--green-50` / `--brand`, not a dark highlight) and renders its trend in a single graph panel beneath the row
- Only one graph panel — cards act as tabs into it, not four separate charts
- Reuses `.card` container + sparkline SVG already planned for #2; no new charting library needed if a simple line/area SVG covers it
- Avg. Transaction Value's line plots only days with actual transactions — no interpolation, no zero-fill for no-transaction days, no rolling average. The KPI card's value and the chart's displayed total must agree exactly, since both are the same unsmoothed range total.

**Panel header — what it may and may not restate.** The panel carries the metric label, its range figure, and the series peak. It deliberately does *not* repeat the selected card's foot line or its delta pill: the card sits a few pixels above and is tinted `.active` for as long as its panel is open, so restating either put the same string on screen twice. The figure stays because the header is the `aria-live` region that announces a card click, and a panel announcing only its own title answers nothing.

The peak (`Peak ₱15,360.00 · 2026-07-27`) is there because it is the one line in the header the card cannot already be read for. The chart has no y-axis, so without it the line's shape is legible and its magnitude is not. Two empty states, which are not the same state:
- **no values to plot** — Avg. Transaction Value drops no-transaction days, so an inactive range leaves it nothing. The panel's own `.empty` ("Nothing to chart") covers this; the peak line stays blank rather than saying the same thing twice
- **values present but all zero** — the chart still draws, as a flat line on the midline, so the header reads "No activity in this range". "Peak ₱0.00" is true and useless

**Absent comparisons — say it once, and say which absence it is.** A delta pill exists to state period-over-period change. When it cannot, the reason is one of two unrelated things, and they belong in different places:

| Reason | True of | Where it goes |
|---|---|---|
| The previous window holds no postings at all | the **range** — so all four metrics at once | one quiet line under the rail, no pills |
| The previous window has postings, but this metric was zero | **one metric** | that card's own pill: "None in the previous 24 days" |

Rendering the first as a per-card pill is what the original build did, and on a young dataset it put the identical sentence in the most prominent slot on all four cards *and* again in the panel header — five statements of one fact, each outweighing the figure it sat under. The line under the rail reads "Nothing was recorded in the previous 30 days, so there is no change to show yet." and the cards render no pill at all, which is what gives the figures the top of the card back.

The second case must not borrow the first's wording. Saying "no previous 24 days to compare" when a previous window demonstrably existed is simply false; percentage change from zero stays undefined rather than being invented as +100%.

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
- Presets: Today, Last 7 days, Last 30 days, All time, Custom range. "This month" was dropped — it overlapped Last 30 days for most of any given month while answering a narrower question
- "All time" resolves to an open-ended lower bound (`start = None`). Surfaces that only filter drop their `__gte` term entirely — no `MIN()` across the tables, since filtering never needs to know the earliest record. The Dashboard is the one exception: it pre-allocates one bucket per day across the range and compares against a previous window of equal length, so it cannot draw anything without a lower edge, and resolves one from the earliest posting on record (two indexed aggregates, only on the 'all' branch). The distinction is the point — the resolver avoids a `MIN()` because filtering doesn't need it, while a per-day chart genuinely does. With no postings at all the span collapses to a single day rather than bucketing from year zero. Because "All time" starts at the earliest record, its previous window is always empty by construction — so this range always takes §2a's "Absent comparisons" path: no delta pills on any card, and the single line under the rail instead. (It does *not* put "No previous N days to compare" in each card's pill, which is what this doc specified before the four-identical-pills problem was found.)
- Built on `<details>`/`<summary>` like the topbar avatar menu, so it needs no JavaScript — disclosure, keyboard handling and screen-reader semantics come from the element, and the control still works if JS fails. Presets are submit buttons in one GET form, so presets and the custom Apply share one mechanism
- Custom range uses native `<input type="date">` rather than a hand-built calendar grid. The inspo's styled month grid would be ~200 lines of new JS and a new component with no precedent in this codebase; the native control brings its own calendar, keyboard and locale handling, and already appears in the Procurement/Billing toolbars. This is a deliberate deviation from the reference
- Selection persists per session (`session['date_range']`), mirroring the existing `session['language']` / `session['branch_id']` precedent. URL params always win over the stored value, so a copied link reproduces exactly what the sender saw
- Legacy `?month=YYYY-MM` links are aliased to that month's equivalent custom range rather than falling through to the default window — a silent wrong-figures failure otherwise

**Surfaces exempt, and why.** §2e originally said "Dashboard, Reports and Alerts". Two of those have no date axis to control, so the control is deliberately absent rather than inert:
- **Alerts Center** — low stock is *current* quantity vs. reorder level and expiry is a fixed `EXPIRY_WINDOW_DAYS` lookahead. Neither is a historical range, so a Today/7/30 control would either do nothing or silently redefine what "low stock" means
- **Reports → Inventory** — point-in-time stock levels, same reasoning
- **Recent Activity (§2f)** on the Dashboard is also unscoped by it, per its own section: it answers "right now", not "over the selected window"

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

**2h. Branch Status (Dashboard) — REMOVED**
Built, then removed from the Dashboard on request. It showed each branch's takings today against that branch's own trailing 7-day baseline, plus stock on hand, in a two-mode panel (tinted alert cards / plain neutral list).

Why it went: sitting beside Needs Attention it read as a sixth unlisted check, and labelling the signal that tripped it (five arms: critical stock / behind pace + low stock / pace / low stock / keeping pace) narrowed but did not close that gap. A branch with no sales *yet today* is the normal state at 9am, so the panel's most common amber was a pace reading that needed explaining rather than acting on.

If per-branch pace is wanted again, it belongs somewhere with room to state its own axis — not adjacent to an actionable checklist. `_branch_status_rows()` and the `BRANCH_*` thresholds were deleted with the panel; recover them from history rather than rewriting from scratch.

**3. Status color rule — enforce consistently**

> **A CTA inside a status-tinted surface takes that status's own color. `--action` green is reserved for CTAs on neutral/unthemed surfaces.**
> The tint decides the CTA: a warn-tinted card's "Review →" is `--warn-cta`, a danger-tinted card's is `--danger-cta`. Off a tint, every CTA stays `--action` green.
> Status color still never *substitutes* for action color: a "Create PO" button on a plain white page is `--action` green, never amber.

**This reverses the previous rule, it does not extend it.** §3 used to read "amber/warning means warning status only — never use it for action buttons… status color and action color are separate systems," and required a CTA to stay green *regardless* of the surface under it. That is no longer the rule and should not be reintroduced from doc history. What changed: on a tinted card the green CTA read as a second, competing status ("green = fine") on a surface whose whole job was to say "not fine." The surface owns the semantics; the CTA inherits them.

The narrower claim inside the old rule survives — status color is not a *palette for buttons*. What died is the "regardless of surrounding color" clause.

**Applying it:**
- Scope is strictly CTAs *inside* a `*-bg`/`*-fg` tinted card or pill. Nav, table row actions, header links like "View all alerts →", and every `.btn-primary` are on neutral surfaces and stay green — this is a per-status modifier, not a change to `--action`
- The card carries its status as local `--status-*` custom properties (`.attn-card` / `.attn-card--danger`); a CTA reads `var(--status-cta)`. A new status needs the five `--status-*` lines, not a new CTA rule
- Hover deepens to the status's own `-fg`, never `--action-700` — jumping hue mid-interaction reads as a different control

**The `-dot` tokens are not all usable as CTA type.** They were authored as dot/border colors. Measured against their own `-bg` at the CTA's 13.5px/700 — below the 18.66px-bold large-text threshold, so the bar is **4.5:1, not 3:1**:

| status | `-dot` on own `-bg` | verdict |
|---|---|---|
| `--danger-dot` | 5.59:1 | passes — `--danger-cta` uses it directly |
| `--warn-dot` | 2.83:1 | fails 4.5:1 *and* 3:1 → `--warn-cta` `#8F5A2B` (5.01:1) |
| `--ok-dot` | 2.94:1 | would fail — needs its own `-cta` before use |
| `--info-dot` | 3.05:1 | would fail — needs its own `-cta` before use |

So: **add an `-cta` token per status and verify it numerically before tinting a CTA on that surface.** Do not reach for the `-dot` token and assume it carries. Softening `--warn-bg` toward the dot's hue is what pushed warn from 4.44:1 to 2.83:1 — a bg change can silently break a CTA that passed before, so re-measure both tokens whenever either moves.

**4. Typography — consider Inter**
Current: `system-ui`. Works fine and avoids font-loading latency. If the team wants a more distinctive look, switch to Inter (open-source, humanist, wide weight range). Not a priority — only change if actively wanted.

**5. Sidebar icon-rail improvements**
The collapsed state already works as an icon-only rail. Potential enhancement: add flyout-on-hover to reveal sub-items without expanding the full sidebar. Not a v1 requirement.

*(Note: stakeholder inspo referenced a hover-to-reveal icon rail as a liked pattern — Gerlyn's sidebar already implements the collapsed icon-only rail described in "Existing components." The flyout-on-hover sub-item behavior above is the only piece not yet built; no other change needed here.)*

**6. Breadcrumbs — mostly already solved, in a different idiom**

Both premises of the original note were wrong, so it is restated rather than kept. A `.breadcrumb` component *is* implemented — `<nav aria-label="breadcrumb">` with `.sep` and `.current` — and eight customer account pages use it. It was defined in `account.css`, which the internal shell never loads, which is why it looked absent from that side. It now lives in `gerlyn.css` where both shells can see it.

And the internal routes it was meant for already have the affordance. Thirteen detail/form/confirm pages open with `← Purchase Orders`, `← Stock Adjustments`, `← Suppliers` and so on. That is a two-level breadcrumb written as an action instead of a location.

**The rule, then, is depth — not "deeper than one level".**

| Chain | Use | Why |
|---|---|---|
| Section → record *(the common case)* | `← Section` back-link | The sidebar already shows the section, and the record is the `<h1>` directly below. A trail would restate the heading it sits on top of |
| Three or more, or a parent that is itself a record | `.breadcrumb` | The middle of the chain is genuinely invisible otherwise |

Converting the thirteen working back-links to `›` trails would be churn that also duplicates every `<h1>`. Not planned. The component stays available for chains that earn it.

**The one route that earns the component: `billing/billing_form.html`.** It is reachable from two parents — `transaction_detail`, and `billing_detail`'s "Edit payment details"/"Upload proof" — and carried a single hardcoded back-link to the order, so anyone arriving from Billing was moved into Sales & Orders on the way back while the page header still said "Billing".

A static trail would have been equally wrong for one of the two paths, so the entry links state where they came from and `billing_create` builds the trail from that:

| Entry | Trail |
|---|---|
| Order detail *(no flag)* | Sales & Orders › ORD-… › Record payment |
| Billing record *(`?from=billing`)* | Billing › ORD-… › Record payment |

A `from` flag rather than `?next=<url>` on purpose: the choice is only ever between two known routes, and a flag cannot be pointed somewhere arbitrary, so there is no redirect target to validate. Anything unrecognised falls through to the order — the older path, and the safe default.

This is also the shape the rule above predicts: three levels with a record in the middle, where both the section and the record are invisible without a trail. The order number came out of the subtitle at the same time, since the trail now names it and the branch was the only other thing that line carried.

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
11. ~~**Add breadcrumbs** under topbar for deep routes~~ — done, though not as written. See §2's "Breadcrumbs": the component moved to `gerlyn.css`, the thirteen internal `← Parent` links stay as they are, and the one route that genuinely needed a trail (`billing_form`, two parents) has one
12. **Fold Categories into Products** as a filter/tab (when team is ready)
13. *(Lower priority, optional)* **Reports multi-panel row** (2c) and **Sales & Orders funnel** (2d)
