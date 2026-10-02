# GerlynPLUS QA Review — Joshua / Procurement

Date: 2026-10-02 (Asia/Manila)
Scope: Suppliers, Purchase Orders, Deliveries, receiving, supplier returns, procurement permissions, branch isolation, inventory/report/audit effects.
Method: source trace plus existing automated tests against Django's existing test database (`--keepdb`). No production records were read, changed, or deleted.

## Executive result

The procurement design has good foundations: shared branch scoping, row locks around stock mutations, atomic stock/receipt writes, positive-quantity validation, warehouse-only delivery credits, stock movements, and append-only audit capture. Exact final receiving and over-receiving are guarded correctly in the normal paths.

The review nevertheless found **1 Critical, 7 High, and 2 Medium definite technical defects**, plus **3 business-rule decisions**. The highest risks are:

1. a Branch Manager can see other branches' recent PO metadata on a supplier page;
2. repeating a partial receipt can add the same physical delivery to stock more than once;
3. the generic API can skip the negotiated procurement lifecycle and can authorize receiving under the wrong module permission;
4. deleting a supplier removes the supplier relationship from historical POs/returns;
5. the default permission matrix blocks manager actions that the procurement workflow and tests say managers should perform.

The existing procurement suite found 303 tests and ended with 12 failures. Those failures are listed under Validation.

## 1. Procurement surface map

### Routes and views

Base web path: `/procurement/`; base API path: `/api/procurement/`.

| Area | Routes | Main handlers |
|---|---|---|
| Purchase orders | `/`, `add/`, `<pk>/`, `<pk>/print/`, `<pk>/pdf/` | `procurement_list`, `procurement_add`, `procurement_detail`, `procurement_print`, `procurement_pdf` |
| PO state/actions | `<pk>/update-status/`, `<pk>/approve/`, `<pk>/funding/`, `<pk>/reschedule/`, `<pk>/communicate/`, `<pk>/respond/` | `procurement_update_status`, `procurement_approve`, `procurement_set_funding`, `procurement_reschedule`, `procurement_mark_communicated`, `procurement_owner_respond` |
| Deliveries | `deliveries/`, `deliveries/<pk>/`, `deliveries/<pk>/receive/`, `scan/`, `add-unexpected/` | `delivery_list`, `procurement_detail`, `delivery_receive`, `delivery_scan`, `delivery_add_unexpected` |
| Supplier returns | `<po>/return/`, `returns/`, `returns/<pk>/`, plus `cancel/`, `ship/`, `credit/`, `replacement/`, `respond/` | `supplier_return_add/list/detail/cancel/ship/credit/replacement/respond` |
| Suppliers | `suppliers/`, `add/`, `<pk>/`, `edit/`, `delete/` | `supplier_list`, `supplier_form`, `supplier_detail`, `supplier_delete` |
| Supplier catalog | supplier product add/remove/transfer, offer/request review | `supplier_product_*`, `supplier_offer_review`, `supplier_request_review` |
| Supplier portal | `portal/`, purchase orders, deliveries, account, respond, confirm | `supplier_portal`, `supplier_purchase_orders`, `supplier_deliveries`, `procurement_supplier_respond`, `procurement_supplier_confirm` |
| API | list/create, detail/delete, status patch, communicate, suppliers, supplier products | `ProcurementListCreateAPIView`, `ProcurementDetailAPIView`, `ProcurementStatusUpdateAPIView`, `ProcurementCommunicateAPIView`, `SupplierListAPIView`, `SupplierProductListAPIView` |

### Models and integrity records

| Model | Purpose / important links |
|---|---|
| `Procurement` | PO, branch, supplier, creator, owner/authorizer, agreed/proposed dates, funding, status, totals and receipt flag |
| `ProcurementItem` | PO line, product, ordered/received quantities, proposed and agreed cost, unexpected-item flag |
| `SupplierProduct` | supplier-product catalog/offer and current supplier cost |
| `SupplierProductRequest` | supplier proposal for a new product |
| `DeliveryReceipt` | one receiving event, receiver, date, courier text, optional replacement-return link |
| `DeliveryScanLog` | barcode scans, matched/wrong/unknown product and actor |
| `SupplierReturn` | PO, supplier, branch, reason, lifecycle, credit/replacement resolution |
| `SupplierReturnItem` | return line linked to PO item and product, returned/replaced quantities |
| `WarehouseStock` / `Batch` / `StockMovement` | aggregate warehouse quantity, received lots, and immutable quantity-change evidence |
| `AuditLog` | registered capture for PO, PO item, delivery receipt, return, return item and supplier-product changes |

