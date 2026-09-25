# Operational notification matrix

`core.notifications.EVENT_MATRIX` is the executable source of truth. Every
fan-out creates one `accounts.Notification` row per recipient so read and
resolution state remain personal. The table records a stable event code, a
snapshot of the recipient's role, creation/read timestamps, and the referenced
business record plus its action link.

| Event | Fires when | Recipients | Priority | Reference / action |
|---|---|---|---|---|
| Low stock | Floor or warehouse stock crosses from above to at/below reorder level; zero units escalates to Critical | Owner; affected branch Manager/Staff with access to the affected stock module | Medium / Critical | Inventory or WarehouseStock row; stock/alert page |
| Near expiry | A positive batch enters the next 30 days before expiry | Owner; affected branch Manager/Staff with Alerts access | High | Batch; Expiring Soon tab |
| PO approved | Owner authorizes a Draft PO, or creates an already-authorized PO | Requester and supplier; approving actor is excluded | Medium | Procurement; internal detail or supplier portal |
| Incoming delivery | Supplier first confirms or changes a PO delivery date | Owner; affected branch Manager/Staff with Deliveries access | Medium | Procurement; Deliveries list |
| Supplier offer for review | A supplier offers a master-list product or requests a new one | Owner only | Medium | Supplier; that supplier's detail page |
| Supplier offer decision | The Owner approves or rejects an offer or new-product request | The supplier who made it | Medium | Supplier; supplier portal |
| PO overdue | An Incoming / Partially Received PO is still undelivered 1 day (`PO_OVERDUE_GRACE_DAYS`) after its delivery date; once, by the daily sweep | Owner; affected branch Manager/Staff with Deliveries access; the supplier | High | Procurement; PO detail / supplier portal |
| PO overdue — escalated | Still undelivered 3 days (`PO_OVERDUE_ESCALATE_DAYS`) after its delivery date; once | Owner only | Critical | Procurement; PO detail |
| Supplier return update | The store raises, ships, settles or cancels a return to a supplier | The supplier | Medium | SupplierReturn; supplier portal |
| Supplier return response | The supplier acknowledges or rejects a return request | Owner; affected branch Manager/Staff with Procurement access | Medium (High if rejected) | SupplierReturn; return detail |
| Order delivered | An online Delivery order moves to Completed | Customer who placed it | Low | SalesTransaction; customer order tracking |
| Payment verified | Pending payment moves to Paid | Customer attached to the order | Low | BillingRecord; customer order tracking |
| Payment rejected | Pending payment moves to Rejected | Customer; Owner; affected branch Manager/Staff with Billing access; deciding actor is excluded from matrix fan-out | High | BillingRecord; customer order tracking or internal billing detail |
| Expiry write-off | The daily expiry sweep removes one or more due batches, unattended (no actor) | Owner; affected branch Manager/Staff with access to the affected stock module | High | One notice per (branch, Floor/Warehouse Stock) group per sweep run — never per batch; Expired tab |

Manager and Staff delivery is both branch-scoped and module-gated. Owners are
global. Supplier and customer messages use their own portal links and never
set `requires_action`; High/Critical internal notices stay unresolved until a
recipient explicitly resolves them.

## Time-based dispatch

`dispatch_notifications` also runs the overdue-delivery sweep (see below).

New batches that are already within 30 days of expiry notify immediately. A
batch can also become near-expiry merely because another day passed, so run
this idempotent command once per day in production:

```bash
python manage.py dispatch_notifications
```

Before dispatching notifications, the same command removes due floor and
warehouse batches and records one batch-linked `Expiry Write-off` movement
per lot. Both steps are idempotent: repeated runs neither deduct stock nor
create notifications twice.

## Overdue deliveries

`python manage.py process_overdue_deliveries` (also run by
`dispatch_notifications`) flags every Incoming or Partially Received purchase
order that is past its delivery date. Two stages, each recorded on the order so
it fires exactly once and re-running is harmless: **overdue** after
`PO_OVERDUE_GRACE_DAYS` (default 1) notifies the buyer side and the supplier;
**escalated** after `PO_OVERDUE_ESCALATE_DAYS` (default 3) notifies the Owner
again at Critical priority. Nothing is auto-cancelled: the Owner cancels the
order or reschedules the delivery date from the PO detail page, which clears
the flags so a new date is tracked afresh.
