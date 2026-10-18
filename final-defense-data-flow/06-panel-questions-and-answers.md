# 06 — Panel Questions & Code-Verified Answers

Every answer below is traceable to a file named in the answer. If a question
comes up that is **not** here, say *"we'd have to check the code"* rather than
guessing — that is a better answer than a confident wrong one.

**Jump to:** [Architecture](#1-architecture--data-flow) ·
[Sales & Orders](#2-sales--orders) · [Billing](#3-billing--payments) ·
[Storefront](#4-storefront--online-ordering) ·
[Accounts & security](#5-account-security--authentication) ·
[Branch isolation](#6-branch-isolation) ·
[Location](#7-location-selection) · [Notifications](#8-notifications) ·
[Order ownership](#9-order-ownership--privacy) ·
[Inventory](#10-inventory--stock-integrity) ·
[Reports](#11-reports--data-accuracy) ·
[Concurrency](#12-concurrency--race-conditions) ·
[Limitations](#13-limitations--honest-answers) ·
[Hostile questions](#14-hard-or-hostile-questions)

---

## 1. Architecture & data flow

**Q: How does an online order get into your Sales & Orders module?**
> It doesn't get *transferred* — it's created there. The `orders` app has no
> `models.py` at all. `orders.customer_views.checkout` writes
> `sales.SalesTransaction` and `sales.SalesItem` directly. The Order Console is a
> **view over the same table**. There is no sync job, no mirror table and no
> import step, which is why they can never disagree.

**Q: What distinguishes an online order from a walk-in sale in the database?**
> `SalesTransaction.transaction_type` — `'Online'` or `'Physical'`. Secondary
> markers: an online order has `customer` set and `staff`/`recorded_by` null; a
> walk-in has `recorded_by` set and `customer` null. And the two have different
> stock timing, which is driven off `transaction_type`.

**Q: Why is there also a REST API if the UI is server-rendered?**
> It's a same-origin DRF API retained under `/api/`. For these flows it
> **mirrors** the template views and re-applies the same rules — the same
> `core.permissions`, `core.scoping`, `can_transition_to`,
> `is_self_confirmation`, `missing_payment_evidence`. Those live in shared
> modules precisely so the two surfaces can't drift. The API views also carry
> `HasModuleAccess`, which re-applies the per-account module grants to API routes
> using the resolved URL name and HTTP method.

**Q: Walk me through every table one order touches.**
> Up to thirteen. At creation: `SalesTransaction`, `SalesItem`, `BillingRecord`,
> `OrderStatusHistory`, and `OrderNumberSequence`. Per status change:
> `OrderStatusHistory`, `OrderStatusChange`, `Notification`. At completion:
> `Inventory`, `Batch`, `SalesItemBatchConsumption`, `StockMovement`. On a refund:
> `Refund`, `RefundItem`, `RefundEvent`.

**Q: Is any of this asynchronous? Do you use a task queue?**
> No. There is no Celery, RQ, worker or websocket in `requirements.txt`. Every
> notification, stock movement and status change happens synchronously in the
> request that caused it. The only scheduled work is four Django management
> commands driven by cron. The "live" search and notification tray are plain XHR
> to the same Django views.

---

## 2. Sales & Orders

**Q: What are the order statuses and what controls the transitions?**
> `Pending`, `Approved`, `Preparing`, `Completed`, `Cancelled`. A dict called
> `_FLOW` in `sales/views.py` declares the legal moves, and
> `transaction_set_status` re-checks it **inside** a row lock. It is the only
> writer of `order_status` anywhere in the system.

**Q: Can an order be marked Completed before it's paid?**
> No. `transaction_set_status` takes a `select_for_update()` lock on the billing
> record, re-reads it, and raises if `status != 'Paid'`. Because it's locked and
> re-read, a payment rejected in the gap can't slip a completion through. The one
> apparent exception is a **cash walk-in**, which is created `Completed` *and*
> `Paid` in the same atomic block — the money crossed the counter, so both facts
> are true at once.

**Q: Does marking a payment Paid automatically complete the order?**
> No, and that's deliberate. Verification **unlocks** completion; a human still
> presses the button. The dependency is enforced one way only.

**Q: Who can cancel an order?**
> Owner, Branch Manager and Staff for an unfulfilled order, with a **required**
> reason. **Voiding a completed sale** is Owner and Branch Manager only, because
> real stock and possibly real money have already moved. And a customer may
> cancel **their own** order while it's `Pending` or `Approved`.

**Q: The customer cancel button posts to a staff endpoint. Isn't that a hole?**
> It's deliberate, and it's narrow. `transaction_set_status` has an
> `is_self_cancel` carve-out requiring **four** conditions simultaneously: the
> role is `CUSTOMER`, `txn.customer_id == request.user.id`, the target is
> `Cancelled`, and the current status is `Pending` or `Approved`. If any one
> fails, the normal role and branch checks run and refuse. The reason we reuse
> that endpoint rather than writing a second cancel is that the history rows,
> the billing update and the notifications all live there — a parallel
> implementation would drift.

**Q: Why can't a customer cancel once it's Preparing?**
> Because whether it can still be stopped depends on state the customer isn't
> shown — how far staff have got with packing. So past `Approved` it becomes a
> staff decision, and the page gives them the branch's contact details instead.

**Q: Where do your totals come from? Could I tamper with the price?**
> No. Prices are never read from the form. Both creation paths call
> `Product.price_at(quantity)` server-side to resolve the bulk tier, then hand the
> lines to `sales.pricing.price_cart`, which owns all the VAT and discount
> arithmetic. The same module prices the POS preview, the written sale, the
> receipt and the online cart — so those four can't disagree.

**Q: How do you handle VAT?**
> Shelf prices are VAT-**inclusive**, which is how they're entered and displayed.
> So VAT is **extracted** for reporting, never added on top. A ₱112 item is
> charged ₱112 and `vat_amount` records ₱12.

**Q: And the PWD/Senior discount?**
> Under RA 10754 and RA 9994 the sale becomes VAT-exempt and the 20% comes off the
> VAT-**exclusive** amount. So: strip VAT first, then discount. ₱112 → ₱100 → ₱80.
> Taking 20% off ₱112 directly would charge ₱89.60, which over-charges by ₱9.60 —
> that worked example is in the `sales/pricing.py` docstring.

**Q: Can a customer claim the PWD discount themselves?**
> Not any more, and that was a real defect we fixed. Checkout used to read
> `discount_type` from the POST and apply the full 20%, guarded only by the ID
> fields being non-empty strings — so `'MADE-UP-123'` bought a permanent
> discount with nobody ever seeing a card. Now **every order checkout writes is
> at full price**, and `discount_type=None` is passed explicitly. What a customer
> *can* do, for a pickup order, is **request** it: that records what they typed,
> changes no money, and staff decide when they see the ID.

**Q: What stops staff just forgetting to check the ID?**
> Billing refuses. While `pwd_request_status == 'Pending'`,
> `BillingRecord.pwd_review_block_message()` blocks the record from being marked
> `Paid` — and an order can't be `Completed` unless it's `Paid`. So "check the ID
> before settling the bill" is enforced by the payment gate, not by memory.

**Q: Why can't a delivery order get the discount at all?**
> Because nobody at the branch ever meets that customer. The discount is
> conditioned on presenting a card, and there's no point in the delivery workflow
> where anyone sees one. `can_apply_pwd_senior_discount()` returns `False` for
> `Online` + `Delivery`, and the view enforces it.

**Q: Can line items be edited after a sale?**
> No — by design. There's no view that edits an existing `SalesItem`. Correcting
> a mistake means cancelling the order, with a reason, and ringing up a new one.
> The new order's `replaces` field links back to the cancelled one, so the
> correction is visibly connected rather than looking like an unrelated sale.

**Q: Explain your refund workflow.**
> Within seven days of completion (`SalesTransaction.REFUND_WINDOW`), on a
> `Completed` order whose payment is `Paid`. Staff pick lines, quantities, a
> condition and a reason code. That writes a `Refund` at `Pending` with
> `RefundItem` rows and a `RefundEvent`. Owner and Branch Manager decide — and
> their *own* request self-approves. On approval: `Sellable` items are restocked
> with a `Customer Return` stock movement, `Damaged` items are not;
> `refunded_amount` goes up; and the billing record moves to `Refunded` or
> `Partially Refunded` depending on whether anything is left.

**Q: Does a refund reduce the order total?**
> No, and that's intentional. `total_amount` is the frozen figure the customer was
> charged and the receipt prints. What the order is worth *now* is the
> `net_amount` property — `total_amount` minus `refunded_amount`. And
> `order_status` stays `Completed`, which is why every revenue figure in Reports
> subtracts refunds separately.

**Q: Can you void an order you've already refunded?**
> No — it's refused outright. `_reverse_stock` credits back the *full* quantity
> sold, so voiding after a sellable refund would restock those units twice. And
> the money side would have nothing coherent to reverse to. The message says
> "refund the remaining items instead."

---

## 3. Billing & payments

**Q: When is a billing record created?**
> Automatically, by a `post_save` signal on `SalesTransaction`, inside the same
> atomic block as the order. There is no manual step to forget. And it's created
> at the correct `amount_due`, because both creation paths compute the total
> *before* the INSERT and pass it in.

**Q: Could one order end up with two billing records?**
> No. `BillingRecord.transaction` is a `OneToOneField`, so there's a unique
> constraint in the database. It isn't a check we perform — it's impossible.

**Q: What payment methods do you support?**
> Cash, GCash, Card, Maya, QR Ph, Charge Sale, Bank Transfer, Cash on Delivery
> and Pay at Store. Which are **offered** depends on the channel:
> `payment_methods_for_channel` gives a walk-in Cash/Card/GCash/Bank Transfer, and
> an online order COD/Pay at Store/GCash/Bank Transfer. The POST is re-validated
> against the same set, so a tampered form can't get past the dropdown.

**Q: Why the channel split?**
> Because the form used to offer all six to both, which let a walk-in sale be
> "settled" as Cash on Delivery and an online order as a counter cash swipe —
> neither of which the counter can act on, and both of which broke the Billing
> report's payment breakdown.

**Q: Explain GCash verification step by step.**
> At checkout, the reference number and a screenshot are **required before the
> order is created at all** — validated by `clean_reference_number`,
> `missing_payment_evidence` and `_validate_proof_of_payment`. So there's no such
> thing as an unverifiable GCash order in our system. Later, staff open the
> record, view the proof through an authorization-checked endpoint, compare the
> reference against the real GCash account, and either mark it `Paid` or reject it
> with a written reason. The customer is notified either way.

**Q: How do you know the screenshot is really an image?**
> `_validate_proof_of_payment` opens it with Pillow and calls `img.verify()`, then
> checks the format is JPEG, PNG or WEBP and the size is under the configured cap.
> That matters because assigning an uploaded file straight onto a model
> `ImageField` skips Django's form-layer image check entirely — a renamed `.html`
> would otherwise save and serve with its own content type.

**Q: Who can see the payment proof images?**
> Only through authorization-checked views. `payment_proofs/` is **excluded** from
> the public `/media/` route with a regex, because `django.views.static.serve` does
> no authentication and those screenshots carry a customer name, a reference number
> and an amount. Product images and avatars stay public.

**Q: Can the person who recorded a payment also verify it?**
> Not for a walk-in they rang up. `is_self_confirmation(user)` returns
> `user.role == STAFF and self.transaction.staff_id == user.id`. Owner and Branch
> Manager fail the role test, so they're exempt by design. And an online order has
> no staff member on it, so this never blocks online payments.

**Q: Can a payment be un-verified?**
> No. `ALLOWED_STATUS_TRANSITIONS` has no edge out of `Paid` back to `Pending` or
> `Rejected`. The only ways out are a **void** (which only the order's own
> cancellation can trigger) or a **refund** — each with its own permissions and
> audit trail. A **rejection** *is* recoverable: it can be re-opened to `Pending`,
> which clears `confirmed_by`, `date_confirmed` and `reject_reason`.

**Q: Why two separate sets of allowed statuses?**
> `ALLOWED_STATUS_TRANSITIONS` is what's structurally legal.
> `HUMAN_CONFIRMABLE_STATUSES` — just `Pending`, `Paid`, `Rejected` — is what a
> person may pick directly. It exists because the API serializer was once a bare
> `ChoiceField` over all six values, so any Billing-access API caller could PATCH a
> `Pending` record straight to `Cancelled`, or a `Paid` one to `Refunded`,
> bypassing the stock reversal, the refund event trail and the Owner-only
> approval. Narrowing the human-pickable set closed that.

**Q: What's the difference between Rejected and Cancelled on a billing record?**
> `Rejected` means a submitted payment was specifically turned down — it needs a
> reason and can be re-opened. `Cancelled` means the **order behind it** fell
> through. They're tracked separately, and a void even gets its own audit pair,
> `voided_by` and `date_voided`, so the record never reads "confirmed by [the
> voider] on [the original date]" — a pairing that never happened and which used
> to erase the real confirmer.

**Q: Can a customer see the Billing module?**
> No. `billing_list`, `billing_detail`, `billing_create` and `billing_proof` are
> all `role_required(OWNER, BRANCH_MANAGER, STAFF)`. It used to allow customers,
> which dropped a shopper into the back-office shell with a live "Mark as Paid /
> Reject" card — the POST was already refused, but the controls were drawn and
> clickable, and a refusal should never be the only guard. A customer sees the
> same information, scoped to their own order, on Track Order.

---

## 4. Storefront & online ordering

**Q: Can someone browse without an account?**
> Yes — catalog, product pages, search, categories, and setting a location. An
> account is required at **Add to cart**.

**Q: What happens if a staff account tries to use the shop cart?**
> They're told why and left on the catalog: *"The shop cart is for customer
> accounts. Staff sales go through the POS instead."* We don't send them to login,
> because `login_view` honours `?next=` for an already-authenticated user — so
> that was an **infinite redirect**. From the shopper's side, Add to cart simply
> did nothing, and the browser gave up with `ERR_TOO_MANY_REDIRECTS`.

**Q: Where is the cart stored?**
> In the Django session — `request.session['cart']` as `{product_id: quantity}`.
> There is no `Cart` model and nothing is written to the database until checkout.

**Q: Does the cart show the price the customer will actually pay?**
> Yes. `_cart_items` re-prices every line through `Product.price_at(quantity)` —
> the same tier lookup checkout uses — so the "each" figure is the tier price, not
> the retail price. What the cart deliberately does **not** preview is the
> PWD/Senior discount, because that's granted by staff against a sighted ID after
> the order exists. Showing it would promise a price the order isn't entitled to.

**Q: What if an item becomes unavailable while it's in a cart?**
> `_cart_items` pops it out of the session and the customer is told by name. It's
> done in that one shared helper on purpose: previously the line only vanished
> from the *display* while staying in the session, so the total shrank with no
> explanation, the header badge kept the old count, and **checkout met the line
> for the first time and rejected a product the shopper could no longer see to
> remove.**

**Q: How do you stop a double-click creating two orders?**
> A one-time token, checked with `cache.add()` — one atomic set-if-absent
> operation, so of two requests racing on the same token exactly one can win,
> however close together they arrive. A session read/write alone couldn't promise
> that. There's also a session token checking the request belongs to the cart page
> currently in the session, and the token is rotated **before** the order is
> created, which stops a third, slower copy. If the cache is down it fails
> **open** deliberately — the real anti-oversell guard is the row lock, and a rare
> duplicate during an outage is better than nobody being able to check out.

**Q: Where does the delivery address come from?**
> The account's **default `Address` row**. There's no address input on the
> checkout form at all. One authoritative, structured, saved record beats a
> free-text box retyped — and mistyped — on every order. If Delivery is chosen and
> there's no saved address, we **refuse** and say where to add one.

**Q: Is the address copied or linked?**
> Copied — `delivery_address` is a `TextField` snapshot taken at checkout, not a
> foreign key. That's deliberate: editing your saved address later must **not**
> rewrite where past orders were actually sent.

**Q: Why is Pay at Store blocked for delivery?**
> Because Pay at Store means settling in person at the counter, and a delivery
> order never puts anyone there. The cart flags it client-side and the server
> enforces the same rule, for a tampered form or a client with JavaScript off.

**Q: What if the fulfillment type is missing from the POST?**
> We **refuse** — we don't default. There's no safe guess: picking one would
> either promise a delivery nobody asked for, or strand a customer waiting at
> home for an order marked pickup. Payment method *does* fall back to a default,
> because every method on that form is recorded-only and settled in person either
> way, so the default can't mislead anyone.

**Q: What are the My Orders tabs mapped to?**
> One dict, `tab_statuses`: "In progress" is `Pending` + `Approved`, "To receive"
> is `Preparing`, then `Completed` and `Cancelled` each have their own. "All" has
> no filter. `Preparing` used to sit in "In progress" too, which made the two
> overlap. The split now means "In progress" is what the **store** is working
> through and "To receive" is what the **customer** is waiting on.

---

## 5. Account security & authentication

**Q: How are customer accounts created?**
> Self-registration at `/register/`. We validate the username length and
> character set, the email format, the password against Django's validators,
> uniqueness of both username and email, a Philippine mobile format if one is
> given, and we require a delivery address. The terms box must be exactly `'1'` —
> not merely present — because the HTML `required` attribute is only a browser
> nicety and a hand-built POST skips it. We record `terms_accepted_at` and
> `terms_version`, so a consent can always be matched to the wording agreed to.

**Q: How are staff accounts created? Can staff self-register?**
> No. Only the Owner, and it's an **invitation**. The Owner enters an email, a
> role and a branch. That creates an account with a placeholder username, an
> **unusable password**, and `is_active=False`. The invited person gets an emailed
> single-use token and picks their own username and password, which activates the
> account. They can't log in before that, and they can't choose their own role or
> branch.

**Q: How do you protect against brute-force login?**
> `is_login_locked_out(username)` is checked **before** `authenticate` is even
> called; failures increment a counter; success clears it. Registration has its own
> IP-keyed rate limit, checked before any of the per-field work runs, because
> otherwise a scripted flood of signups costs an attacker nothing.

**Q: Do you have two-factor authentication?**
> Optional, per account. It's an emailed six-digit code. The important detail:
> `login()` does **not** happen until the code is verified — only a session
> pointer is set, so the account isn't actually signed in. There's a separate
> lockout for wrong codes. And if the backend holding the code is unreachable, we
> say so and leave the password unconsumed rather than sending someone to a code
> screen they can never pass.

**Q: How do you handle sessions?**
> We keep an `accounts.UserSession` row per login, because Django's own session
> store has no user→sessions index — it can't be queried without scanning the
> whole table. That's what makes Settings → Sessions listable and individually
> revocable. Revoking deletes both the real `django_session` row and our tracking
> row, so the device is actually logged out on its next request.

**Q: What stops someone pressing Back after logout and seeing a protected page?**
> `NoCacheForAuthenticatedMiddleware` sets `Cache-Control: no-store, no-cache,
> must-revalidate, private` on every authenticated response. Without it the
> browser can redisplay a cached copy from bfcache or disk — no request reaches
> Django, so `login_required` never gets a chance to reject it.

**Q: Is the `?next=` parameter safe?**
> Yes, it's validated. `next` arrives from a query string on the way in and a
> hidden field on the way out, either of which an attacker can set, and
> `redirect()` would happily send the browser to another origin. We check it with
> `url_has_allowed_host_and_scheme` against the request's own host. There are
> three sibling helpers in the storefront for the same reason, including one that
> validates the `Referer` header.

**Q: Can an account delete its own data?**
> A customer can **deactivate** — that sets `is_active=False` and is fully
> reversible. Erasing personal data is a separate, **Owner-only** action that
> blanks the identity fields but **never deletes the row** and never clears a
> foreign key, so sales, stock movements and billing confirmations all keep
> working. It records `deleted_at` and `deleted_by` — that pair is the
> accountability record.

**Q: Why not just delete the user row?**
> Because every table referencing them by id would break or lose history.
> Blanking the identity fields achieves the privacy outcome while keeping the
> financial record intact and auditable.

---

## 6. Branch isolation

**Q: How do you stop one branch seeing another's data?**
> Three layers, all server-side. Role, then per-account module permissions, then
> **branch scoping** via `core.scoping`. For lists it's `scope_queryset`, for a
> single record it's `user_can_access_branch`.

**Q: The branch is in the URL. Can't I just change it?**
> You can change it, and it won't help you. `scope_queryset` is applied **first**
> and the URL filter is applied **on top**, so you get the *intersection*. A
> Branch Manager at branch 1 asking for `?branch=2` gets
> `filter(branch__in=[1]).filter(branch_id=2)` — zero rows. **The URL filter can
> narrow a view but it can never widen it.** That's why the parameter being
> user-controllable is safe.

**Q: What if a manager guesses an order ID at another branch?**
> `transaction_detail` calls `user_can_access_branch(request.user, txn.branch_id)`
> and refuses with "That order belongs to another branch." On the API side, billing
> answers **404** rather than 403 — deliberately, so it doesn't confirm to an
> out-of-scope caller that the record exists.

**Q: Where is the "active branch" stored for a staff member?**
> Nowhere. There's no session key and no current-branch field. It's **re-derived
> from the database on every request** from the account's role profile —
> `StaffProfile.branch` or `BranchManagerProfile.branch`. The Owner is unrestricted.

**Q: What happens if a branch is deleted while staff are assigned to it?**
> Every relation is `SET_NULL`, so sales history survives and the staff profile's
> branch becomes null. `branch_ids_for_user` then returns an **empty list**, not
> `None` — so that account sees **nothing**. It fails **closed**. The scoping
> module carries an explicit warning against the `if user_branch:` shortcut, which
> would hand such an account every branch's data.

**Q: What does the POS do for an unassigned account?**
> It refuses outright, on both GET and POST, and says why. `_pos_user_branch`
> returns a distinct sentinel, `POS_BRANCH_UNASSIGNED` — deliberately **not**
> `None`, because `None` means "the Owner, unrestricted" and collapsing them
> handed an unassigned staff account the Owner's every-branch selector *and*
> skipped the posted-branch access check.

**Q: Can a Branch Manager manage branches or users?**
> No. Both are **Owner-only, full stop**, and neither is in the module permission
> registry — so there is no grant the Owner could give to delegate them.

**Q: Can a Staff account see Reports?**
> No, and it's blocked in three independent places: the
> `@management_report_required` decorator, `user_can_perform_module_action` (which
> caps the reports module to Owner/Manager regardless of any grant), and
> `ModulePermissionMiddleware` — which raises a real **403** rather than a flash
> redirect, because a redirect would imply the Owner could unlock it.

---

## 7. Location selection

**Q: Does the customer choose which branch to buy from?**
> No. The customer sets a **location**; the **server** picks the branch. And a
> posted `branch_id` is explicitly ignored — the view's docstring says so.

**Q: Then what does "Set location" actually do?**
> It supplies a geographic hint. Priority: browser geolocation if shared, else the
> default saved address, else a configured store fallback. `resolve_nearest_branch`
> ranks active branches by straight-line distance where coordinates exist on both
> sides, and otherwise by exact barangay match, then same city, then the previous
> assignment, then the configured default, then primary key.

**Q: How is the branch actually chosen from that ranking?**
> It walks the ranked list and returns the **first** branch that can fulfil
> **every** line in the cart, in full. If none can, it returns no branch at all.
> We never split one order across branches, and we say so, naming the products
> that are blocking it.

**Q: Is that availability just the stock on the shelf?**
> No — `fulfillable_quantities` is sellable floor stock **minus** everything other
> open online orders have already been promised. That subtraction matters because
> an online order doesn't deduct stock until it completes; without it, ten
> shoppers could all be promised the same last unit.

**Q: Is the storefront location a branch, a delivery address, or a preference?**
> All three feed it, but it is none of them. The **input** is a location hint; the
> **output** is a fulfillment branch assignment. It is **not** the delivery
> address — that's read separately at checkout from the account's default
> `Address`, and only for Delivery. And it isn't a stored preference either: it's
> recomputed on essentially every cart operation, with the previous assignment
> used only as a **tie-breaker**.

**Q: Do you store customers' GPS coordinates?**
> No. Browser coordinates live in the **session** for 30 minutes and then expire;
> they're also cleared on successful checkout. What we persist to the session is
> the resolved branch and a distance rounded to one decimal place for display.
> `remember_assignment`'s docstring is explicit: *"Persist no customer
> coordinates — only branch and rounded display data."*

**Q: What if the resolved branch changes while someone is checking out?**
> We refuse and send them back to review the cart. It's checked **twice** — once
> before the lock and once inside it — and the second check rolls the transaction
> back with `set_rollback(True)`.

**Q: What if the assigned branch is deactivated?**
> Only `is_active=True` branches are ever candidates, and the cart page and nav
> both re-filter on `is_active`. So a stale `session['branch_id']` resolves to
> nothing and the next resolution reassigns.

---

## 8. Notifications

**Q: How are notifications generated?**
> Synchronously, from views, signals and service functions, through
> `core.notifications`. Four entry points: `notify` for internal fan-out,
> `notify_event` for matrix-driven multi-audience events, and `notify_customer` /
> `notify_supplier` for a single recipient.

**Q: Do you send emails or SMS?**
> **Not for orders or payments.** Those are in-app notification rows read in the
> bell. Email is used only for account lifecycle: verification, email change,
> password change and reset, account deactivation, and staff invitations. We'd
> rather say that plainly than claim a feature we didn't build.

**Q: Who gets an internal alert?**
> All active **Owners**, always, for every branch. Plus the branch's active
> Manager and Staff — but **only** when the event names a module and that account
> actually has it enabled. Administration-level events (a new staff account, a
> role change) name no module and so reach the **Owner only**, not even the
> branch's own manager.

**Q: Where is that policy defined?**
> In `EVENT_MATRIX` in `core/notifications.py` — a declarative table, one rule per
> event, keeping the *when* and *who* as prose alongside the routing data, so it's
> reviewable without reconstructing it from a dozen views.

**Q: How do you avoid duplicate notifications?**
> Three ways. `seen_user_ids` gives one row per person even if they match two
> audiences. `exclude_actor` drops whoever caused the event. And the expiry sweep
> passes `deduplicate=True` so it can't re-alert for the same batch. There's also
> one hand-written guard: for the completion of an online **delivery** order, the
> generic status notification is skipped because the delivery-specific one already
> covers that event — with a comment warning not to "fix" it, since removing the
> guard would double-notify.

**Q: What's the difference between read and resolved?**
> Opening a notification marks it **read**. Only clicking its action button marks
> it **resolved**. The internal badge counts **unresolved high and critical**
> items, not raw unread — so the badge doesn't empty just because the Owner
> glanced at the list, and something that needs doing stays visible until it's
> done.

**Q: What if the customer's account was deleted?**
> `notify_customer` returns `0` and raises nothing. `SalesTransaction.customer` is
> nullable, so an order can outlive its customer and a notification attempt must
> not crash the transaction it's part of. Same for `notify_supplier`.

**Q: Did you find any problems with notifications?**
> Two, and they're in our findings document. The **large-order alert never
> fires** — it waits for a second database save that no longer happens, because we
> moved the order number onto the insert to close a concurrency hole and didn't
> update the condition. And the customer's **notification preference toggles save
> but nothing reads them** — the gate function exists and is called from nowhere.
> Neither affects a transaction, but both are real.

---

## 9. Order ownership & privacy

**Q: Can a customer see another customer's order?**
> No. All three customer order views put the ownership clause **in the query**:
> `get_object_or_404(..., id=transaction_id, customer=request.user)`. So another
> customer's ID returns a **404**, not a 403 — it doesn't even confirm the order
> exists. We use that same convention on the billing API.

**Q: Why 404 and not 403?**
> A 403 confirms the record exists, which is itself information. A 404 doesn't.

**Q: What can a customer see about their own order?**
> The order number, date, branch, items, total, order status with
> plain-language wording, the payment method and payment status, and the branch's
> contact details. They can also fetch their own billing record through
> `/api/billing/`, which scopes customers to `transaction__customer=self`.

**Q: What can't they see?**
> The staff Order Console, the Billing module, other branches' stock, internal
> cost prices, supplier data, purchase orders, staff names beyond the branch
> contact, internal notifications, or Reports.

**Q: Is the order history complete?**
> Yes, and it's recorded twice over. `OrderStatusHistory` carries the full trail
> including creation; `OrderStatusChange` is written alongside it and holds the
> optional note for a non-cancellation step. `_status_timeline` merges them and
> preserves any orphaned row from an order predating the history table. Every
> row names the actor.

---

## 10. Inventory & stock integrity

**Q: When exactly does stock leave the shelf?**
> A **walk-in** sale deducts at the moment of sale, inside the same transaction
> that creates it — the customer is carrying the goods out. An **online** order
> deducts only when staff mark it **Completed** — nothing has been handed over
> before that.

**Q: How do you guarantee stock isn't deducted twice?**
> `_deduct_stock` is called from exactly two places, and they're mutually
> exclusive on `transaction_type`: `transaction_add` handles `Physical`, and
> `transaction_set_status` has an explicit `ONLINE` guard. A `Physical` order
> reaching `Completed` through the status view hits that guard and does nothing.

**Q: What about restocking twice on a cancellation?**
> `_reverse_stock` fires either when the order wasn't completed **and** is
> `Physical`, or when it **was** completed. Those two conditions can't both be
> true, so it can't run twice in one call. And a void is refused outright if the
> order already has approved refunds, because those units came back already.

**Q: Is there an audit trail for stock?**
> Every single change writes a `StockMovement` row with the movement type, the
> quantity change, the quantity **before** and **after**, a reference to the
> causing record, the batch where applicable, a note, and who did it. It's the
> fastest-growing table in the schema, which is why it carries its own index.

**Q: How do you handle expiry?**
> Batches. `Inventory.objects.sellable()` annotates `available_qty` — the portion
> of floor stock whose expiry hasn't arrived, summed across batches. It's
> **batch-aware, not a whole-row exclusion**: a product with one expired lot and
> one fresh lot still shows the fresh quantity as buyable rather than disappearing
> entirely. Every customer-facing and point-of-sale path reads `available_qty`,
> never raw `quantity_on_hand`.

**Q: Why is that distinction important?**
> Because `quantity_on_hand` can briefly include expired stock until the daily
> sweep writes it off. Reading it in the POS let the cart accept a quantity the
> server would then reject with "Not enough stock."

**Q: What order are batches consumed in?**
> First-expiry-first-out — `Batch.consume_fefo` drains the nearest-expiry floor
> lots. And `_deduct_stock` records exactly which batches each line drew from, and
> how much, on `SalesItemBatchConsumption`. That's what lets a void credit back
> the **same** lots rather than only reversing the aggregate and leaving the batch
> ledger short.

**Q: What if a product was deleted after the sale?**
> `SalesItem.product` is `SET_NULL` precisely so sales history survives. On a
> reversal, that line is **skipped** — there's no inventory row to credit — and
> the person voiding the order is **warned** which lines couldn't be restored,
> with the unit count. It isn't silently short-restocked.

**Q: How does stock get in?**
> Receiving a delivery credits **`WarehouseStock`** and creates a warehouse
> `Batch` with the expiry captured at that moment. Goods aren't sellable yet — a
> `StockTransfer` has to move them to the floor, and that release writes two stock
> movements, one per side, and creates **one floor batch per source lot consumed**,
> so a restock with a different expiry can't merge into whatever the floor already
> had.

---

## 11. Reports & data accuracy

**Q: How do you make sure Reports reflect real transactions?**
> One shared definition of revenue, in `reports/services.py`, imported by **both**
> the web views and the API. The views module used to carry its own copy — that's
> exactly how the two drifted, and moving it out is the fix.

**Q: What counts as revenue?**
> Only orders whose `order_status` is `Completed`. A `Cancelled` order is a voided
> sale and a `Pending`/`Approved`/`Preparing` order hasn't moved stock or been
> paid for. Neither belongs in Total Sales, the daily trend, the branch donut, or
> the average transaction value.

**Q: Are cancelled orders hidden?**
> No — they're excluded from every *money* figure but still listed in the
> transactions table so they can be seen and audited. That split fixed a real
> inconsistency: a voided ₱300 sale showed as ₱300 with one order and a ₱300
> average on the Sales tab, while the Billing tab correctly showed ₱0 on the same
> data.

**Q: How are refunds handled in revenue?**
> Subtracted separately, because **a refund never changes `order_status`** — a
> refunded order is still `Completed`. Without that subtraction, a fully refunded
> order would keep counting its original amount as revenue forever. And only
> **approved** refunds count; pending and rejected requests moved no money.

**Q: Does the average order value include refunds?**
> No, deliberately — it stays on the gross figure, because it describes the size
> of a typical completed order, not a figure blended with refunds that don't line
> up one-to-one with that window's own orders.

**Q: Does the CSV export match what's on screen?**
> Yes — it inherits the same branch and date filters as the page, so a download
> reflects the rows displayed rather than the branch's whole history. There's also
> a `_csv_safe` guard against formula injection in spreadsheet software.

**Q: Any timezone issues with the date ranges?**
> We hit one and fixed it. `date_recorded` and `transaction_date` are
> `DateTimeField`s, and using `__date__gte` goes through `CONVERT_TZ` on MySQL and
> **silently matched zero rows** — which once made both date inputs return an empty
> report for every range. We use `local_range_bounds` instead, which turns local
> calendar days into aware half-open bounds inclusive of the whole end day.

---

## 12. Concurrency & race conditions

**Q: Two cashiers sell the last unit simultaneously. What happens?**
> Each `_deduct_stock` takes a `SELECT FOR UPDATE` lock on that inventory row. The
> second waits, then reads the real remaining quantity, finds it short, and raises
> `_OrderActionError` — which rolls back its **entire** sale. Exactly one sale
> succeeds.

**Q: Two customers check out for the last unit?**
> Same answer, one layer earlier. `checkout` locks every relevant inventory row
> **before** resolving the branch, in a stable `(branch_id, product_id)` order.
> The second is refused inside the lock with the exact remaining count.

**Q: Why does the lock order matter?**
> Deadlock. Two concurrent checkouts with overlapping carts could each hold a row
> the other needs. Locking in a deterministic order makes them queue instead.

**Q: Two staff click Approve at the same time?**
> `transaction_set_status` re-fetches the order with `select_for_update()` before
> any check, so the second can't pass the transition test against the same stale,
> pre-lock read.

**Q: Someone leaves an order page open, a colleague advances it, then they click.**
> The confirmation dialog posts back the status the order was in when it was
> drawn. If it no longer matches, the action is refused with *"it changed while you
> were reviewing it — check the order and try again."*

**Q: Two staff verify the same payment at once?**
> `billing_confirm` locks the billing row, so the second re-reads the now-`Paid`
> status, fails `can_transition_to`, and is refused.

**Q: A payment is confirmed in the same instant the order is cancelled?**
> Both paths lock the **same** billing row. That's what serialises them. Without
> it, a payment confirmed in the gap was silently overwritten back to `Cancelled`
> — or the reverse — and the order and its bill could disagree about whether there
> was still money to collect.

**Q: Two concurrent orders getting the same order number?**
> `OrderNumberSequence` is a per-year counter read under `select_for_update()` in
> the same transaction as the insert, and the number is written **on** the insert.
> So no row ever sits under the unique index with an empty order number for two
> concurrent creates to collide on.

**Q: Two staff apply a discount to the same order?**
> `_lock_for_discount` locks both the order and its billing row and re-checks
> "no discount yet" against the **locked** rows, so they can't both pass and
> compound the reduction.

---

## 13. Limitations — honest answers

**Q: What are the main limitations of your system?**
> Four, and we'd list them in this order. **One: no payment gateway** — payments
> are recorded and verified, not processed. That's a documented scope limitation
> and the entire Billing module is built around it honestly. **Two: notifications
> are in-app only** — no email or SMS for orders. **Three: online stock is a soft
> reservation**, not a hard hold; availability subtracts open commitments on read,
> and the real guarantee is the row lock at checkout. **Four: Filipino support is
> partial** — it's a hand-rolled dictionary of around 130 strings with an English
> fallback, not full Django internationalisation.

**Q: Is there a delivery fee?**
> No. There's a ₱500 "free delivery" progress meter on the cart, but it is
> **display only** — there's no delivery-fee field on any order model. We should
> be clear about that rather than imply a fee is charged or waived.

**Q: Do you manage courier assignment or delivery routing?**
> No. The system records that an order is for delivery and the address it goes to.
> Dispatch is outside its scope.

**Q: Can two shoppers be shown the same last unit?**
> In principle, yes, between two reads — the reservation is computed at read time
> and nothing writes a hold row. What can't happen is both of them **getting** it:
> the row lock at checkout serialises them and the second is refused. So the worst
> case is a refusal at Place Order, not an oversell.

**Q: Can an online order fail after you've accepted it?**
> Yes, and we should be straight about it. Walk-in sales deduct immediately and
> aren't constrained by online commitments, so a busy counter can drain the floor
> below what an open online order needs. `Mark Completed` then fails with the exact
> shortfall and the whole thing rolls back — safe, but it leaves the order needing
> a manual decision: transfer stock in, or cancel. The Order Console flags
> over-committed orders, but that's **reporting only** — nothing holds stock.

**Q: What happens to expired stock that hasn't been swept yet?**
> `quantity_on_hand` still includes it, but **nothing can sell it** —
> `available_qty` nets out anything past its expiry, and every sale path reads
> that. So the worst case is an internal screen showing a lot that's due, for the
> short interval before the daily sweep runs.

---

## 14. Hard or hostile questions

**Q: This looks like a lot of defensive code. Did you actually hit these problems, or are you guessing?**
> We hit them. Most of the guards in this system have a specific incident behind
> them, and the code comments name it. The infinite redirect loop on Add to cart
> was reproducible in three lines with the test client. Customers could see a live
> "Mark as Paid" button on a billing record. Checkout accepted a typed PWD ID and
> applied 20%, so `'MADE-UP-123'` bought a permanent discount. Payment proofs were
> served off the public media route with no authentication. An over-long GCash
> reference reached a `VARCHAR(100)` column and 500'd mid-checkout. We can show
> you the comment for any of those.

**Q: Your tests all pass, so how did you find bugs?**
> By reading the code against what it claims to do, which is how we found all
> three. A green suite tells you the behaviour you **wrote tests for** still works.
> The large-order notification has **no test at all** — we found it by noticing
> that its docstring described a two-save creation pattern that the model no
> longer uses. The About link we found by actually resolving the URL names through
> `django.urls.resolve`. And the preference toggles we found by searching for call
> sites of the gate function and getting zero hits.

**Q: Isn't the whole thing over-engineered for a variety store?**
> The places we spent effort are the places where being wrong costs money: stock
> counts, payment status and branch isolation. Those have row locks and audit
> trails. Everything else is a plain Django view — server-rendered templates, no
> build step, no JavaScript framework, no task queue. We'd argue the architecture
> is the opposite of over-engineered; the *transaction handling* is careful on
> purpose.

**Q: Why no JavaScript framework?**
> Nothing in these workflows needs one. The pages are forms and tables; the two
> places we wanted live behaviour — the search dropdown and the notification tray
> — are plain XHR against the same Django views. Avoiding a framework also avoided
> a Node build step and a set of CDN dependencies, which keeps the app working
> offline-first and keeps the deployment to one service.

**Q: Show me where you handle the case I'm about to ask about.**
> Happy to — and if it isn't somewhere we can point to, we'll say so rather than
> improvise. Our audit document lists what we found wrong in our own system.

**Q: You said staff can't verify their own payments. What about an Owner?**
> An Owner *can*, and that's a deliberate trust decision rather than an oversight.
> `is_self_confirmation` tests `user.role == STAFF` first, so Owner and Branch
> Manager never reach the identity comparison. In a single-owner store, requiring a
> second approver for the Owner would mean nobody could ever verify an
> Owner-recorded sale.

**Q: What's the weakest part of your system?**
> Probably the gap between accepting an online order and completing it. We
> deliberately don't hold stock, so a busy counter can leave an online order
> unfulfillable. The failure is safe — it rolls back and reports the shortfall —
> but resolving it is manual. A hard reservation with a timeout would be the next
> step, and we'd want to be careful, because a hold that leaks is worse than no
> hold at all.

**Q: If you had another month, what would you fix first?**
> The three confirmed defects, because they're small and they're real: the dead
> notification trigger, the misrouted About link, and wiring the preference
> toggles to something or disabling them. Then we'd align the API registration
> path with the web one so both produce accounts with the same capabilities. Then
> the stock reservation question above.

---

## 15. One-line answers to keep ready

| Question | Answer |
|---|---|
| Order statuses? | `Pending`, `Approved`, `Preparing`, `Completed`, `Cancelled` |
| Payment statuses? | `Pending`, `Paid`, `Rejected`, `Cancelled`, `Partially Refunded`, `Refunded` |
| Roles? | Owner, Branch Manager, Staff, Supplier, Customer |
| When is stock deducted? | Walk-in: at sale. Online: at completion. |
| Can you complete an unpaid order? | No — the billing row is locked and must read `Paid` |
| Can one order have two bills? | No — `OneToOneField` |
| Who picks the storefront branch? | The server, from a location hint + full cart coverage |
| Where's the cart stored? | The Django session; no database row until checkout |
| Where's the delivery address from? | The account's default `Address`, snapshotted onto the order |
| Email for orders? | No — in-app notifications only |
| Payment gateway? | None, by design |
| Refund window? | 7 days from `completed_at` |
| Large-order threshold? | ₱5,000 — **but the trigger currently never fires** |
| Free-delivery threshold? | ₱500 — **display only, no fee is modelled** |
| VAT? | 12%, extracted from VAT-inclusive shelf prices |
| PWD/Senior discount? | 20% off the VAT-exclusive base, after stripping VAT |
| Tests? | **1,712 pass** (952 core/sales/orders/billing + 760 accounts/inventory/procurement/reports); `manage.py check` clean |
| Known defects? | Three — see `07-audit-findings.md` |