### Permission and scoping layers

- Role checks live in each view/API.
- Module/action checks are applied by middleware/DRF through `core.module_permissions` and `HasModuleAccess`.
- Record-level access uses `procurements_visible_to()`, `scope_queryset()`, and `user_can_access_branch()`.
- Owner: all branches and exempt from per-user module action restrictions.
- Branch Manager: assigned branch only; fails closed if unassigned.
- Staff: assigned branch, with PO visibility restricted to delivery-relevant statuses.
- Supplier: only POs/returns tied to that supplier.
- Customer: no procurement access.

## 2. Major workflows

### PO lifecycle

1. Owner web creation creates an `Approved` PO; Manager web creation creates `Draft`.
2. Owner approval records `owner` and `authorized_at`.
3. Supplier/buyer negotiate price and delivery date, or the store records externally communicated terms.
4. Supplier confirmation moves agreed portal terms to `Incoming`; external communication uses `Communicated`.
5. Store receiving records per-line quantities, increments warehouse stock, creates batches, stock movements and a delivery receipt, then sets `Partially Received` or `Received`.
6. Floor stock is not changed by receiving; it changes through the separate warehouse-to-floor transfer workflow.

### Supplier-return lifecycle

`Requested -> Acknowledged -> Shipped -> Settled`, with supplier rejection and store cancellation branches. Warehouse stock decreases only at `Shipped`. Replacement stock increases only when replacement goods are received. Credit settlement changes the return status but does not modify PO cost/report totals.

## 3. State-transition table

This is the model's declared transition map. “Dedicated action” is the route that should supply the required business data.

| Current | Allowed target(s) | Intended action/actor | Important blocked examples |
|---|---|---|---|
| Draft | Approved, Cancelled | Owner approval; store cancellation | Incoming, Partial, Received, negotiation states |
| Approved | Pending Owner Review, Communicated, Cancelled | Supplier offer; external communication; store cancellation | Incoming/Received directly |
| Pending Owner Review | Pending Supplier Review, Terms Agreed, Communicated, Cancelled | Buyer counter/accept; external communication | Receipt states directly |
| Pending Supplier Review | Pending Owner Review, Terms Agreed, Communicated, Cancelled | Supplier counter/accept; external communication | Receipt states directly |
| Terms Agreed | Communicated, Incoming, Cancelled | External communication or supplier final confirmation | Receipt states directly |
| Communicated | Incoming, Partially Received, Received, Cancelled | Supplier acknowledgement; store receiving; cancellation | Draft/Approved/backward moves |
| Incoming | Partially Received, Received, Cancelled | Store receiving; cancellation | Any backward move |
| Partially Received | Received, Cancelled | Further receiving; cancellation | Any backward move |
| Received | none | Terminal | all |
| Cancelled | none | Terminal | all |

The web generic status handler blocks negotiation, communication, incoming and receipt targets and redirects to dedicated workflows. The API generic status handler only blocks `Communicated`; this mismatch is defect JP-PO-002.

## 4. PO-to-delivery reconciliation

| Scenario | Expected invariant | Current result |
|---|---|---|
| Exact | Sum received = sum ordered; one stock credit per accepted event; PO Received | Passes for a single normal submission. Further receipt is blocked once terminal. |
| Partial | Received increases only by submitted positive quantities; remaining stays open | Quantity/status math works, but the same partial POST can be repeated and credited again (JP-RCV-001). |
| Over | Submitted quantity greater than remaining is rejected atomically | Passes in web and API receiving paths. |
| Wrong product | Must not silently count as ordered product | Scan reports `not_on_order`; receiver may add it as an `is_unexpected` discrepancy line. Duplicate add is race-prone (JP-RCV-003). |
| Failed/damaged | Record rejection/failure without adding usable stock | No first-class failed/rejected delivery event; decision required (BR-JP-002). |
| Returned | Warehouse decreases on shipment; replacement increases on verified receipt | Stock math is atomic, but original delivery trace and report reconciliation are incomplete (JP-RET-001, BR-JP-001). |

## 5. Definite technical defects

