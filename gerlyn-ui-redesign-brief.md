# Gerlyn Variety Store — UI Redesign Brief

> A copy-paste design brief for redesigning the Gerlyn Variety Store platform.
> Goal: make it **simple and user-friendly** while keeping its existing identity.

---

## PART 1 — CONTEXT & PLAN (for your reference)

### What the system is
A multi-branch variety/grocery store platform for **Gerlyn Variety Store** (Baliuag, Bulacan, Philippines) with two halves:

- **Internal system** (staff-facing — Owner, Manager, Staff):
  Dashboard · Sales & Orders (Order Console, Walk-in POS) · Inventory (Product Master List, Categories, Stock per Branch, Withdrawal Slips) · Procurement (Suppliers, Purchase Orders, Deliveries) · Reports (Sales, Inventory, Procurement, Billing) · Alerts (low stock + expiry) · Administration (Branches, Users, Bundles).
- **Customer storefront** (public e-commerce):
  Home · Products · Cart · Login/Register · Track Order.

Currency is Philippine Peso (₱). Users include non-technical shop staff and everyday customers, often on **mobile phones**.

### What's already good (keep it)
- Consistent green/white identity, clean sans-serif, card-based layout.
- Sensible information architecture and sidebar grouping.
- Traffic-light status colors (green/yellow/red) are intuitive.

### Problems to fix ("not simple enough")
| # | Problem | Where |
|---|---------|-------|
| 1 | Dashboard overload — 8 competing stat cards, no clear "look here first." | Dashboard |
| 2 | Truncated/cut-off text — "Gerlyn S...", "In Stoc", "Lo w Stoc", clipped action text. | Sidebar, Stock, Alerts |
| 3 | Dense tables, no breathing room — hard to scan. | Stock, Alerts, Orders |
| 4 | Weak visual hierarchy — headings, labels, data all similar weight. | Most internal pages |
| 5 | Inconsistent number/currency formatting & alignment. | Stock, Reports |
| 6 | Storefront hero has overlapping/ghosted headline text. | Storefront home |
| 7 | No obvious mobile/touch consideration. | All |
| 8 | Tiny, low-contrast action icons; primary vs. secondary not distinct. | Product list, Suppliers, Users |

### Design principles to enforce
1. One clear primary focus and one obvious primary action per screen.
2. Progressive disclosure — summary first, details on expand.
3. Generous white space; minimum 44px touch targets.
4. Consistent formatting — right-aligned numbers, standard ₱ currency, one date format.
5. Scannable tables that collapse to stacked cards on mobile.
6. Mobile-first, responsive — sidebar becomes drawer/bottom nav on small screens.
7. Accessible — WCAG AA contrast, visible focus states, 16px base font.

---

## PART 2 — PASTE THIS INTO CLAUDE

**Role:** You are a senior product designer. Redesign the UI for an existing web platform. Prioritize **simplicity and ease of use** above visual flair.

**Product:** "Gerlyn Variety Store" — a management platform for a multi-branch Filipino variety/grocery store in Baliuag, Bulacan. It has two parts:

1. **Internal system** (staff-facing, roles: Owner, Manager, Staff) with: Dashboard, Sales & Orders (Order Console + Walk-in POS), Inventory (Product Master List, Categories, Stock per Branch, Withdrawal Slips), Procurement (Suppliers, Purchase Orders, Deliveries), Reports (Sales/Inventory/Procurement/Billing), Alerts (low stock + expiry), and Administration (Branches, Users, Bundles).
2. **Customer storefront** (public e-commerce) with: Home, Products, Cart, Login/Register, Track Order.

Currency is Philippine Peso (₱). Users include non-technical shop staff and everyday customers who will often be on **mobile phones**.

**Keep this identity:** Green (~#2E9E6B) + white primary palette, clean sans-serif, card-based layout, and traffic-light status colors (green = good/in-stock, yellow = low/warning, red = critical/overdue). Refine it — don't reinvent it.

**Fix these specific problems from the current design:**
- Dashboard shows 8 competing stat cards with no clear priority — establish a hierarchy (1–2 hero metrics, the rest secondary).
- Text is truncated and cut off (store name, status pills like "In Stock"/"Low Stock", suggested-action text) — never truncate critical labels; wrap text or size containers properly.
- Tables are dense and hard to scan — add row spacing, dividers/zebra striping, sticky headers, and status pills.
- Weak visual hierarchy — make headings, labels, and data visually distinct with type scale and weight.
- Numbers and currency aren't consistently right-aligned or formatted — standardize.
- The storefront hero has overlapping/ghosted headline text — make it one clean hero.
- Small, low-contrast icon actions (edit/delete) — give clear primary vs. secondary action styling and larger tap targets.

**Design principles to follow:**
1. One clear primary focus and one obvious primary action per screen.
2. Progressive disclosure — summary first, details on expand (apply to restock suggestions and stock-per-branch tables).
3. Generous white space; minimum 44px touch targets.
4. Consistent formatting: right-aligned numbers, standard ₱ currency, one date format (YYYY-MM-DD).
5. Scannable tables that collapse into stacked cards on mobile.
6. Mobile-first and responsive — sidebar becomes a drawer/bottom nav on small screens.
7. Accessible: WCAG AA contrast, visible focus states, 16px base font.

**Deliverables:**
1. A short design-system foundation: color tokens, type scale, spacing scale, and reusable components (buttons, inputs, status pills, stat cards, tables, empty states, modals).
2. High-fidelity redesigns of these priority screens: **Internal Dashboard, Stock per Branch, Order Console, Alerts Center, Product Master List, and the customer Home + Products + Cart.** Show desktop and a mobile variant for each.
3. Brief annotations explaining each key simplification decision.

Build it as a single self-contained, responsive HTML/CSS artifact (no external dependencies) so I can preview it live. Start by proposing the design-system foundation and a redesigned Dashboard, then continue through the other screens.

---

## PART 3 — TIPS FOR USING THE PROMPT

- Pasting into **claude.ai** produces a live, clickable HTML artifact — the fastest way to preview and show the client.
- If results feel rushed, feed it **one screen at a time** (Dashboard → Stock → Orders → …) after the intro + principles. More polish per screen.
- Want a different output? Swap the last paragraph for:
  - *"Output as React + Tailwind components."*
  - *"Output as a Figma-style component spec with tokens and redlines."*
