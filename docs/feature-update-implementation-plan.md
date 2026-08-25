# GerlynPLUS — Feature Update Implementation Plan
### Owner-requested additions: Delivery Scanning, Supplier–Product Mapping, Stock Withdrawal Scanning, VAT/PWD-Senior Discounts, PO Printing

> **Status: implemented.** All five items are built, migrated and tested —
> see `core/tests_feature_update.py` (28 tests). The plan below is kept as the
> design record; §8 at the end lists the three places the implementation
> deliberately departed from it, and why.

This plan is grounded in the current codebase (Django apps: `inventory`, `procurement`, `sales`, `billing`, `accounts`, `branches`). Each section notes what **already exists** and what is **net-new**, because two of the five requested features are largely built already and only need extending.

---

## 0. What already exists (read this first)

| Requested feature | Current state |
|---|---|
| Barcode scanning | `static/js/barcode-scanner.js` (`GerlynBarcodeScanner`) already handles USB/Bluetooth HID scanners (Enter-terminated input) and in-app camera scanning (html5-qrcode). Used today on POS quick-add and the Product barcode field. **Reusable as-is for delivery scanning and withdrawal scanning.** |
| Stock Withdrawal Process | Already implemented as `inventory.StockTransfer` / `StockTransferItem` with a full approval chain (Request → Approve → Release → Acknowledge), a `Purpose` enum (`Branch Restock`, `Damaged/Spoiled`, `Sample/Display`, `Other`), and discrepancy flagging. UI lives at "Stock Withdrawals". **This is not being built from scratch — see §3.** |
| Supplier records | `accounts.SupplierProfile` (one per `User` with `role=SUPPLIER`) plus `templates/procurement/supplier_list.html`. **No product-association table exists yet** — suppliers and products are only ever linked indirectly through past `ProcurementItem` rows. |
| PO fields | `procurement.Procurement` already has `po_number`, `order_date`, `delivery_date` (used today as the *target/expected* delivery date — `delivery_list` sorts and flags it as overdue), `total_cost`, `branch`, `supplier`, `owner`, `created_by`. **No printable template exists.** |
| VAT / PWD-Senior discount | **Nothing exists.** No tax field on `Product`, no discount field on `SalesTransaction`/`SalesItem`, no VAT math anywhere in `billing` or `sales`. This is a new subsystem. |
| Delivery receiving | `procurement/delivery_receive.html` is a manual quantity-entry table (`ProcurementItem.quantity_received`), already produces `Procurement.receipt_flag` (Partial/Discrepancy). **No scan-to-verify UI yet.** |

---

## 1. Delivery Scanning & Product Breakdown