### JP-SEC-001 — Cross-branch PO metadata leaks on supplier detail

- **Role:** Branch Manager
- **Branch:** Branch A; target records belong to Branch B
- **Preconditions:** One supplier has recent POs in both branches; manager is assigned only to Branch A and has Procurement view access.
- **Exact steps:**
  1. Log in as Branch A Manager.
  2. Open `/procurement/suppliers/<supplier_id>/`.
  3. Inspect Recent Orders.
- **Expected:** Only Branch A records, or no operational PO list if supplier management is centralized.
- **Actual:** The page queries the supplier's latest five POs without branch scoping and renders PO number, order date, Branch B name, and total. Clicking the PO is later blocked, but the data is already disclosed.
- **Severity:** **Critical** — wrong-branch operational/financial exposure.
- **Affected:** `procurement.views.supplier_detail` (`recent_orders`); `templates/procurement/supplier_detail.html` Recent Orders table.
- **Reproducible:** **Yes**, deterministically from the query and template.

### JP-RCV-001 — Repeating a partial receipt double-increases stock

- **Role:** Owner, Branch Manager, or Staff with Deliveries Edit; API variant also affects Owner/Manager with API status access.
- **Branch:** Any branch with a warehouse.
- **Preconditions:** Incoming/Communicated PO line ordered quantity 10, received 0.
- **Exact steps:**
  1. Submit receipt quantity 4 for the line.
  2. Repeat the same POST by refresh, retry, double-click/concurrent tabs, or repeat the same API payload.
  3. Inspect PO item, warehouse stock, batches, stock movements and delivery receipts.
- **Expected:** The same delivery event is applied once; quantity remains 4 unless a distinct second shipment/reference is supplied.
- **Actual:** First request leaves the PO `Partially Received` with remaining 6. The repeated request is still legal and adds another 4, producing received/stock 8 plus a second batch, movement and receipt. Row locking prevents over-receipt, but it does not provide idempotency for partial receipts.
- **Severity:** **High** — core stock can be overstated.
- **Affected:** `procurement.views.delivery_receive`; `procurement.serializers.ProcurementStatusSerializer.update`; `DeliveryReceipt` lacks an idempotency/delivery reference.
- **Reproducible:** **Yes** for a repeated partial quantity not exceeding the post-first-receipt remainder.

### JP-PO-002 — Generic API can skip negotiation and create impossible PO states

- **Role:** Owner; also any Branch Manager explicitly granted Procurement Edit.
- **Branch:** Own branch for Manager; any branch for Owner.
- **Preconditions:** Approved PO with at least one line.
- **Exact steps:**
  1. `PATCH /api/procurement/<id>/update_status/` with `{"status":"Pending Owner Review"}` and no proposed date/costs.
  2. PATCH again to `Terms Agreed` with no accepted proposal.
  3. PATCH again to `Incoming` with no supplier final confirmation.
- **Expected:** Generic status writes reject negotiation/incoming targets; dedicated endpoints must validate prices, date, actor and turn.
- **Actual:** The API blocks only `Communicated`. It accepts any target allowed by the model map and the serializer merely writes the status for non-receipt states. Required negotiation data and supplier confirmation can be skipped.
- **Severity:** **High** — approval/procurement lifecycle bypass and inconsistent cost/date state.
- **Affected:** `ProcurementStatusUpdateAPIView.patch`; `ProcurementStatusSerializer.update`; contrast `procurement_update_status` web guard.
- **Reproducible:** **Yes**, by direct API requests.

### JP-PO-003 — API PO creation/approval contradicts the web lifecycle

- **Role:** Owner or Branch Manager using API.
- **Branch:** Accessible branch.
- **Preconditions:** Valid supplier/product/branch.
- **Exact steps:**
  1. POST a PO to `/api/procurement/` as Owner, optionally with `expectedDelivery`.
  2. Compare with web Owner creation.
  3. PATCH Draft to Approved.
- **Expected:** One lifecycle across UI/API: delivery date set only by negotiation/external communication; Owner creation authorization behavior consistent; approval must not snapshot standing catalog cost in place of negotiated terms.
- **Actual:** API accepts and persists `expectedDelivery` at Draft creation, always creates Draft even for Owner while pre-populating `owner`, and API approval calls `snapshot_supplier_costs()`. Web Owner creation creates Approved, does not accept delivery date, and web approval intentionally snapshots no costs pending negotiation.
- **Severity:** **High** — status, authorizer, date and cost integrity differ by entry point.
- **Affected:** `ProcurementWriteSerializer` fields/create; `ProcurementStatusSerializer.update`; `procurement_add`; `procurement_approve`.
- **Reproducible:** **Yes**. Existing tests also encode both conflicting behaviors, showing implementation drift.

