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

- KPI tiles: Total Products, Low/Critical Stock, Total Sales, Recent Sales (30 days), Procurement Cost, Total Transactions
- Sales by Branch table
- Top-Selling Products list
- Low Stock preview table (compact, read-only) — links to Alerts Center via "View all alerts →" instead of being its own independent implementation

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

**2a. KPI → own-graph pattern (Dashboard, click-to-expand)**
Reference: stakeholder-liked pattern where a KPI strip sits above a large graph, and selecting a different KPI swaps the graph below it to match.

Adapt as:
- Row of `.stat-hero` cards (Total Sales, Recent Sales, Procurement Cost, Total Transactions)
- Clicking a card sets it `.active` (green-tinted border/background using `--green-50` / `--brand`, not a dark highlight) and renders its trend in a single graph panel beneath the row
- Only one graph panel — cards act as tabs into it, not four separate charts
- Reuses `.card` container + sparkline SVG already planned for #2; no new charting library needed if a simple line/area SVG covers it

**2b. "Needs attention" list — formalize existing pattern**
This already exists conceptually as the Dashboard's low-stock preview and the Alerts Center. Reference inspo showed it as a clean icon + label + count-badge + chevron list.

Adapt as:
- Dashboard keeps a compact `.card` version, max 5 rows: Low Stock, Expiring Soon, Pending POs, Unfulfilled/Pending Orders, Overdue Billing — each with a `.pill` count badge and trailing chevron
- Every row links out: Low Stock/Expiring → Alerts Center tabs, Pending POs → Purchase Orders, Pending Orders → Sales & Orders, Overdue Billing → Billing's Outstanding Balances view
- Retains "View all alerts →" link already specced in Cleanup tasks — this list is that preview, formalized
- Alerts Center's own Low Stock Alerts tab is the full, non-compact version of this same row pattern (icon, label, count) but with the existing Qty/Reorder/Suggested/Risk columns intact — no change to that table

**2c. Multi-panel "informative" row (Reports only, not Dashboard)**
Reference: donut chart + sparkline + spend-vs-return proportion bar shown together in one dense row.

Adapt as:
- New optional row for Reports' Sales and Procurement tabs (not Dashboard — too dense for a landing view)
- Three `.card` panels side by side on desktop, stacking at ≤860px per existing responsive rules: a donut/breakdown (e.g. sales by branch or category), a daily trend sparkline, and a spend-vs-return `.meter` bar
- Built from existing primitives (`.card`, `.meter`, sparkline SVG) — no new dependency

**2d. Funnel visualization — Sales & Orders only, optional**
Reference: views → cart → checkout → purchase funnel with percentage retention at each stage.

Adapt as:
- Optional panel on Sales & Orders showing the order lifecycle funnel: Created → Fulfilled → Paid (or Created → Accepted → Fulfilled → Paid, depending on what stages the workflow tracks)
- Light-mode treatment: stacked horizontal bars in `--green-100` → `--brand` gradient steps rather than dark-mode grayscale, with `--ink-900` percentage labels
- Lower priority than 2a/2b — flag as nice-to-have, not part of the initial task order below

**2e. Date range control — standardize across Reports/Alerts**
Reference: calendar dropdown with preset ("Last 7 days") + custom range + "Customize" affordance.

Adapt as:
- Single date-range component added to `.toolbar` / `.toolbar-card`, used by Reports and Alerts consistently (already flagged in Component rules: "All date filtering should use the same date-range control pattern" — this is that component, now specified)
- Presets: Today, Last 7 days, Last 30 days, This month, Custom range
- Calendar dropdown uses `--surface` background, `--brand` for selected day fill, `--green-50` for range-highlight — light equivalent of the dark inspo, not a new visual language

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
8. **Build standardized date-range control** (2e) and wire into Reports + Alerts toolbars
9. **Add breadcrumbs** under topbar for deep routes
10. **Fold Categories into Products** as a filter/tab (when team is ready)
11. *(Lower priority, optional)* **Reports multi-panel row** (2c) and **Sales & Orders funnel** (2d)
