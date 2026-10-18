# 05 — Live Defense Script

**How to use this file.** It is split into **nine independent sections**. Each is
self-contained, so you can divide them between group members however you like —
no section depends on who delivered the one before it. No durations or speaker
names are assigned; that is your call.

Each section has the same five parts:

- **Presenter script** — natural English you can speak as written
- **What to show** — the exact page to have on screen
- **Flowchart to display** — which diagram from this folder
- **Key technical explanation** — the precise version, for a technical follow-up
- **Possible panel questions** — short answers; the full set is in [`06-panel-questions-and-answers.md`](06-panel-questions-and-answers.md)

**Three rules for the whole presentation:**

1. **Never say "the system processes the payment."** Say *"records and verifies."*
   There is no payment gateway, by design.
2. **Never say "it syncs the order to Sales & Orders."** There is nothing to sync
   — the storefront writes the same record the POS writes.
3. **If you don't know, say "we'd have to check the code."** Everything in this
   folder is code-verified; nothing outside it is.

---

## Section 0 — Safe demo setup (do this before the defense)

> ⚠️ **Read this before any live transaction.** A live demo writes real rows:
> orders, billing records, stock movements, notifications. Some of it cannot be
> undone cleanly.

### What a live demo actually writes

| Action | Permanent effect |
|---|---|
| Place an order | `SalesTransaction` + `SalesItem` + `BillingRecord` + `OrderStatusHistory`, and consumes an `order_number` from `OrderNumberSequence` |
| Mark Completed | **Deducts real stock** and writes a `StockMovement` |
| Verify a payment | `BillingRecord.status='Paid'` — **this cannot be undone.** `Paid` has no transition back to `Pending` |
| Cancel an order | **Terminal.** `Cancelled` has no outbound transition |
| Reject a payment | Can be re-opened to `Pending` ✅ |

### Recommended setup

1. **Use a seeded demo database, not production data:**
   `python manage.py seed_demo --reset` — **destructive to local application
   data.** Run it well before the defense, not during.
2. **Create a dedicated demo customer account** (e.g. `demo_shopper`) with a
   **default delivery address already saved**, so the Delivery path works. An
   account with no address is refused at checkout.
3. **Pre-create two staff accounts at the same branch.** You need two people to
   demonstrate payment verification — the maker/checker rule refuses the Staff
   member who recorded a walk-in sale from verifying it.
4. **Pre-stage one order at each stage** so you are not forced to demonstrate
   live if the network is slow:
   - one `Pending` online order with an unverified GCash payment
   - one `Preparing` order with a verified payment (ready for Mark Completed)
   - one `Completed` order
   - one `Cancelled` order
   - one order with a `Rejected` payment
5. **Note a product's current stock number before the demo.** You will point at
   it changing.
6. **Have two browser windows or profiles** — one signed in as the customer, one
   as staff. Switching accounts in one window wastes time and risks a wrong-role
   redirect mid-sentence.

### Static/CSS note for whoever runs the laptop

In `DEBUG` mode, CSS is served as `/static/css/gerlyn.css?v=APP_VERSION`. The dev
server runs with `--noreload`, so if anyone edits a template or CSS file the day
of the defense, **restart the server** — otherwise the old version is cached and
you will be presenting a stale page.

---

## Section 1 — Purpose of Financials and its relationship to other modules

### Presenter script

> "GerlynPLUS is an information system for a multi-branch variety store. It has
> nine working modules, and the one we'll focus on is **Financials**, which has
> two parts: **Sales & Orders**, and **Billing**.
>
> Financials is where money and goods meet. Every other module either feeds it or
> reads from it. Procurement brings stock in. Inventory holds it and tracks every
> movement. The Storefront is how a customer places an order. Reports read the
> finished transactions. And notifications tell the right person when something
> needs attention.
>
> There are exactly **two doors** into Financials. A staff member can ring up a
> walk-in sale at the point of sale. Or a customer can place an order online
> through the storefront. Both doors write the **same kind of record** — the same
> database table, the same line items, the same billing record. There's no
> copying between systems and no synchronisation step, because there's only ever
> one record.
>
> One thing we want to state clearly up front, because it shapes the whole
> Billing module: **this system does not process payments.** There is no payment
> gateway integrated. It **records** payments and lets staff **verify** them. For
> a GCash payment, the reference number and the screenshot the customer uploads
> *are* the record that the payment happened. That's a documented limitation in
> our Chapter 1, and the code is built around it rather than pretending
> otherwise."

### What to show

The Owner dashboard, then the left sidebar — point at **Sales & Orders** and
**Billing** under Financials, and at Inventory, Procurement and Reports around
them.

### Flowchart to display

[`00-system-overview.md`](00-system-overview.md) §5 — the context diagram.

### Key technical explanation

`orders/` has **no `models.py`**. The storefront's checkout view writes
`sales.SalesTransaction` and `sales.SalesItem` directly. `BillingRecord` is
created by a `post_save` signal on `SalesTransaction` inside the same atomic
block, and `BillingRecord.transaction` is a `OneToOneField` — so one order has at
most one billing record, enforced by the database.