### JP-RBAC-001 — API receiving checks Procurement Edit instead of Deliveries Edit

- **Role:** Branch Manager with Procurement Edit granted but Deliveries Edit denied.
- **Branch:** Manager's branch.
- **Preconditions:** Receivable PO and configured warehouse; custom action permissions as above.
- **Exact steps:**
  1. Confirm the web `/deliveries/<id>/receive/` action is denied by the Deliveries permission.
  2. PATCH `/api/procurement/<id>/update_status/` to `Received` with item quantities.
- **Expected:** Both receiving surfaces require Deliveries Edit.
- **Actual:** `api-procurement-status` is registered under Procurement Edit and the endpoint performs the complete stock-credit operation. A user denied receiving can therefore receive through the API if granted PO editing.
- **Severity:** **High** — action-level authorization bypass with inventory impact.
- **Affected:** `core.module_permissions.MODULE_PERMISSION_REGISTRY`; `ProcurementStatusUpdateAPIView`; `ProcurementStatusSerializer.update`.
- **Reproducible:** **Yes**, with the specified permission combination.

### JP-RBAC-002 — Default manager permissions block documented procurement/return actions

- **Role:** Branch Manager using role defaults.
- **Branch:** Manager's own branch.
- **Preconditions:** Default `ROLE_DEFAULT_ACTIONS`; own-branch draft/open PO or return.
- **Exact steps:**
  1. Log in as a default Branch Manager.
  2. Try own-branch draft cancellation, funding edit, delivery reschedule, return cancellation/shipping/credit/replacement.
  3. Compare role checks and page comments with middleware result.
- **Expected:** The repository's views, comments and regression tests describe Owner/Branch Manager as return managers and allow own-branch non-approval procurement actions.
- **Actual:** Manager default has Procurement `[ADD]` only. Middleware rejects Procurement Edit/Delete before those view rules run. Current suite failures confirm these actions do not execute.
- **Severity:** **High** — core own-branch workflows become unavailable.
- **Affected:** `core.module_permissions.ROLE_DEFAULT_ACTIONS`; procurement status/funding/reschedule/return routes.
- **Reproducible:** **Yes**; multiple current automated tests fail.
- **Decision note:** If the approved RBAC matrix intentionally reserves all these actions to Owner, then the views, UI wording and tests must be changed together. As committed, the layers contradict each other.

### JP-SUP-001 — Supplier deletion breaks historical supplier/return traceability

- **Role:** Owner
- **Branch:** Any
- **Preconditions:** Supplier has only Received/Cancelled POs, optionally with supplier returns.
- **Exact steps:**
  1. POST `/procurement/suppliers/<id>/delete/`.
  2. Reopen historical PO/return/export/audit context.
- **Expected:** Supplier is deactivated/retired and cannot be selected for new POs, while historical records keep the original supplier identity/link.
- **Actual:** `supplier.delete()` physically deletes the User. PO and SupplierReturn supplier FKs use `SET_NULL`; catalog/request/profile rows cascade. Historical records remain but lose their supplier relationship/name, directly defeating return traceability.
- **Severity:** **High**.
- **Affected:** `procurement.views.supplier_delete`; `Procurement.supplier`; `SupplierReturn.supplier`; `SupplierProduct.supplier`; `SupplierProfile.user`.
- **Reproducible:** **Yes**. This review did not execute the destructive step.

### JP-RET-001 — A return cannot identify the original delivery event

- **Role:** Owner/Branch Manager reviewing or creating a return.
- **Branch:** PO branch.
- **Preconditions:** Same PO/product received in two or more partial deliveries.
- **Exact steps:**
  1. Receive the product in two separate events.
  2. Create a return for some units.
  3. Trace the return to the exact original delivery receipt.