### Functional Requirements & System Logic
- On the existing **Receive Delivery** screen (`delivery_receive.html`, backed by `procurement_detail`'s items), add a barcode input wired via `GerlynBarcodeScanner.attach()` (camera button included, since warehouse staff are on tablets/phones as often as desktop).
- Each scan resolves the code against `Product.barcode` for products already on **this PO's** `ProcurementItem` list (not the whole catalog):
  - **Match found on this PO** → increment that row's running received count by 1 (capped display, but allow going over `quantity_ordered` — see below), flash the row, and re-render its verification badge.
  - **Match found, but not on this PO** (right product, wrong order) → show a non-blocking warning toast ("Not on this PO") and do *not* silently add it — staff must confirm via an "Add unexpected item" action, which appends a new adjustable row flagged `unexpected=True`.
  - **No product matches the code at all** → same "Add unexpected item" path, but the row prompts staff to link it to a catalog product or leave it as an unresolved note for the Owner/Manager to review.
  - **Scanned count exceeds `quantity_ordered`** → row is not blocked, just flagged `Over` (amber) instead of `Complete` (green) — matches the existing tolerant discrepancy pattern already used by `Procurement.ReceiptFlag`.
- Manual entry (the existing numeric `<input>`) remains fully functional side-by-side — scanning just increments the same field, it doesn't replace typing. This matters because not every product has a `barcode` (`Product.barcode` is nullable/optional today).
- Per-row verification status, computed client-side and re-validated server-side on submit:
  - `Not Started` (0 received) / `Partial` (0 < received < ordered) / `Complete` (received == ordered) / `Over` (received > ordered).
- On submit, the existing receive logic is unchanged: it still writes `ProcurementItem.quantity_received`, still computes `Procurement.receipt_flag` (Partial/Discrepancy vs None), still creates the FEFO `Batch` and `StockMovement` rows. Scanning only changes *how the number gets into the field*, not what happens after submit.
- Audit trail: log each individual scan event (not just the final tally) so "who scanned what, when" is answerable if a discrepancy is disputed later — mirrors the audit posture of `StockMovement`.

### Database Schema Updates
No changes required to `ProcurementItem` — `quantity_received` already exists and already drives `receipt_flag`.

New table for scan-level audit (optional but recommended given the store's existing appetite for movement-level audit trails):

```python
class DeliveryScanLog(models.Model):
    procurement = models.ForeignKey('procurement.Procurement', on_delete=models.CASCADE, related_name='scan_logs')
    procurement_item = models.ForeignKey('procurement.ProcurementItem', on_delete=models.SET_NULL, null=True, blank=True, related_name='scan_logs')
    scanned_code = models.CharField(max_length=64)
    matched_product = models.ForeignKey('inventory.Product', on_delete=models.SET_NULL, null=True, blank=True)
    was_unexpected = models.BooleanField(default=False)   # scanned but not on this PO / no match
    scanned_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True)
    scanned_at = models.DateTimeField(auto_now_add=True)
```

For unresolved/unexpected items added during receiving, extend `ProcurementItem`:

```python
# procurement/models.py — ProcurementItem
is_unexpected = models.BooleanField(default=False, help_text="Added during receiving; was not on the original PO")
```

### UI/UX Workflow Steps
1. Staff opens **Deliveries → Receive Delivery** for a given PO (existing entry point).
2. New "Scan to Verify" panel at the top of the item table: a text input (camera button beside it) with focus auto-set on page load.
3. Staff scans each physical unit as it's unloaded; the matching row's "Quantity Received" counter increments live and the row highlights green/amber based on status.
4. Items with no barcode are entered manually in the same column, same as today.
5. Any scan that doesn't match a row on this PO surfaces an inline banner: "Barcode 4800XXXX not on this PO — [Add as unexpected item]".
6. Staff reviews the summary strip (Complete / Partial / Over / Unexpected counts) before submitting.
7. Submit behaves exactly as it does today — updates stock, flags discrepancy, redirects to `procurement_detail`.

---

## 2. Supplier Management & Product Association

### Functional Requirements & System Logic
- **New concept**: a persistent Supplier↔Product catalog association, independent of order history. Today, "which products does Supplier X carry" can only be inferred by scanning past `ProcurementItem` rows — there is no forward-looking source of truth for "who do I currently buy this from," which is what a PO-creation dropdown or a supplier detail page needs.
- **Transfer**: reassigning a product's supplier association is a metadata change only — it never rewrites historical `Procurement`/`ProcurementItem` rows (those must stay as an accurate record of what was actually ordered from whom at the time). Transfer just updates which supplier is the *current* default for future orders.
- Support both single-product transfer (from a product's context) and bulk transfer (from a supplier's product list, multi-select → "Transfer Selected").
- Deleting a supplier (existing `supplier_delete` view) should be blocked or warned if it still has associated products, so the transfer flow is the guided path off a supplier rather than silent data loss.

### Database Schema Updates
```python
# procurement/models.py (new)
class SupplierProduct(models.Model):
    supplier = models.ForeignKey('accounts.User', on_delete=models.CASCADE,
        limit_choices_to={'role': 'SUPPLIER'}, related_name='supplied_products')
    product = models.ForeignKey('inventory.Product', on_delete=models.CASCADE,
        related_name='supplier_links')
    supplier_sku = models.CharField(max_length=50, blank=True,
        help_text="Supplier's own product code, if different from our sku_code")
    default_cost_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Pre-fills cost_price when this product is added to a new PO for this supplier")
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)
    is_preferred = models.BooleanField(default=False,
        help_text="Preferred supplier when a product has more than one")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('supplier', 'product')
```

Transfer is implemented as an update to `SupplierProduct.supplier` (with a small service function `transfer_supplier_products(product_ids, from_supplier, to_supplier, actor)` that also handles the `unique_together` collision if the destination supplier already carries that product — merge rather than error).

### UI/UX Workflow Steps
1. **Suppliers** list (`supplier_list.html`) gains a "Products" count column per row, linking into a new **Supplier Detail** page.
2. Supplier Detail = tabs: **Overview** (existing profile fields) | **Products** (new).
3. Products tab: table of `SupplierProduct` rows (Product, Supplier SKU, Default Cost, Preferred, Added) with:
   - "Add Product" → searchable product picker modal (reuses the catalog search pattern already used elsewhere).
   - Per-row "Transfer" action → modal: pick destination supplier → confirm → row moves off this list.
   - Checkbox multi-select + "Transfer Selected" for bulk moves.
   - "Remove" to unlink without transferring (product simply has no current supplier).
4. On the **Add/Edit Product** page and on **Create PO**, the supplier dropdown/product picker can now be pre-filtered or annotated by `SupplierProduct`, cutting down manual matching — this is a natural follow-on but not required for the MVP of this feature.

---

## 3. Stock Withdrawal Workflow

### Framing
This workflow **already exists** as `StockTransfer` / `StockTransferItem` (UI: "Stock Withdrawals"), including the exact request → approve → release → acknowledge chain, purpose codes for internal/damaged/manual cases, and discrepancy flagging (`StockTransfer.flag_reason`, `received_qty`). The only genuinely new work is deciding how scanning plugs into it.

### Functional Requirements & System Logic — scan vs. manual
- **Where scanning applies**: at the **Release** step, when warehouse staff physically pull an *approved* withdrawal's items off the shelf. This is the same shape of problem as delivery receiving (§1) — confirming what's physically moving against what was authorized — so it reuses the same `GerlynBarcodeScanner` component and the same match/no-match/over logic.
- **Where scanning does not apply**: the *Request* step (a manager typing "5 sacks of rice, damaged" while filling out the form) and the *Acknowledge* step at the destination branch, which is a simple confirm/discrepancy action already in place — adding scanning there is unnecessary friction for a single-line confirmation.
- **Scan vs. manual decision rule**: manual entry is always available and is never blocked by scanning — this mirrors §1's rule and is necessary because `Product.barcode` is optional. The system does not treat "scanned" as more trustworthy than "typed"; it only records *how* the quantity was captured, for audit purposes (was this counted with a scanner or eyeballed and typed in).
- **Per-item granularity gap**: today, `StockTransferItem` only has `quantity_requested` and `quantity_approved` — there is no item-level "how much was actually picked/released" field (only `StockTransfer.received_qty`, which is a single header-level number for the whole slip). Scanning needs a per-item running counter to know when each row is done, so this plan adds `quantity_released` per item (see schema below). The header-level `received_qty` stays as-is for the acknowledge step.

### Database Schema Updates
```python
# inventory/models.py — StockTransferItem
quantity_released = models.PositiveIntegerField(
    default=0,
    help_text="Actually picked/scanned at release time. Drives per-item release status."
)
release_method = models.CharField(
    max_length=10,
    choices=[('SCAN', 'Scanned'), ('MANUAL', 'Manual')],
    blank=True,
    help_text="How quantity_released was captured, for audit purposes"
)
```
(Optional, matching §1's audit table if the Owner wants full parity: a `WithdrawalScanLog` mirroring `DeliveryScanLog`. Skip unless the capstone's evaluators specifically ask for symmetric audit trails — otherwise `release_method` on the item row is enough.)

### UI/UX Workflow Steps
1. **Request** (unchanged): Staff/Branch Manager fills out purpose, items, quantities — typed, no scanning.
2. **Approve** (unchanged): Branch Manager/Owner approves or adjusts `quantity_approved` per item.
3. **Release** (new scan integration): warehouse staff opens the approved slip's Release screen. Same "Scan to Verify" panel as delivery receiving: scan or type per item, running counter against `quantity_approved`, status badges (Not Started/Partial/Complete/Over). Submitting sets `status = Released`, stamps `release_method` per row.
4. **Acknowledge** (unchanged): destination branch confirms `received_qty`; mismatch vs. released quantity flips `status = Discrepancy` with `flag_reason` required, exactly as today.

---

## 4. Tax & Discount Configuration (VAT-Exempt / PWD-Senior)

### Assumptions (flag these with your adviser/owner before building — no tax logic exists in the codebase today to anchor against)
- `Product.unit_price` is currently a plain VAT-inclusive shelf price with no tax breakdown anywhere in `sales`/`billing`. This plan treats 12% as the standard PH VAT rate baked into `unit_price` unless `is_vat_exempt` is set.
- PWD/Senior discount follows RA 10754 / RA 9994 mechanics: the discount is 20% off the **VAT-exclusive** price, and the line becomes VAT-exempt for that transaction — it is not simply "20% off the sticker price."
- Not all products legally qualify for the PWD/Senior discount (e.g., it typically excludes non-essential/non-covered goods) — hence a **per-product eligibility flag**, separate from the VAT-exempt flag, as the Owner's request implies ("VAT-Exempt **or** eligible for PWD/Senior discount" — two distinct booleans, not one).

### Functional Requirements & System Logic
- **Product-level (Add/Edit Product form)**: two independent toggles —
  - `is_vat_exempt` — this specific product never has VAT added, regardless of buyer.
  - `is_pwd_senior_eligible` — this product is legally eligible for the PWD/Senior 20% discount when a qualifying transaction claims it.
- **Transaction-level (POS checkout)**: the discount is claimed once per sale (an ID belongs to one customer), not per line item — a new "Apply PWD/Senior Discount" toggle on checkout captures `discount_type` (`PWD`/`SENIOR`), the ID number, and the holder's name (required for BIR-style recordkeeping and to prevent casual misuse).
- **Per-line computation at checkout**, evaluated per `SalesItem`:
  1. If `discount_type` is set on the transaction **and** `product.is_pwd_senior_eligible`:
     - `vat_exclusive_price = unit_price / 1.12` if the product is normally VATable (`is_vat_exempt=False`); otherwise `vat_exclusive_price = unit_price`.
     - `discount_amount = vat_exclusive_price * 0.20`
     - `net_price = vat_exclusive_price - discount_amount` (no VAT added back — the line is now exempt)
  2. Else if `product.is_vat_exempt` (no discount claimed, or product isn't discount-eligible): `net_price = unit_price`, no VAT line.
  3. Else (normal VATable item, no discount): `net_price = unit_price` — VAT stays embedded as it is today; store the implied VAT amount (`unit_price - unit_price/1.12`) for reporting even though it doesn't change what the customer pays.
- Store the computed breakdown per line so receipts and reports don't have to recompute after the fact — prices/eligibility can change later, but a past receipt must stay accurate.

### Database Schema Updates
```python
# inventory/models.py — Product
is_vat_exempt = models.BooleanField(default=False)
is_pwd_senior_eligible = models.BooleanField(default=True,
    help_text="Whether this product qualifies for the PWD/Senior Citizen 20% discount")
```

```python
# sales/models.py — SalesTransaction
class DiscountType(models.TextChoices):
    NONE = 'None', 'None'
    PWD = 'PWD', 'PWD'
    SENIOR = 'Senior', 'Senior Citizen'

discount_type = models.CharField(max_length=10, choices=DiscountType.choices, default=DiscountType.NONE)
discount_id_number = models.CharField(max_length=50, blank=True)
discount_holder_name = models.CharField(max_length=150, blank=True)
total_discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
total_vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0,
    help_text="Sum of VAT actually embedded across all lines, for reporting")
```

```python
# sales/models.py — SalesItem
is_vat_exempt_line = models.BooleanField(default=False, help_text="True if this line ended up VAT-exempt (product flag or discount applied)")
vat_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
```

### UI/UX Workflow Steps
1. **Add/Edit Product**: two new checkboxes near `unit_price` — "VAT-Exempt" and "Eligible for PWD/Senior Discount" (defaulted checked, since most retail goods qualify).
2. **POS Checkout**: an "Apply Discount" toggle near the payment section. Selecting it reveals PWD/Senior radio choice + ID number + holder name (all required to proceed). Non-eligible line items show a small "Not discount-eligible" tag in the cart so staff isn't surprised the total didn't drop as much as expected.
3. **Cart/receipt totals** break out: Subtotal (VAT-inclusive) → Less: VAT-Exempt Sales → Less: PWD/Senior Discount → Amount Due — matching the line-item-breakdown format PH receipts typically show.
4. **Reports**: a VAT/discount line becomes available wherever sales totals are already reported (existing `reports` app), summing `total_discount_amount`/`total_vat_amount`.

---

## 5. Purchase Order (PO) Printing

### Functional Requirements & System Logic
- No new model fields needed — every field the Owner listed already exists on `Procurement`/`ProcurementItem`/`SupplierProfile`/`Branch`:
  - PO Number → `Procurement.po_number`
  - Supplier Details → `Procurement.supplier` → `SupplierProfile` (name, contact person, contact number, address)
  - Item List & Quantities → `ProcurementItem` (`product`, `quantity_ordered`, `cost_price`)
  - Target Delivery Date → `Procurement.delivery_date` (already used as the expected/target date pre-receipt, per `delivery_list`'s overdue logic)
  - Total Cost → `Procurement.total_cost`
  - Authorized Signatures → `Procurement.owner` (authorizer) and `Procurement.created_by` (preparer) supply the printed names; actual signatures are physical/ink on the printout, as is standard for this kind of document — no e-signature capture is in scope.
- New read-only print view, server-rendered so it works with the browser's native print-to-PDF (no PDF library dependency needed).

### Database Schema Updates
None required.

### UI/UX Workflow Steps
1. `procurement_detail.html` gains a "Print PO" button, visible for any status.
2. Opens `procurement/po_print.html` — a print-optimized template (no sidebar/nav chrome, `@media print` CSS) laid out as:
   - Header: store name/logo placeholder, "Purchase Order", PO Number, Order Date.
   - Two-column block: **Supplier** (name, contact person, phone, address) vs. **Deliver To** (branch name, location).
   - Target Delivery Date, prepared-by (`created_by`), authorized-by (`owner`).
   - Item table: Product, SKU, Qty Ordered, Unit Cost, Line Total.
   - Total Cost footer.
   - Signature blocks: two blank-lined boxes labeled "Prepared By" / "Authorized By", pre-filled with the two names above, plus a blank "Supplier Acknowledgement" box.
3. Browser's native print dialog (`window.print()` on a toolbar button, hidden under `@media print`) handles the actual PDF/paper output — no server-side PDF generation needed for the capstone scope.

---

## 6. Consolidated migration summary

New migrations needed, grouped by app:

- **inventory**: `Product.is_vat_exempt`, `Product.is_pwd_senior_eligible`; `StockTransferItem.quantity_released`, `StockTransferItem.release_method`.
- **procurement**: new `SupplierProduct` model; `ProcurementItem.is_unexpected`; optional new `DeliveryScanLog` model.
- **sales**: `SalesTransaction.discount_type`, `discount_id_number`, `discount_holder_name`, `total_discount_amount`, `total_vat_amount`; `SalesItem.is_vat_exempt_line`, `vat_amount`, `discount_amount`.
- **No changes** to `billing`, `branches`, `accounts`, or `reports` models (reports consume the new sales fields via existing aggregation, no schema change there).

## 8. Where the implementation departed from this plan

Three decisions were changed during the build. Each is recorded here because the plan is the design record and should not read as though it were followed verbatim.

**1. Over-delivery is still rejected, not accepted-and-flagged.**
§1 proposed letting a received quantity exceed what was ordered, flagged amber as `Over`. The existing `delivery_receive` already rejected that case, with a comment stating the reason: under-delivery is a normal short shipment, but over-delivery "needs a human to look at it, not a silent accept." That is a deliberate control, and the plan overturned it without arguing against it. The implementation keeps the rejection and instead:
- warns **client-side**, live, as soon as a count goes over — so staff find out while they can still fix it, rather than after losing the form to a failed submit;
- routes genuinely-unordered goods through the explicit "add unexpected item" action, which is the sanctioned way to record more than was ordered.

**2. `quantity_released` drives the actual stock movement.**
§3 described the new field as driving "per-item release status". That would have made it decorative: the slip would still have moved `quantity_approved` regardless of what was picked, so scanning 8 of 10 units would still have transferred 10. The release step now moves exactly what was picked, clamped at `quantity_approved` — a picker cannot raise the ceiling the approver set. A POST with no per-item fields still releases the approved amount, so the old direct-POST button remains backwards compatible (covered by `test_a_post_without_picked_quantities_releases_what_was_approved`).

**3. The sales API records VAT but never accepts a discount claim.**
§4 was written around the POS counter. The DRF `SalesTransactionWriteSerializer` had its own duplicated pricing, which would have left API-created sales recording zero VAT and quietly skewing reports — so it now calls the same `sales.pricing` engine. It deliberately does **not** expose `discount_type`: that endpoint serves online and customer-facing checkout, where nobody has presented a PWD or OSCA card to a cashier, and granting a statutory discount on an unverified assertion over the wire is a different decision from the one the Owner asked for. Worth raising with the Owner if online PWD/Senior orders are wanted.

Two smaller notes:
- `DeliveryScanLog` was built (the plan marked it optional). Scans are logged on each scan rather than batched at submit, so a receipt that was abandoned still leaves a record of what was scanned at the loading bay.
- `WithdrawalScanLog` was **not** built (the plan also marked it optional). `StockTransferItem.release_method` records scanned-vs-typed per line, which is what the audit question actually needs.
- `ProcurementItem.line_total` turned out to fix a live bug, not just serve the new print view: `procurement_detail.html` already referenced `item.line_total`, which did not exist, so every line total on that page rendered as ₱0.00.

## 9. Suggested build order

This was the order actually followed:

1. **PO Printing** (§5) — zero schema risk, immediately demoable, good first win.
2. **Supplier↔Product association** (§2) — self-contained new model, unblocks nicer PO creation later.
3. **Delivery scanning** (§1) — reuses existing scanner component; moderate risk around the "unexpected item" edge case.
4. **Stock Withdrawal scanning** (§3) — smallest diff once §1's scan-to-verify pattern exists, since it's the same UI pattern applied to `StockTransferItem`.
5. **VAT/PWD-Senior discounts** (§4) — highest risk (new computation, no existing tax logic to build on).

## 10. Still needs the Owner's / adviser's confirmation

The VAT work is built on three assumptions that no existing code could confirm, because the system had no tax logic at all before this. **Confirm these before the system is used for real sales:**

1. **Shelf prices in `Product.unit_price` are VAT-inclusive.** Everything in `sales/pricing.py` extracts VAT out of a price and never adds it on top. If prices were actually being entered VAT-exclusive, every computed figure is wrong.
2. **The 12% rate**, and that it is baked into those prices.
3. **The discount order of operations** — strip VAT first, then take 20% off the VAT-exclusive amount (RA 10754 / RA 9994), rather than 20% off the sticker price. On a ₱112.00 item this is the difference between charging ₱80.00 and ₱89.60, so it is worth confirming explicitly rather than assuming.

A fourth, smaller one: `is_pwd_senior_eligible` defaults to **True** for new products, on the assumption that most retail goods qualify and the exceptions are the minority. Every product that existed before the migration also got `True`. If a meaningful set of the catalog is actually excluded, those need untickng — the flag is per-product on the Add/Edit Product form.