### Possible panel questions

**"How do you keep the online order and the sales record in step?"**
> "We don't have to. They're the same row. The storefront writes
> `SalesTransaction` directly — there's no second table to keep in step.

**"Why no payment gateway?"**
> "It's a documented scope limitation. A real gateway needs a merchant account
> and PCI considerations outside a capstone's scope. What we built instead is the
> verification workflow a store actually needs: the payment is recorded with its
> reference and proof, and a *different* staff member checks it before the order
> can be completed."

---

## Section 2 — The Sales & Orders workflow

### Presenter script

> "Let's start at the counter. A staff member opens the Walk-in Sale form. The
> products they can see are only the ones **their own branch** actually stocks —
> the list is scoped to their branch, so they can't ring up something the branch
> has never carried.
>
> They add items and quantities, choose a payment method, and for cash they enter
> the amount tendered. When they submit, the server checks everything: that the
> quantities are valid, that the products still exist, that the branch is one
> this account is allowed to sell for, and that the cash tendered covers the
> total. If anything is wrong, **the form comes back with the basket still
> intact** and every problem listed. That matters at a real counter — an earlier
> version redirected on error and threw away everything the cashier had rung up.
>
> Prices are never taken from the form. The server looks up each product's price
> itself, including bulk price tiers, and works out VAT and any PWD or Senior
> discount using one shared pricing module. The same module prices the online
> cart and prints the receipt, so those three can never disagree.
>
> Then the sale is written. For a **cash** sale, the order is Completed
> immediately and the payment is marked Paid — the money crossed the counter, so
> it's done. **Stock comes off the shelf right there**, in the same database
> transaction. If there turns out not to be enough stock on any single line, the
> **entire** sale rolls back. You never get half a sale.
>
> For a GCash or card walk-in, it's different. The goods still leave, so stock is
> still deducted — but the order stays **Pending** and the payment stays
> **Pending** until someone else verifies it.
>
> Now the online side. An online order starts as **Pending** and moves Pending →
> Approved → Preparing → Completed. Or it can be Cancelled from any of those
> stages. And here's the key difference: **an online order does not touch stock
> when it's placed.** The goods haven't been handed to anybody. Stock only comes
> off the shelf when staff mark it **Completed**.
>
> That's what makes stock deduction exactly-once. A walk-in deducts at sale time;
> an online order deducts at completion. The two never both fire for the same
> order.
>
> One more rule, and it's the one we're most careful about: **an order cannot be
> marked Completed until its payment is verified as Paid.** The system locks the
> billing record, re-reads it, and refuses if it isn't Paid."

### What to show

1. `/sales/add/` — the Walk-in Sale form. Point at the branch-scoped product list.
2. Submit with a deliberate error (blank quantity on one row) — show the errors
   **and the surviving basket**.
3. Complete a cash sale. Land on Order Detail. Point at the status, the payment
   showing **Paid**, and the recorded-by line.
4. `/sales/` — the Order Console. Show the status tabs and counts.
5. Open a `Preparing` online order. Point at the **Mark Completed** button and
   whether it is live.

### Flowchart to display

[`01-sales-and-orders.md`](01-sales-and-orders.md) §2 (POS) and §3 (the
lifecycle state diagram).

### Key technical explanation

`transaction_add` collects validation errors into a list and re-renders with
`pos_errors` + `pos_restore_rows` rather than redirecting. Pricing is
`sales.pricing.price_cart`. The write is one `transaction.atomic()` block
containing `Branch.select_for_update()`, the `SalesTransaction` INSERT (which
allocates `order_number` from a locked `OrderNumberSequence` row), the
`SalesItem` rows, and `_deduct_stock`, which locks each `Inventory` row with
`select_for_update()` and raises `_OrderActionError` on a shortfall — rolling
back the whole block.

`transaction_set_status` is the **only** writer of `order_status`. It locks the
order, honours an `expected_status` echo so a stale confirmation dialog is
refused, validates against `_next_actions(txn, user)`, and for `Completed` locks
and re-reads `BillingRecord` requiring `status='Paid'`.

### Possible panel questions

**"Could a cashier sell stock the branch doesn't have?"**
> "No. Two guards. The product dropdown is filtered to products with an inventory
> row at that branch. And `_deduct_stock` locks each row and compares against
> `available_qty` — the non-expired portion — and raises an error that rolls back
> the whole sale if it's short."

**"What if two cashiers sell the last unit at the same time?"**
> "Each one's `_deduct_stock` takes a `SELECT FOR UPDATE` row lock on that
> inventory row. The second one waits, then sees the real remaining quantity and
> is refused. Its whole sale rolls back."

**"Why is a GCash walk-in Pending if the goods already left?"**
> "Because the money hasn't been confirmed. The goods leaving and the money
> arriving are two different facts, and we record them separately. Stock is
> deducted because the goods physically left; the order stays Pending because
> nobody has verified the transfer yet."

---

## Section 3 — The Billing and payment workflow