- **Expected:** Return line links to the correct PO, delivery, supplier, product and branch.
- **Actual:** Return links PO/PO-item/supplier/product/branch, but neither `SupplierReturn` nor `SupplierReturnItem` links the original `DeliveryReceipt`. `DeliveryReceipt` itself has no per-line quantities, and batches/movements are linked to the PO rather than receipt. The source shipment is therefore ambiguous.
- **Severity:** **High** — required chain-of-custody/reconciliation is unavailable.
- **Affected:** `DeliveryReceipt`, `SupplierReturn`, `SupplierReturnItem`, receiving batch/movement creation.
- **Reproducible:** **Yes** by model inspection and any multi-receipt PO.

### JP-PO-004 — Duplicate PO submissions are not idempotent

- **Role:** Owner or Branch Manager with Procurement Add.
- **Branch:** Accessible branch.
- **Preconditions:** Valid PO form/API payload.
- **Exact steps:**
  1. Submit the same create request twice (double-click, retry, two tabs).
  2. List POs.
- **Expected:** One intended PO or a duplicate-submission warning/token check.
- **Actual:** Each request independently creates a new PO and lines. `po_number` uniqueness only gives each duplicate a different number; no idempotency key or business duplicate check exists.
- **Severity:** **Medium** — duplicate commitments require manual detection/cancellation.
- **Affected:** `procurement_add`; `ProcurementListCreateAPIView.post`; `ProcurementWriteSerializer.create`; `Procurement.save`.
- **Reproducible:** **Yes** by code path; no destructive/live execution performed.

### JP-VAL-001 — Malformed receiving/supplier edits can produce server errors

- **Role:** Authenticated permitted user with a crafted request.
- **Branch:** Accessible branch.
- **Preconditions:** Receivable PO, or editable supplier.
- **Exact steps:**
  1. POST a receipt with an `item_id` belonging to another PO/nonexistent row, or a syntactically parseable-looking but impossible expiry date.
  2. Separately, edit a supplier username to another existing username or post a nonexistent branch ID.
- **Expected:** Form-level 400/error message; no mutation.
- **Actual:** Receiving uses `.get(pk=item_id)` without converting `DoesNotExist` to the workflow error and parses expiry inside the transaction without handling `ValueError`. Supplier edit validates duplicate username only on create and assigns `profile.branch_id` without validating existence. These paths can raise unhandled ORM/database exceptions (transaction rollback occurs where atomic).
- **Severity:** **Medium** — reproducible 500/availability and validation defect; no confirmed cross-record mutation.
- **Affected:** `delivery_receive`; `supplier_form`.
- **Reproducible:** **Yes** with crafted input, based on direct exception paths.

## 6. Business-rule decisions (not asserted as definite defects)

### BR-JP-001 — Should procurement reports be gross or net of supplier returns?

- **Test ID:** BR-JP-001
- **Role/branch:** Owner or scoped Manager/Staff; any branch with a settled supplier return.
- **Preconditions:** Received PO, shipped return, then credit or replacement settlement.
- **Steps:** Compare PO, warehouse stock/movements, supplier return and Procurement Report/CSV.
- **Expected:** **Decision required.** If reports represent net procurement/usable goods, returned quantity/value and credits must be visible and reconciled. If reports deliberately represent original commitments, label them as gross and add a separate returns measure.
- **Actual:** Report totals continue to use PO `total_cost`; CSV `actual_received_cost` remains based on cumulative received quantity. Return/credit quantities and values are not subtracted or shown.
- **Severity if net reporting is required:** Medium/High.
- **Affected:** `reports.views.procurement_report`, `reports.services.procurement_cost_total`, procurement CSV export/template.
- **Reproducible:** Yes.

### BR-JP-002 — No explicit failed/rejected/damaged delivery-receipt state

- **Test ID:** BR-JP-002
- **Role/branch:** Receiver in PO branch.
- **Preconditions:** Supplier arrives with all goods damaged, wrong, or refused.
- **Steps:** Attempt to record zero accepted quantity or a failed delivery.
- **Expected:** **Decision required.** Either record a failed/rejected delivery event without inventory impact, or formally define that no receipt is created and evidence lives outside the system.
- **Actual:** Zero accepted quantity is rejected and no `DeliveryReceipt` is created. Damaged goods can only be received into stock then returned, or left unrecorded; there is no failed delivery state/reason.
- **Severity if auditable failed deliveries are required:** Medium.
- **Affected:** `Procurement.Status`, `DeliveryReceipt`, `delivery_receive`, receiving templates.
- **Reproducible:** Yes.

