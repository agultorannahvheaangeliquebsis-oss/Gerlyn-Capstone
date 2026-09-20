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
| Order delivered | An online Delivery order moves to Completed | Customer who placed it | Low | SalesTransaction; customer order tracking |
| Payment verified | Pending payment moves to Paid | Customer attached to the order | Low | BillingRecord; customer order tracking |
| Payment rejected | Pending payment moves to Rejected | Customer; Owner; affected branch Manager/Staff with Billing access; deciding actor is excluded from matrix fan-out | High | BillingRecord; customer order tracking or internal billing detail |

Manager and Staff delivery is both branch-scoped and module-gated. Owners are
global. Supplier and customer messages use their own portal links and never
set `requires_action`; High/Critical internal notices stay unresolved until a
recipient explicitly resolves them.

## Time-based dispatch

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