### Presenter script

> "Every order gets a billing record **automatically**, the moment the order is
> created, by a database signal. There's no 'create billing' step a staff member
> can forget. And because it's a one-to-one relationship, the database itself
> prevents a second billing record for the same order — duplicate billing isn't
> something we have to check for, it's something the schema forbids.
>
> The billing record starts as **Pending**, carrying the amount due.
>
> The payment methods offered depend on the **channel**. A walk-in sale can be
> Cash, Card, GCash or Bank Transfer. An online order can be Cash on Delivery,
> Pay at Store, GCash or Bank Transfer. Earlier, all six were offered to both —
> which meant a walk-in sale could be marked 'Cash on Delivery', which the
> counter can't act on. Now the dropdown only shows the valid ones, **and the
> server re-checks it** when the form is submitted, so a tampered form can't get
> around it.
>
> Which methods need evidence is defined once, in the model. GCash and Bank
> Transfer need **both** a reference number and a proof image. A card payment
> needs the proof, because a card has no reference the counter can type in. Cash,
> Cash on Delivery and Pay at Store leave nothing to attach.
>
> Then there are two separate steps, and this is the part we want to be clear
> about. **Recording** a payment is saving what the customer handed over.
> **Verifying** it is accepting it. They're deliberately different actions, and
> often different people.
>
> When a staff member clicks Verify, five things are checked. One — **can this
> person verify this?** A staff member who rang up a walk-in sale **cannot verify
> its own payment.** Someone else has to check it. That's maker-checker
> separation. Owners and Branch Managers are trusted to do both. Two — is this
> transition even legal? Three — if there's an unresolved PWD discount request,
> the amount due isn't final yet, so it's blocked. Four — is the required evidence
> actually on file? And five — if it's a rejection, is there a written reason?
>
> Once a payment is **Paid**, it cannot be un-paid. There's no transition from
> Paid back to Pending. The only way out is a void or a refund, which are their
> own workflows with their own permissions.
>
> A **rejection**, on the other hand, is recoverable. It needs a written reason,
> the customer is notified with that reason, and staff can re-open the record to
> Pending for a fresh attempt.
>
> And now the three words we're careful not to mix up. A **completed sale** means
> the goods were handed over. A **paid payment** means someone verified the money
> arrived. An **order marked completed** is the first of those. They are three
> different fields written by three different actions. The only dependency the
> system enforces is one-way: you cannot complete an order whose payment isn't
> Paid. The reverse isn't automatic — verifying a payment doesn't complete the
> order. A human still has to press the button."

### What to show

1. `/billing/` — the Billing list. Point at the status tabs and the
   **Outstanding** total.
2. Open a `Pending` GCash record. Show the reference number and **the uploaded
   proof image**.
3. Click **Verify** as the *same* staff member who recorded it → show the
   maker-checker refusal.
4. Switch to the second staff account → verify it. Show the success message
   naming the next step: *"The order can now be marked Completed."*
5. Go back to the order. **Mark Completed** is now live.
6. Open a different `Pending` record and **Reject** it with a reason. Show the
   reason stored on the record.

### Flowchart to display

[`02-billing-and-payments.md`](02-billing-and-payments.md) §4 (the status
lifecycle) and §6 (the five gates).

### Key technical explanation

`billing.signals.auto_create_billing_record` fires on `post_save` with
`created=True`. `BillingRecord.ALLOWED_STATUS_TRANSITIONS` is the legal map;
`HUMAN_CONFIRMABLE_STATUSES = {Pending, Paid, Rejected}` is the narrower set a
person may pick, which is what stops an API caller PATCHing a Pending record
straight to `Cancelled` or a Paid one to `Refunded`, bypassing the order's own
workflows. `is_self_confirmation(user)` returns
`user.role == STAFF and self.transaction.staff_id == user.id` — Owner and Manager
fail the role test, so the role condition *is* the override.
`missing_payment_evidence` is re-checked at verification, not only at save,
because `Paid` is irreversible. `record_payment_details` locks the row, re-reads
the status, and writes **only** the payment columns.

### Possible panel questions

**"Can one order have two billing records?"**
> "No — `BillingRecord.transaction` is a `OneToOneField`, so there's a unique
> constraint in the database. It's not a check we perform; it's impossible."

**"Can a staff member mark their own sale as paid?"**
> "Not for a walk-in they rang up. `is_self_confirmation` compares the sale's
> staff member against the person clicking Verify. Owners and Branch Managers are
> exempt by role. And an online order has no staff member on it, so this doesn't
> block online payments."

**"What stops someone marking a payment Paid without any proof?"**
> "`missing_payment_evidence` is checked again at verification. If the method
> needs a reference or a proof image and it isn't on file, Verify is refused and
> the message names exactly what's missing."

---

## Section 4 — The Storefront and online ordering workflow

### Presenter script