### BR-JP-003 — Duplicate supplier identity rule is undefined

- **Test ID:** BR-JP-003
- **Role/branch:** Owner; supplier may be global or branch-assigned.
- **Preconditions:** Existing supplier.
- **Steps:** Create another supplier with same name/contact/email but a different username.
- **Expected:** **Decision required.** Define whether uniqueness is by legal/name normalization, email, phone, tax/business identifier, branch, or only login username.
- **Actual:** Only username uniqueness is enforced. Same supplier name/contact/email is accepted; case/spacing normalization is not used for supplier identity.
- **Severity if duplicates are prohibited:** Medium.
- **Affected:** `supplier_form`, `User.username`, `SupplierProfile` fields.
- **Reproducible:** Yes.

## 7. Passing controls observed

- Missing/zero/negative PO quantities are rejected in web/API create paths.
- Missing/inactive supplier and unknown product IDs are rejected; Manager branch is server-selected and API branch access is validated.
- Multiple PO lines are supported and duplicate product IDs within one payload are rejected.
- Non-Owner draft approval is blocked in both web/API business logic.
- Received and Cancelled are terminal in the model transition map.
- PO line editing after approval is not exposed by a route/API.
- Over-receiving is rejected against remaining quantity inside an atomic transaction.
- Normal receiving updates warehouse stock, not floor stock, and creates stock movements/batches/receipt records.
- Final exact receiving is protected from a second receipt by terminal status plus a row lock.
- Return quantities are limited by received, already-claimed and still-in-PO-warehouse-lot quantities.
- Return shipping and replacement receiving use atomic row locks and create stock movements.
- Supplier portal PO/return access is tied to the logged-in supplier.
- Main PO/detail/delivery/return querysets generally fail closed by branch; the supplier-detail recent-order subquery is the identified exception.
- Procurement, receipt and return models are included in centralized audit capture.

## 8. Scope audit

No implementation of actual online payment processing, courier booking/tracking integration, HR/payroll, external accounting/enterprise integration, or replacement POS hardware/software was found in the reviewed procurement flow. Courier/driver is only free-text receipt metadata. Payment references elsewhere are documented as recorded/verified rather than processed.

Potential scope note: third-party browser assets/CDNs and a bundled barcode-scanning library exist, but they are supporting UI dependencies rather than courier/enterprise integration or POS replacement. They are not marked as procurement scope creep here.

## 9. Validation evidence

Commands run:

```text
.\venv\Scripts\python.exe manage.py test procurement --verbosity 1
```

The first run stopped safely because `test_gerlyn_db` already existed and Django requested permission to delete it. No deletion was authorized.

```text
.\venv\Scripts\python.exe manage.py test procurement --keepdb --verbosity 1
```

Result: **303 tests, 12 failures, 0 reported errors**, 169.684 seconds. Failures:

- manager could not cancel own Draft;
- manager own-branch status update did not occur;
- two foreign-branch status tests redirected at module permission before record-level behavior;
- manager funding edit did not occur;
- manager delivery reschedule did not occur;
- return cancel, ship/credit and replacement actions did not occur;
- return action controls were absent for the manager;
- return GET/action and other-role status expectations differed because module middleware redirected first.

These failures are consistent with JP-RBAC-002 rather than stock-service arithmetic failures. The suite also emits tests that intentionally preserve legacy API behavior conflicting with the newer negotiation workflow, supporting JP-PO-003's cross-surface drift finding.

## 10. Recommended remediation order

1. Fix JP-SEC-001 branch leakage and add a manager foreign-branch supplier-detail regression test.
2. Introduce receipt idempotency/a real delivery reference and per-receipt line records; use them for returns (JP-RCV-001, JP-RET-001).
3. Remove receiving from generic PO status updates or make it call one shared receiving service guarded by Deliveries Edit (JP-PO-002, JP-RBAC-001).
4. Make web/API creation and approval use one service and one negotiation rule (JP-PO-003).
5. Replace supplier hard delete with reversible deactivation/retirement and retain historical identity (JP-SUP-001).
6. Resolve the manager permission-matrix decision, then align middleware registry, role defaults, UI and tests (JP-RBAC-002).
7. Add idempotency to PO creation and close the malformed-input exception paths.
8. Decide and document the three business rules before changing reports or failed-delivery behavior.