> "Now the customer side. A visitor can browse without an account — the catalog,
> product pages, search, categories. But the storefront **only lists what it can
> actually sell**. Products out of stock aren't shown; neither are categories with
> nothing in them. A card you can see is a card you can buy from.
>
> We were deliberate about what the product card does *not* show. There's no star
> rating, because we have no review model. There's no struck-through 'was' price,
> because each product has one price and a fake 'was' price is a false discount
> claim. Where the design reference had a star rating, we put **'N sold'** — which
> is a real figure, summed from actual sales.
>
> An account is required at **Add to cart**, not before. If you're not signed in,
> you're sent to log in and brought straight back to what you were doing.
>
> Now location, and this is the part that's easiest to misunderstand, so let us be
> precise. **The customer does not choose a branch.** There's a 'Set location'
> control, and it is **not** a branch picker. The customer shares their location,
> or we use their saved address, and then the **server** picks the branch — and it
> picks the one branch that can fulfil the **entire** cart.
>
> We never split an order across branches. If no single branch can cover
> everything, we say so, and we **name the products that are blocking it** so the
> customer knows what to change.
>
> And the availability we check against isn't just what's on the shelf. It's
> what's on the shelf **minus what other open online orders have already been
> promised** — because an online order doesn't deduct stock until it completes.
> Without that subtraction, ten shoppers could all be promised the same last unit.
>
> On privacy: we store **no coordinates** in the database from this. Browser
> location lives in the session for 30 minutes and then expires. What we keep is
> the branch it resolved to and a rounded distance for display.
>
> The cart lives in the session. Every time it changes, the branch is re-resolved
> and every line re-priced at the tier that line's quantity earns. If an item
> becomes unavailable, it's **removed and the customer is told** — not silently
> dropped, which used to make the total shrink with no explanation.
>
> At checkout: the customer picks delivery or pickup, and a payment method. A few
> rules the server enforces. Delivery ships to the address **saved on the
> account** — there's no address box on the checkout form, because one structured
> saved record beats a free-text box retyped on every order. If Delivery is chosen
> and there's no saved address, we refuse and tell them where to add one. 'Pay at
> Store' isn't offered for delivery, because delivery never puts anyone at the
> counter. And for GCash, the reference number and screenshot are **required
> before the order is created at all** — so there's no such thing as an
> unverifiable GCash order sitting in our system.
>
> One thing checkout deliberately **does not** do: apply a PWD or Senior
> discount. An earlier version let the customer claim it by typing an ID number,
> which means 'MADE-UP-123' bought a permanent twenty percent discount. Nobody
> ever saw a card. Now **every order checkout writes is at full price.** What a
> customer *can* do, if they're collecting in store, is **request** the discount.
> That records what they typed, changes no money, and staff approve or reject it
> when they see the actual ID at pick-up. And billing **will not let the order be
> marked Paid** while that request is unresolved — so the ID gets checked before
> the money is settled, enforced by the system rather than by remembering.
>
> When the order is placed, everything is written in one database transaction —
> the order, its line items, and its billing record. And **stock does not move.**
> The goods are still on the shelf, because nobody has handed them to anyone.
>
> After that, the customer tracks it under My Orders."

### What to show

1. `/` then `/shop/` — browse as a **guest**. Point at categories and the "N sold"
   line.
2. Press **Add to cart** as a guest → the login redirect.
3. Sign in as the demo customer → you land **back on the product**.
4. `/shop/branch/` — **Set location**. Show the branch it resolved to and the
   distance.
5. Add items → `/shop/cart/`. Point at the per-line prices and the branch.
6. Check out choosing **Pickup + GCash**, with a reference and a screenshot. Try
   it **without** the reference first to show the refusal.
7. The confirmation page with the order number.
8. **Then show the product's stock number is unchanged** — this is the single most
   convincing moment of the demo.

### Flowchart to display

[`03-storefront-online-ordering.md`](03-storefront-online-ordering.md) §3
(branch resolution) and §5 (the checkout validation chain).

### Key technical explanation

`orders.services.resolve_nearest_branch` ranks active branches by Haversine
distance when coordinates exist on both sides, and otherwise by barangay match →
city match → previous assignment → configured default → primary key. It then
walks that ranked list and returns the **first** branch where
`fulfillable_quantities` covers every line in full. `fulfillable_quantities` is
`max(0, sellable_floor_stock − open_online_commitments)`.

`checkout` is POST-only, guarded by `cache.add()` (one atomic set-if-absent) plus
a session token rotated **before** the order is created. It locks every
active-branch `Inventory` row for the cart in `(branch_id, product_id)` order —
stable ordering is what prevents deadlock — re-resolves the branch inside the
lock, validates availability **without deducting**, prices with
`price_cart(..., discount_type=None)`, and writes the order with
`total_amount` on the INSERT.

### Possible panel questions

**"So the customer picks which branch to buy from?"**
> "No — and that's deliberate. The server picks it, from their location and from
> what's actually available. The customer sees which branch was chosen and can
> change their location, but they can't override the branch, because the branch
> has to be one that can fulfil the whole cart."

**"What if no branch has everything?"**
> "We refuse the checkout and name the products blocking it. We don't split one
> order across branches."

**"Why doesn't stock come off when the order is placed?"**
> "Because nothing has been handed over. The customer could still cancel; we
> could still reject the payment. Deducting at checkout would mean we'd have to
> reverse it in all those cases, and every reversal is a chance to get the count
> wrong. We deduct once, at the moment the goods actually leave."

**"Then how do you stop overselling in the meantime?"**
> "Availability for online shoppers already subtracts everything other open
> online orders have been promised. And at checkout we take row locks on the
> inventory rows, so two shoppers racing for the last unit are serialised — the
> second is refused inside the lock."

---

## Section 5 — The customer journey, end to end

### Presenter script

> "Let's follow one order all the way through, so you can see the whole chain.
>
> A customer registers. They give a username, email, password, and a **delivery
> address** — which is required, because that's where every delivery order ships
> to and we don't want it retyped on each order. They have to tick the terms box,
> and we record **when** they agreed and **which version** they agreed to.
>
> They browse, set a location, and the server assigns their fulfillment branch.
> They add to cart and check out. That writes the order, the line items, and the
> billing record — all at once, in one transaction. Stock hasn't moved.
>
> Now we switch to the staff side. The order appears in the Order Console
> immediately — not because anything was transferred, but because the console is
> a **view over the same table** the checkout wrote.
>
> Staff approve it. The customer gets a notification. Staff mark it Preparing.
> Another notification. On the customer's My Orders page, the order moves from
> 'In progress' to 'To receive'.
>
> A **second** staff member opens Billing, looks at the GCash reference and the
> screenshot, and verifies the payment. The customer gets a 'Payment verified'
> notification.
>
> Now — and only now — Mark Completed is live. Staff click it. Three things happen
> at once: the order becomes Completed, **the stock comes off the shelf** with a
> stock movement recording who did it and the before and after quantities, and
> the customer gets a final notification.
>
> The customer opens My Orders, sees Completed, and can open the order to see
> every stage it went through.
>
> And in Reports, that order now counts as revenue — because it's Completed.
> Pending orders don't count as revenue; they're shown separately as pipeline.
> Cancelled orders are excluded from the money entirely, but still listed in the
> table so they can be audited."

### What to show

Follow the sequence in [Section 9](#section-9--recommended-demonstration-sequence)
below. Have both windows open.

### Flowchart to display

[`11-integrated-system-data-flow.md`](11-integrated-system-data-flow.md) §2 —
the integrated happy path.

### Key technical explanation

A single online order can touch up to **13 tables**: `SalesTransaction`,
`SalesItem`, `BillingRecord`, `OrderStatusHistory`, `OrderStatusChange`,
`OrderNumberSequence`, `Inventory`, `Batch`, `SalesItemBatchConsumption`,
`StockMovement`, `Notification`, and on a refund `Refund`, `RefundItem`,
`RefundEvent`.

### Possible panel questions

**"Where exactly does the online order become a sales record?"**
> "It never 'becomes' one — it's created as one. `orders/` has no models at all;
> the checkout view writes `sales.SalesTransaction` directly."

---

## Section 6 — Accounts, roles and branch isolation

### Presenter script

> "Accounts. There are five roles: Owner, Branch Manager, Staff, Supplier and
> Customer.
>
> Customer accounts are **self-registered** — anyone can sign up. Staff accounts
> are **not**. Only the Owner can create one, and the way it works is an
> **invitation**: the Owner enters an email, a role and a branch. That creates an
> account that is **inactive** and has **no usable password**. The invited person
> gets an emailed link and picks their own username and password. They can't log
> in before that, and they can't choose their own role or branch — the Owner set
> those.
>
> Then authorization, which is **three independent layers**, and all three run on
> the server.
>
> First, **role**: is your role allowed on this page at all? Reports, for example,
> are Owner and Branch Manager only — Staff never see them, no matter what else
> their account is granted.
>
> Second, **per-account module permissions**: the Owner can grant each Manager or
> Staff account View, Add, Edit and Delete on each module individually. So two
> staff members at the same branch can have genuinely different access. Every
> write permission also requires View, so switching a module off closes its write
> URLs too.
>
> Third, **branch**: does this particular record belong to a branch you're allowed
> to touch? Owner sees every branch. Managers and Staff see only their own.
>
> On the branch question, the detail we're proudest of is this: branch filtering
> is applied **before** any URL filter. So if a Branch Manager edits the URL to
> ask for another branch's orders, what they actually get is the **intersection**
> — which is nothing. The URL filter can narrow their view but it can never widen
> it. That's why a branch parameter in the URL isn't a security hole.
>
> And it **fails closed**. If a branch gets deleted, the staff profiles pointing
> at it become null. An account in that state sees **nothing**, not everything.
> That's deliberate, and the scoping module carries an explicit warning against
> the shortcut that would have got it backwards.
>
> The one place that's worth calling out: the point of sale **refuses outright**
> for an account with no branch, and says why — 'Your account is not assigned to a
> branch, so it cannot record sales.' It doesn't silently hand them a branch
> picker.
>
> Branch and user administration are **Owner-only, full stop** — there's no module
> permission the Owner could grant to delegate them."

### What to show

1. `/manage/users/` — User Management. Open a Staff account's edit page and show
   the **permission matrix** of View/Add/Edit/Delete per module.
2. `/branches/` — branch administration. Point out it's Owner-only.
3. Sign in as a Branch Manager. Show the Order Console — **no branch switcher is
   even drawn**.
4. Hand-edit the URL to `?branch=<another branch id>` → **empty list**.
5. Try `/sales/<pk>/` for an order at another branch → *"That order belongs to
   another branch."*
6. Try `/reports/` as a Staff account → the styled **403**.

### Flowchart to display

[`00-system-overview.md`](00-system-overview.md) §4 (the three layers) and
[`09-locations-and-branch-navigation.md`](09-locations-and-branch-navigation.md)
§4 (how branch context is derived).

### Key technical explanation

`core.scoping.branch_ids_for_user` returns `None` for the Owner (meaning *do not
filter*) and a **list** for everyone else — `[]` when a Manager/Staff profile's
branch is null. `scope_queryset` applies `filter(branch__in=ids)` only when `ids
is not None`. Views then layer `?branch=` on top, which intersects.
`branch_for_user` returns a `Branch` object and must **never** be used to filter,
because its `None` is ambiguous between "Owner" and "unassigned" — its docstring
spells out the fail-open idiom it exists to prevent. `_pos_user_branch` returns a
distinct sentinel, `POS_BRANCH_UNASSIGNED`, rather than `None`, for exactly that
reason.

### Possible panel questions

**"What if a Branch Manager types another branch's ID in the URL?"**
> "They get an empty list. Branch scoping runs first and the URL filter is
> applied on top, so it's an intersection. The URL can only narrow what they're
> already allowed to see."

**"Can a customer see another customer's order?"**
> "No. The ownership filter is in the database query itself, not an `if`
> afterwards — so another customer's order ID returns a **404**, which doesn't
> even confirm the order exists."

**"Can staff create their own accounts?"**
> "No. Only the Owner can, and it's an invitation: the account is created
> inactive with no usable password, and the role and branch are set by the Owner
> before the invited person ever logs in."

---

## Section 7 — Notifications

### Presenter script

> "Notifications are one database table serving two audiences. A customer's order
> update and an Owner's low-stock alert are the same model — one row, one
> recipient. If something needs to reach five people, that's five rows, not a
> shared one, so each person reads and resolves theirs independently.
>
> We want to be precise about something: **these are in-app notifications.** They
> appear in the bell in the header. **We do not send emails or SMS for orders or
> payments.** Email is used only for account things — verifying your address,
> resetting a password, a staff invitation.
>
> Who gets what is defined in one place — an event matrix in the code — rather
> than each view deciding for itself. The rule is: the **Owner always gets every
> internal alert, for every branch.** A Branch Manager or Staff member gets it
> only if the event names a module and they actually have that module enabled.
> And some events are administration-level — a new staff account, a role change —
> and those reach the **Owner only**, not even the branch's own manager.
>
> A customer typically gets three to six notifications per order: each status
> change, the payment being verified or rejected, and the final completion.
>
> One detail we're pleased with. There's a difference between **read** and
> **resolved**. Opening a notification marks it read. Only clicking its action
> button marks it resolved. And the internal badge counts **unresolved
> high-priority items**, not unread ones — so the badge doesn't empty just because
> the Owner glanced at the list. Something that needs doing stays visible until
> it's actually done.
>
> We should also flag two things our own audit found, because we'd rather tell you
> than have you find them.
>
> First, there's a 'large order' alert meant to notify the Owner above five
> thousand pesos. **It currently never fires.** The order number used to be
> written in a second database save and the alert waits for that save; we later
> moved the number onto the insert itself to close a concurrency hole, and this
> alert's condition wasn't updated with it. It's a one-line fix.
>
> Second, the notification **preference** toggles on the customer's page — order
> updates, promotions, and so on — save correctly, but nothing reads them yet. The
> gate function exists in the code and is never called. So turning 'order updates'
> off doesn't currently stop the in-app notifications."

### What to show

1. Sign in as the customer → the **bell** in the header with its unread count.
2. Open the tray, then the full `/notifications/` page.
3. Sign in as the Owner → `/internal/notifications/` with its category tabs.
4. Point at an item needing action, click **Resolve**, and show the badge drop.

### Flowchart to display

[`10-notifications-and-customer-dashboard.md`](10-notifications-and-customer-dashboard.md)
§7 (generation and delivery) and §3 (the fan-out rule).

### Key technical explanation

`core.notifications` exposes four entry points: `notify` (internal fan-out via
`_recipients`), `notify_event` (matrix-driven, multi-audience), `notify_customer`
and `notify_supplier` (single recipient, no-op on an inactive or wrong-role
account). `_recipients` returns all active Owners, **plus** the branch's active
Manager/Staff **only when** `required_module` is passed **and**
`user_can_access_module` passes. `notify_event` de-duplicates a person appearing
in two audiences via `seen_user_ids`, honours `exclude_actor`, and the expiry
sweep passes `deduplicate=True` so it can't re-alert for the same batch.

### Possible panel questions

**"Do you email the customer when their order ships?"**
> "No — it's an in-app notification in the bell. We only send email for account
> things like verification and password resets. We'd rather say that plainly than
> claim an email feature we didn't build."

**"How do you avoid notifying the same person twice?"**
> "Two ways. `seen_user_ids` means one row per person even if they match two
> audiences. And for the completion of an online delivery order, the generic
> status notification is deliberately skipped because the delivery-specific one
> already covers it — there's a comment warning not to 'fix' that, because
> removing the guard would double-notify."

---

## Section 8 — How the system prevents invalid transactions

### Presenter script

> "We'd like to go through what the system refuses to do, because that's most of
> the engineering.
>
> **It won't complete an order whose payment isn't verified.** It locks the
> billing record, re-reads it, and refuses. So a payment rejected a second earlier
> can't slip a completion through.
>
> **It won't let the same person record and verify a payment** for a walk-in sale
> they rang up.
>
> **It won't create a duplicate order from a double-tap.** There's a one-time
> token checked with an atomic cache operation — set-only-if-absent — so of two
> requests racing, exactly one can win. And the token is rotated *before* the
> order is created, which also stops a third, slower copy.
>
> **It won't create a second billing record** — the database relationship forbids
> it.
>
> **It won't oversell.** Inventory rows are locked. If any line is short, the
> whole sale rolls back, so you never get half a sale.
>
> **It won't deduct stock twice.** A walk-in deducts at sale; an online order
> deducts at completion; the code paths are mutually exclusive on the channel.
>
> **It won't restock twice either.** If an order was already refunded, voiding it
> is refused outright — because voiding credits back the full quantity, and those
> units were already returned.
>
> **It won't accept a price from the browser.** Prices, VAT and discounts are all
> computed server-side from the database.
>
> **It won't let a customer grant themselves a discount.** Checkout passes a
> discount of 'none' explicitly, no matter what's in the request.
>
> **It won't let a delivery order get a PWD discount at all**, because nobody at
> the branch ever meets that customer to check the ID.
>
> **It won't mark a payment Paid while a PWD request is unresolved**, because the
> amount due isn't final yet.
>
> **It won't act on a stale screen.** The confirmation dialog sends back the status
> the order was in when it was drawn; if someone else moved it, the action is
> refused and you're told to re-read the order.
>
> **It won't serve payment proof images publicly.** Those screenshots carry a
> customer name, a reference number and an amount, so they're excluded from the
> public media route and only served through views that check authorization.
>
> **And it won't exit silently.** Every refusal, and every success, says what
> happened. We treat a silent redirect as a defect — because a button that
> changes nothing and says nothing looks exactly like a button that's broken."

### What to show

Pick two or three live refusals — they are more convincing than the list:

1. Try **Mark Completed** on an order whose payment is still Pending.
2. Try to **Verify** a payment you recorded yourself.
3. Try to check out with **Delivery** on an account with no saved address.
4. Add a quantity larger than available stock.

### Flowchart to display

[`11-integrated-system-data-flow.md`](11-integrated-system-data-flow.md) §8 —
the integrated exception matrix.

### Key technical explanation

Twelve distinct `select_for_update()` row locks guard the Financials path, plus
the checkout cache token and the `expected_status` echo — see
[`11-integrated-system-data-flow.md`](11-integrated-system-data-flow.md) §7 for
the table. `_OrderActionError` exists specifically so a refusal raised *inside*
an atomic block rolls the whole block back, including line items already
processed in the same loop. `checkout` locks inventory rows in a stable
`(branch_id, product_id)` order, which is what prevents deadlock between two
concurrent checkouts for overlapping carts.

### Possible panel questions

**"How do you know these guards work?"**
> "Our test suite covers them — **1,712 tests pass**: 952 across core, sales,
> orders and billing, and 760 across accounts, inventory, procurement and
> reports. And `manage.py check` reports no issues. We can run them now if
> you'd like."

**"Did you find anything wrong in your own audit?"**
> "Yes, three things, and they're written up in our findings document. A
> notification trigger that never fires, a navigation link that goes to the wrong
> page, and a preference toggle that saves but isn't read yet. None of them affect
> the correctness of a transaction — no money or stock is wrong — but they're real
> and we'd rather name them than have them found."

---

## Section 9 — Recommended demonstration sequence

Each step lists the page, the **code-verified** expected result, the diagram, and
a line you can say.

| # | Open this | Expected result (verified) | Diagram | Say this |
|---|---|---|---|---|
| 1 | `/` then `/shop/` — signed out | Only in-stock products; categories hidden below two; "N sold" on products that have sold | `03-…` §2 | *"Browsing needs no account. And notice everything here is actually buyable — we don't list what we can't sell."* |
| 2 | Press **Add to cart** as a guest | Redirect to `/login/?next=…` | `03-…` §2 | *"The cart needs an account. We keep where they were, so the button they pressed still happens after they sign in."* |
| 3 | Sign in as the demo customer | Back on the product, not a dashboard | `08-…` §4 | *"We honour the destination. An earlier version dropped shoppers on a dashboard and made them find the product again."* |
| 4 | `/shop/branch/` — **Set location** | Names the resolved branch, the distance, and which source was used | `09-…` §7 | *"This isn't a branch picker. The server picks the branch that can fulfil the whole cart."* |
| 5 | Add 2–3 items → `/shop/cart/` | Tier prices per line; branch named; free-delivery meter | `03-…` §4 | *"Every line is re-priced at the tier its quantity earns, at this branch."* |
| 6 | Check out **Pickup + GCash**, no reference | Refused, back to cart. **No order created** | `03-…` §5 | *"Without a reference we could never match the payment. So we refuse before creating anything."* |
| 7 | Add the reference + screenshot, submit | Confirmation page with `ORD-YYYY-NNN` | `03-…` §5 | *"Order, line items and billing record — all written in one transaction."* |
| 8 | **Inventory → Floor Stock**, find that product | **Quantity unchanged** | `04-…` §3 | *"This is the moment to watch. The order exists and stock has not moved — nothing has been handed over yet."* |
| 9 | Staff window → `/sales/` | The order is already there as `Pending` | `01-…` §8 | *"No import, no sync. This is a view over the same table the checkout wrote."* |
| 10 | Open it → **Approve** | `Approved`; two audit rows; customer notified | `01-…` §3 | *"Every step is locked, recorded twice, and the customer is told."* |
| 11 | → **Preparing** | `Preparing` | `01-…` §3 | *"Now it shows under 'To receive' on the customer's side."* |
| 12 | Try **Mark Completed** now | **Refused** — "can only be completed once its payment is verified as Paid" | `02-…` §4 | *"This is the rule we enforce hardest. Goods don't leave before the money is verified."* |
| 13 | `/billing/` → open the record | Reference + proof image, via the authorization-checked endpoint | `02-…` §7 | *"The reference and the screenshot are the record. There's no gateway to ask."* |
| 14 | Verify as the **same** person who recorded it | **Refused** — maker/checker | `02-…` §6 | *"One person records, a different person verifies."* |
| 15 | Verify as the **second** staff account | `Paid`; message says *"The order can now be marked Completed."* | `02-…` §6 | *"And it tells us what this unlocks."* |
| 16 | Back to the order → **Mark Completed** | `Completed`; `completed_at` set | `01-…` §3 | — |
| 17 | **Floor Stock** again | **Quantity has now dropped** | `04-…` §3 | *"There's the deduction — at completion, not at checkout. Once, never twice."* |
| 18 | **Stock movement history** for that product | A `Sale` row: actor, before, after, "Order ORD-…" | `04-…` §1 | *"Every stock change names who did it and the quantity before and after."* |
| 19 | Customer window → the **bell** | Notifications for each status change and the payment | `10-…` §7 | *"In-app, in the bell. We don't claim an email we didn't build."* |
| 20 | `/shop/orders/` + the tabs | The order under **Completed**; tabs map to backend statuses | `03-…` §7 | *"'In progress' is Pending and Approved; 'To receive' is Preparing. One table defines that mapping."* |
| 21 | Open **Track Order** | Four-step bar complete, plain-language blurb, payment pill, branch contact | `03-…` §6 | *"Same data as the staff page, worded for a customer, scoped to their own order."* |
| 22 | Try another customer's order ID | **404** | `08-…` §9 | *"Ownership is in the query, so it's a 404 — we don't even confirm the order exists."* |
| 23 | Branch Manager window → `/sales/`, then `?branch=<other>` | **Empty list** | `09-…` §4 | *"Branch scoping runs first; the URL filter intersects. It can narrow, never widen."* |
| 24 | Staff account → `/reports/` | Styled **403** | `04-…` §6 | *"Reports are Owner and Branch Manager only, blocked in three independent places."* |
| 25 | Owner → `/reports/sales/` | The order counts as revenue; pipeline shown separately; cancelled excluded from money but listed | `04-…` §6 | *"One definition of revenue, shared by the web reports and the API."* |

### Shorter version, if time is tight

Steps **1 → 7 → 8 → 9 → 12 → 15 → 16 → 17 → 20**.

That is: browse, order, **show stock unchanged**, show it in the console, show
completion refused without payment, verify, complete, **show stock dropped**,
show the customer tracking it. It demonstrates the two things that matter most —
the single shared record, and exactly-once stock at the right moment.

### Do **not** demonstrate live

| Action | Why |
|---|---|
| Verifying a payment on a **real** record | `Paid` is irreversible — no transition back |
| Cancelling a **real** order | Terminal — no outbound transition |
| `seed_demo --reset` | Destructive to local application data |
| Hard-deleting personal data | Irreversible by design |
| A bulk import or a migration rollback | Destructive |

Use pre-staged demo records for anything in this table.
