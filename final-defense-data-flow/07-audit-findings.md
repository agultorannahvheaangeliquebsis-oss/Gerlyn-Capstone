# 07 — Audit Findings

**Read-only audit.** No application code, migrations, settings, icons or
unrelated files were modified. Only this documentation folder was added.

> **Note on finding IDs.** `A-n` / `B-n` / `C-n` / `D-n` below are **this
> audit's** IDs. The source code carries its *own* unrelated finding codes in its
> comments — `A-1` (maker/checker), `B-3` (channel-specific payment methods),
> `F-02`, `F-04`, `F-06`, `F-10`, `F-11`, `S-1`, `S-9`, `S-11`, `X-1`–`X-4`,
> `O-1`, `O-3`, `D-1`, `P14`, `P15`, `KATRINA-*`. Where those are cited elsewhere
> in this folder they are named as the code's own. The two schemes overlap by
> coincidence; they are not the same findings.

Findings are split into the three categories the brief asked for:

- **§1 Verified correct behaviour** — checked against the code and found sound
- **§2 Confirmed implementation problems** (`A-n`) — defects with a demonstrable wrong outcome
- **§3 Confirmed inconsistencies** (`C-n`) — real behavioural divergences, lower severity
- **§4 Potential issues requiring further testing** (`B-n`) — plausible, not proven
- **§5 Informational** (`D-n`) — worth knowing, not wrong
- **§6 Documentation drift** — stale comments, no behavioural impact

### Validation performed

| Command | Result |
|---|---|
| `python manage.py check` | **System check identified no issues (0 silenced).** |
| `python manage.py test core sales orders billing --parallel 1` | **Ran 952 tests — OK** (1,188 s) |
| `python manage.py test accounts inventory procurement reports --parallel 1` | **Ran 760 tests — OK** (467 s) |
| **Total** | **1,712 tests, 0 failures, 0 errors** |

Run with `venv\Scripts\python.exe`, `DB_NAME` pointed at a scratch database, and
`PYTHONIOENCODING=utf-8`. The findings below were reached by source inspection
and by URL resolution through `django.urls.resolve` (for **A-2**); they are **not**
test failures, which is precisely why they survive a green suite.

---

## 1. Verified correct behaviour

### 1.1 Status transitions

| Check | Verdict |
|---|---|
| Order transitions constrained to a declared map | ✅ `_FLOW`, re-checked inside the lock by `transaction_set_status` |
| `_next_actions` asked **with** the user, so the buttons drawn match what is allowed | ✅ Fixed deliberately — the comment notes the two checks "can drift apart with nobody noticing" otherwise |
| Walk-in `Pending` overridden to `['Completed','Cancelled']` | ✅ `Approved`/`Preparing` are meaningless once goods have left the counter |
| Payment transitions constrained | ✅ `ALLOWED_STATUS_TRANSITIONS` + `can_transition_to`, every writer routed through it |
| Humans cannot pick `Cancelled` / `Refunded` / `Partially Refunded` directly | ✅ `HUMAN_CONFIRMABLE_STATUSES` — only `transaction_set_status` and `sales.refunds` pass those |
| `Paid` is terminal except via void or refund | ✅ No outbound edge back to `Pending`/`Rejected` |
| Stale-view protection on status changes | ✅ `expected_status` echo: *"it changed while you were reviewing it"* |
| Cancellation requires a reason | ✅ Required for `Cancelled`, optional note otherwise, length-capped at 255 before the DB |
| Rejection requires a reason | ✅ `billing_confirm` refuses without one |
| Re-open clears the prior decision | ✅ `confirmed_by`, `date_confirmed`, `reject_reason` all cleared |

### 1.2 "Orders marked complete before payment is satisfied" — **cannot happen**

```
if new_status == COMPLETED:
    paying = BillingRecord.objects.select_for_update().filter(transaction=locked_txn).first()
    if not paying or paying.status != PAID:
        raise _OrderActionError(...)
```

The billing row is **locked and re-read**, so a payment rejected in the gap
cannot slip a completion through. The one case where an order is `Completed`
without passing this check is a **Cash walk-in**, which is created `Completed`
**and** `Paid` in the same atomic block — the money crossed the counter. That is
correct, not an exception.

### 1.3 Duplicate billing or order creation — **structurally prevented**

| Risk | Guard |
|---|---|
| Two billing records for one order | `BillingRecord.transaction` is a **`OneToOneField`** — DB-enforced |
| Billing record missed entirely | `post_save` signal, same atomic block as the order |
| Billing created at `amount_due=0` | Total computed **before** the INSERT and passed to it |
| Duplicate order from a double-tap | `cache.add()` atomic set-if-absent **+** session token **+** token rotated *before* creation (stops a slower third copy) |
| Duplicate `order_number` | `OrderNumberSequence.select_for_update()`, number written **on the INSERT** — no row ever sits under the unique index at `''` |
| Duplicate POS receipt reference | `UniqueConstraint(['branch','pos_invoice_number'])` + pre-check + re-check under `Branch.select_for_update()` |

### 1.4 Stock exactly-once — **verified across all four channel/state combinations**

| Scenario | Deducts | Restores | Net |
|---|---|---|---|
| Cash walk-in → `Completed` at creation | At creation | — | −qty ✅ |
| Cash walk-in → later `Cancelled` (void) | At creation | `_reverse_stock` (Owner/Manager only) | 0 ✅ |
| GCash walk-in created `Pending` → `Completed` | At creation | — (`ONLINE` guard blocks a second deduction) | −qty ✅ |
| GCash walk-in created `Pending` → `Cancelled` | At creation | `_reverse_stock` (`was_completed=False` **and** `Physical`) | 0 ✅ |
| Online → `Completed` | At completion | — | −qty ✅ |
| Online → `Cancelled` before completion | **Never** | **Nothing to restore** | 0 ✅ |
| Online → `Completed` → `Cancelled` | At completion | `_reverse_stock` | 0 ✅ |
| Refund, `Sellable` | — | `+qty`, `Customer Return` | ✅ |
| Refund, `Damaged` | — | **No restock** — correct | ✅ |

**"Missing stock restoration after cancellation" is not a defect here.** An
online order cancelled before completion restores nothing *because nothing was
ever deducted*. This asymmetry is the whole point of the design and is worth
stating explicitly, since it looks like a gap until the timing is understood.

Three further guards:

- **Void + refund are mutually exclusive.** `has_approved_refunds(txn)` blocks a
  void, because `_reverse_stock` credits the *full* quantity and would restock
  refunded units twice.
- **Batch-level reversal** credits the *same* lots via
  `SalesItemBatchConsumption` (O-1), with an honest no-expiry fallback for
  pre-tracking sales.
- **Deleted products** are skipped on reversal and the actor is **warned** which
  lines could not be restored — not silently short-restocked.

### 1.5 Totals and pricing

| Check | Verdict |
|---|---|
| Prices never read from the client | ✅ Both creation paths resolve `Product.price_at()` server-side |
| One arithmetic authority | ✅ `sales.pricing` — POS preview, written sale, receipt, checkout, re-pricing |
| VAT extracted, never added | ✅ Shelf prices are VAT-inclusive |
| Statutory discount strips VAT *then* takes 20% | ✅ RA 10754 / RA 9994. Worked example in the docstring: ₱112 → ₱100 → ₱80, not ₱89.60 |
| Rounding half-up, once, per unit | ✅ `peso()` — so `SalesItem.save()`'s independent `quantity * unit_price` recompute agrees by construction |
| Quantity totalled per product before the tier is read | ✅ 5 + 5 earns the same break as one 10 |
| `total_amount` never rewritten by a refund | ✅ `refunded_amount` + the `net_amount` property |
| Discount lives in `unit_price`, not subtracted twice | ✅ Documented at every call site |

### 1.6 Validation and permission checks

| Check | Verdict |
|---|---|
| Three independent authorization layers, all server-side | ✅ Role → module/action → branch |
| Branch scoping fails **closed** | ✅ `[]` for an unassigned Manager/Staff, `None` only for the Owner; `core/scoping.py` warns against the fail-open idiom |
| `?branch=` cannot widen scope | ✅ Applied **after** `scope_queryset` → intersection |
| Non-numeric `?branch=` rejected | ✅ `.isdigit()` guard before the ORM |
| Every write action also requires View | ✅ `user_can_perform_module_action` |
| Reports capped to Owner/Manager in three places | ✅ `management_report_required`, `user_can_perform_module_action`, `ModulePermissionMiddleware` |
| Order ownership enforced in the **query** | ✅ `my_orders`, `track_order`, `order_confirmation` → 404, never a revealing 403 |
| API billing scoping | ✅ `user_can_access_billing_record`; **404** convention (F-02) |
| Maker/checker separation | ✅ `is_self_confirmation` — Owner/Manager exempt by role, null-actor safe |
| Open-redirect protection | ✅ `safe_next`, `_safe_next`, `_safe_get_next`, `_safe_referer` |
| Uploads are real images | ✅ Pillow `verify()` in `_validate_avatar` and `_validate_proof_of_payment` |
| Payment proofs not publicly served | ✅ `payment_proofs/` excluded from `/media/` (F-06) |
| State-changing reads made POST | ✅ `mark_notification_read` |
| Customer checkout cannot self-grant a discount | ✅ `discount_type=None` passed **explicitly** (F-04) |
| Tampered `fulfillment_type` refused, not defaulted | ✅ No safe guess exists |
| Tampered payment method falls back safely | ✅ To `Cash` (POS) / `Cash on Delivery` (online) — both recorded-only |
| Payment method validated **per channel** | ✅ `payment_methods_for_channel`, re-checked on POST (B-3) |
| Customers removed from Billing pages | ✅ `role_required` (X-1) — controls were previously drawn and clickable |

### 1.7 POS ↔ online consistency

| Shared rule | Both paths read |
|---|---|
| Pricing / VAT / discount | `sales.pricing` |
| Price tiers | `Product.price_at` |
| Payment evidence | `BillingRecord.missing_payment_evidence` |
| Reference format | `billing.validators.clean_reference_number` |
| Proof validation | `billing.views._validate_proof_of_payment` |
| Sellable stock | `Inventory.objects.sellable()` |
| Deduction | `sales.views._deduct_stock` |
| Status transitions | `sales.views.transaction_set_status` |
| Billing creation | `billing.signals` |
| Order numbering | `SalesTransaction.save` |

The **differences** (stock timing, who may discount, which payment methods, who
the actor is) are all deliberate and documented. See
[`01-sales-and-orders.md`](01-sales-and-orders.md) §1.

### 1.8 Record linkage

| Link | Verdict |
|---|---|
| Order ↔ billing | ✅ `OneToOne`, created together |
| Order ↔ items | ✅ `CASCADE`; items immutable by design |
| Order ↔ history | ✅ Two complementary tables, merged by `_status_timeline`, orphans preserved |
| Sale line ↔ batches | ✅ `SalesItemBatchConsumption` (O-1) |
| Refund ↔ order ↔ billing | ✅ `refunded_amount` and `BillingRecord.status` updated under the approval's own lock |
| Customer ↔ orders | ✅ `SET_NULL` — erasure anonymises without destroying history |
| Branch ↔ everything | ✅ `SET_NULL` throughout — no cascade loss of sales history |
| Actor trail | ✅ `recorded_by` (X-3), `confirmed_by`, **separate** `voided_by`/`date_voided` (F-10), `discount_applied_by`, `refunded_by`, `StockMovement.created_by` |

### 1.9 Reports reflect actual transactions

| Check | Verdict |
|---|---|
| One shared revenue definition | ✅ `reports/services.py`, imported by **both** web views and API — the views module's own copy was the original drift |
| `Cancelled` excluded from money, **included** in the table | ✅ Fixed a real bug: a voided ₱300 sale showed ₱300 on Sales and ₱0 on Billing |
| Refunds subtracted separately | ✅ A refund never changes `order_status`, so gross alone would count returned money forever |
| Only `Approved` refunds counted | ✅ Pending/Rejected moved no money |
| Pipeline shown separately, never folded into revenue | ✅ `pipeline_sales` |
| CSV export matches the page | ✅ Same branch + range filters (KATRINA-REPORT-002) |
| Timezone-safe range filtering | ✅ `local_range_bounds`, not `__date__gte` (which silently matched zero rows on MySQL) |
| CSV formula injection | ✅ `_csv_safe` |

### 1.10 User feedback — no silent exits

The project treats a silent `return redirect()` as a defect. Verified examples:

- Cart decrease at the floor **says so**: *"X is down to one. Use Remove to take
  it out of your cart."*
- Already-removed item (second tab / double-click) **says so** (S-9).
- Unrecognised cart action **says so**: *"That cart change was not understood."*
- Unknown notification preference field **says so** — previously a bare redirect,
  so *"a typo'd switch looked exactly like a working one."*
- Unavailable language **says so** — previously silent, which *"is exactly what a
  save that failed to stick looks like."*
- POS validation **collects** errors and rebuilds the basket, including rows that
  failed the digit check.
- Dropped cart lines are **named**.
- `billing_create` distinguishes `Cancelled` from `Rejected` from "already
  exists" — the generic message *"reads as if nothing was wrong."*
- `billing_confirm` names the **next step** for each outcome.
- An unfillable cart **names the blocking products**.
- A refused self-confirmation is announced when the payment is *recorded*, "rather
  than let them discover it only after clicking Verify."

---

## 2. Confirmed implementation problems

### A-1 — The large-order notification can never fire

| | |
|---|---|
| **Severity** | Medium — a designed Owner control silently does nothing |
| **File** | `sales/signals.py:26` |
| **Status** | **Confirmed by code reading. No test covers it.** |

```python
@receiver(post_save, sender=SalesTransaction)
def notify_large_order(sender, instance, created, update_fields, **kwargs):
    if not (update_fields and 'order_number' in update_fields):
        return
```

The guard waits for a save carrying `update_fields={'order_number'}`. The
docstring explains why:

> *"`SalesTransaction.save()` does an initial insert with `order_number` still
> blank (`created=True`), then immediately a second save assigning it
> (`update_fields={'order_number'}`, `created=False`) — this signal fires on
> BOTH, and only the second carries a real `order_number`."*

**That is no longer how creation works.** `SalesTransaction.save()` was changed to
allocate the number inside the atomic block and write it **on the INSERT itself**:

> *"`order_number` is written on the INSERT itself, so the old create-then-patch
> step is gone."*

So creation is now a **single** save with `update_fields=None`. A repository-wide
search finds **no** call anywhere that saves with `update_fields` containing
`order_number`, except `sales/migrations/0002_…`. Every later save
(`locked_txn.save()` in `transaction_set_status`, etc.) also passes
`update_fields=None`.

**Failure scenario.** An Owner records or receives any order above
`LARGE_ORDER_THRESHOLD` (₱5,000). No `Notification` row is created. The
Notification Center shows nothing, and the Owner has no idea the review trigger
they were told exists is inert.

**Why the green test suite missed it:** there is no test for
`notify_large_order` at all — searching for the function name or the string
`'Large order placed'` finds only `sales/signals.py`.

**Honest answer if a panel asks:** *"That trigger is currently dead. The order
number used to be written in a second save and the signal waits for that second
save; we moved the number onto the insert to close a concurrency hole and the
signal's condition was not updated with it. It's a one-line fix and it's on our
list."*

---

### A-2 — The account rail's "About" link cannot reach the view written for it

| | |
|---|---|
| **Severity** | Medium — a navigation item goes somewhere other than intended; two templates are unreachable |
| **Files** | `gerlyn_is/urls.py`, `accounts/urls.py`, `accounts/views.py:2021`, `templates/accounts/_account_sidebar.html` |
| **Status** | **Confirmed by live URL resolution** |

The root URLconf registers:

```python
path('about/', about_view, name='about'),          # core.views.about_view
...
path('', include('accounts.urls', namespace='accounts')),
```

and `accounts/urls.py` registers its own:

```python
path('about/', views.about_view, name='about'),    # accounts.views.about_view
```

Both reverse to the **same path**, and Django resolves the **first** matching
pattern. Verified with `django.urls`:

```
accounts:about  ->  /about/  ->  core.views.about_view
about           ->  /about/  ->  core.views.about_view
```

**Consequences:**

1. `accounts.views.about_view` is **unreachable dead code**.
2. `templates/accounts/about.html` and `templates/procurement/supplier_about.html`
   are **orphaned** — rendered only by that unreachable view.
3. Clicking **About** in the account rail **leaves the account shell**: the
   customer lands on the public storefront About Us page, losing the sidebar, and
   `active_page` never matches so nothing highlights.
4. That rail row is **not login-gated**, unlike every other row beside it
   (`core.views.about_view` has no decorator).
5. `core.views.about_view`'s own docstring is now false: it describes the accounts
   version as *"reached from there"*.

**Failure scenario.** A signed-in customer on My Orders clicks **About** in the
left rail. Instead of the Settings-shelled About card they are dropped onto the
public marketing page with the shop header and no way back into their account
except the header icons. The Supplier variant is unreachable by any route.

**Fix shape (not applied):** give the accounts route a distinct path
(e.g. `profile/about/`), or remove it and point the rail at the public page
deliberately.

---

### A-3 — Notification preferences are saved but never consulted

| | |
|---|---|
| **Severity** | Medium — a working-looking control with no effect |
| **Files** | `accounts/models.py:309` (`NotificationPreference`), `accounts/services.py:85` (`should_notify`), `accounts/views.py:1980` |
| **Status** | **Confirmed — `should_notify` has zero call sites** |

The Notifications page offers four toggles — `order_updates`, `promotions`,
`delivery_reminders`, `newsletter` — and `notification_preferences_update` saves
each one and confirms it:

```python
messages.success(request, f"{LABEL} turned {'on' if on else 'off'}.")
```

The gate function exists:

```python
def should_notify(user, kind):
    """Gate for any outbound notification (email/push/SMS) tied to a user
    preference toggle. Call this before sending ..."""
```

A repository-wide search for `should_notify` returns **two** hits: its own
definition, and the example *inside its own docstring*. It is called from
nowhere. `notify_customer`, `notify`, and `notify_event` do not consult
`NotificationPreference` at all.

**Failure scenario.** A customer turns **Order updates** off. They continue to
receive a `Notification` row for every status change, every payment decision, and
every PWD decision — with the UI having told them the setting was applied.

**Mitigating context:** the toggles' labels talk about outbound channels
(email/push/SMS), and none of those exist for order events — every order notice
is an in-app row. So the toggles were arguably written ahead of a channel that
was never built. That is still a control that reports success and does nothing.

**Fix shape (not applied):** either call `should_notify(customer,
'order_updates')` inside `notify_customer`, or disable the toggles with an
explanatory note until an outbound channel exists.

---

## 3. Confirmed inconsistencies

### C-1 — `/api/auth/register/` diverges materially from the web registration

| | |
|---|---|
| **Severity** | Medium |
| **Files** | `accounts/api_views.py` (`RegisterCustomerView`) vs. `accounts/views.py` (`register_view`) |
| **Status** | **Confirmed** |

Both are live, both are `AllowAny`, both create a `CUSTOMER` and log them in.
The API path omits:

| Check | Web | API |
|---|---|---|
| IP rate limiting | ✅ | ❌ |
| Username length + `username_validator` | ✅ | ❌ |
| `validate_email` **format** | ✅ | ❌ (uniqueness only) |
| `validate_ph_mobile_number` | ✅ | ❌ |
| `contact_number_taken` | ✅ | ❌ |
| Address **required** | ✅ | ❌ |
| **`Address` row created** | ✅ `is_default=True` | ❌ **none** |

It *does* share the consent gate (`acceptTerms`) and password validation, and it
wraps writes in `transaction.atomic()`.

**Failure scenario.** An account created via the API has no default `Address`.
At checkout, choosing **Delivery** is refused: *"Add a delivery address to your
account — open Settings then Addresses."* The customer can fix it in Settings, so
this is a divergence rather than a dead end — but two live signup paths produce
accounts with different capabilities, and the API path will also accept
`email="not-an-email"` and an unlimited number of attempts from one IP.

---

### C-2 — `/dashboard/customer/` lists products the storefront refuses to list

| | |
|---|---|
| **Severity** | Medium — it is the page a customer lands on after registering **and** after every login |
| **Files** | `accounts/views.py:323` (`customer_dashboard`), `templates/accounts/customer_dashboard.html`, `templates/partials/home_content.html`, `templates/partials/product_card.html` |
| **Status** | **Confirmed** |

`customer_dashboard` renders the **same** `partials/home_content.html` that
`home_view` does, but passes a different, unfiltered context:

```python
products = Product.objects.prefetch_related('images')[:8]
branches = Branch.objects.all()
categories = ProductCategory.objects.all()
```

versus `home_view`:

```python
products = attach_storefront_counts(list(
    Product.objects.select_related('category')
    .filter(id__in=in_stock_product_ids())     # <-- the gate
    .prefetch_related('images')[:12]))
branches = Branch.objects.filter(is_active=True)
... best_sellers=..., hero_slides=..., selected_branch_id=...
```

`partials/product_card.html` states its own contract explicitly:

> *"There is no stock badge and no out-of-stock state at all here: both callers
> filter to in-stock products before rendering (`inventory.stats.in_stock_product_ids`),
> so every card that renders is a card you can buy from."*

`customer_dashboard` is a **third** caller, and it does not.

**Four concrete effects:**

1. **Out-of-stock and never-stocked products are advertised** as buyable, with a
   live "Add to Cart" button. Pressing it fails in `add_to_cart` with *"… is not
   currently available for fulfillment"* — a refusal the storefront design
   deliberately avoids everywhere else.
2. **`best_sellers` is not passed**, so that whole rail silently disappears on
   this page.
3. **`hero_slides` / `hero_side_cards` are not passed**, so the hero falls back to
   the template's built-in copy — meaning the **Owner's Store Settings hero edits
   do not appear on the customer's own landing page.**
4. **`attach_storefront_counts` is never called**, so `units_sold` is unset and the
   *"N sold"* line never renders here (silently, since a missing template
   attribute resolves to empty).

`branches` and `categories` are passed but unused — `home_content.html` reads
`nav_branches` / `nav_categories` from context processors.

**Why the test suite missed it:** `orders/tests_storefront_truthfulness.py` covers
the catalog, the product page, the category rail and My Orders. It does not
exercise `customer_dashboard`, and `accounts/tests.py` only asserts the
*redirect* to it, never its context.

---

### C-3 — Unresolved BULK-badge gap left by the price-tier migration

| | |
|---|---|
| **Severity** | Low — a feature gap, flagged in-code |
| **File** | `orders/customer_views.py`, end of `product_detail` |
| **Status** | **Confirmed — a live `TODO` in the source** |

```python
# TODO: Dropped is_wholesale check — order_type_for() was removed in
# bb3aa4d (tier-pricing migration). Needs Marie's input on what should
# replace this for the BULK badge gating logic.
```

When `SalesTransaction.order_type` / wholesale pricing was replaced by
`ProductPriceTier`, the product page's BULK-badge gating lost its condition and
was not re-specified. No wrong data results — the badge simply does not gate on
anything. `product_card.html` separately decided **not** to draw a BULK flag at
all, so the two surfaces are at least not contradicting each other.

**Related, already recorded as intentional:** the wholesale buyer-name /
`recorded_by` audit trail dropped in the same migration is tracked to be rebuilt
against `ProductPriceTier`, not abandoned.

---

### C-4 — Two template contracts describe controls that no longer exist

| | |
|---|---|
| **Severity** | Low — misleading to a maintainer, no user-visible fault |
| **Files** | `templates/partials/_add_to_cart.html`, `orders/customer_views.py` (`product_detail`) |
| **Status** | **Confirmed** |

**`_add_to_cart.html`** says *"Expects `product` and `selected_branch_id` in
context"* and describes *"these same four branches"*, naming a **"Choose a
branch"** state. The file actually has **three** branches (anonymous,
non-customer, customer-with-form) and **never reads `selected_branch_id`**. The
branch state became obsolete when branch assignment became automatic —
`add_to_cart` resolves a branch server-side on the first add. `product_catalog`,
`home_view` and `product_detail` all still compute and pass `selected_branch_id`
for a consumer that no longer uses it.

**`product_detail`'s `can_buy`** is documented as *"The same conditions that reach
the live Add to Cart form further down the template (logged in, a Customer
account, **a branch chosen**, stock at it)"*, but the code is:

```python
can_buy = bool(request.user.is_authenticated
               and request.user.role == 'CUSTOMER'
               and product.on_hand > 0)
```

There is no branch check. Harmless in practice, because the template gates the
form separately on `{% if not selected_branch_id %}` and `can_buy` only drives
whether the tier ladder is *interactive*. But the stated invariant ("cannot drift
apart") does not hold.

---

### C-5 — No customer-facing surface shows a refunded amount

| | |
|---|---|
| **Severity** | Low–Medium — the status is honest; the figure is not the full story |
| **Files** | `templates/orders/my_orders.html`, `templates/orders/track_order.html`, `templates/orders/confirmation.html` |
| **Status** | **Confirmed** |

All three print `sale.total_amount`. `SalesTransaction.net_amount`
(`total_amount − refunded_amount`) is **never** rendered on a customer page, and
`refunded_amount` is not shown either.

What the customer *does* get: the payment pill correctly reads **"Refunded"** or
**"Partially Refunded"** (`partials/_billing_status_pill.html` handles all five
states for both audiences — an earlier version let those two fall through to the
"unpaid" branch, *"so a refunded order's payment pill lied to the customer"*).

**Failure scenario.** A customer refunded ₱200 on an ₱896 order opens My Orders.
The row reads **₱896.00** with a *Partially Refunded* pill. They can see
*that* a refund happened but not *how much*, and the only figure on screen is
the pre-refund charge. Internally, Reports correctly nets it out — so the
business view and the customer view disagree on the order's value.

---

### C-6 — `view_cart` claims a cleanup that nothing performs

| | |
|---|---|
| **Severity** | Low — the outcome is still correct |
| **File** | `orders/customer_views.py` (`view_cart`, and two comments in `product_catalog`) |
| **Status** | **Confirmed — the named function does not exist** |

```python
# branch. (_start_shopping_at then clears the stale lines, so this also
# cleans up a session left in that state by an older build.)
```

`_start_shopping_at` is referenced in three comments and one test docstring
(`orders/tests_branch_cart_decoupling.py`) but **is not defined anywhere** in the
codebase. Nothing clears the cart when the branch changes.

**Why the outcome is still fine:** `view_cart` redirects a cart-without-branch to
`select_branch`, whose POST runs `_resolve_cart_branch` against the existing
cart — which either finds a branch that covers every line or refuses. And
`_cart_items` prunes any line that cannot be priced. So the stale state resolves
itself; only the explanation is wrong.

---

## 4. Potential issues requiring further testing

### B-1 — Price drift between the displayed cart and the submitted checkout

The code path is consistent — the cart page and `checkout` both read
`Product.price_at()`, and `sales/tests_online_pricing.py::test_editing_the_ladder_moves_the_cart_and_checkout_together`
proves they move together. But that test **reloads the cart page** before checking
out. It does not cover a price change landing *after* the shopper's cart page
rendered and *before* they press Place Order.

In that window the customer is charged the **new** price with **no notice**. The
`checkout_token` guards duplicates, not price drift, and there is no
"price changed, please review" branch (unlike the branch-changed and
stock-short branches, which both bounce the shopper back to the cart).

**Recommended test:** render the cart, mutate `unit_price`/`ProductPriceTier`,
then POST checkout **without** re-rendering, and assert on what the customer is
charged versus what they last saw.

**Likely real-world impact:** low for this business (prices change rarely), but it
is squarely within the brief's "stale product prices" question.

### B-2 — An accepted online order can later fail at *Mark Completed*

Online orders are validated at checkout but not deducted. `fulfillable_quantities`
nets out open online commitments, but **walk-in POS sales deduct immediately and
are not themselves constrained by online commitments.** A busy counter can
therefore drain the floor below what an open online order needs.

`_deduct_stock` then raises `_OrderActionError` and the whole completion rolls
back — **safe**, with the exact shortfall reported. But the order is stuck
pending a manual decision (transfer stock in, or cancel), and the Order Console's
over-commitment flag is **reporting only** (nothing holds stock).

**Recommended test:** create an online order, deduct the same stock via the POS,
then attempt `Mark Completed` and assert the rollback, the message, and that no
`StockMovement` row was written.

### B-3 — The soft reservation is read-time only

`fulfillable_quantities` is computed per request. Between two reads, two shoppers
**can** both be shown the same last unit. The hard guard is the
`select_for_update()` lock in `checkout` (and `_deduct_stock`), which serialises
them — the second shopper is refused inside the lock. So the worst case is a
refusal at Place Order, not an oversell.

**Recommended test:** two concurrent `checkout` POSTs for the last unit; assert
exactly one order is created and the other gets the "only N left" message.

### B-4 — Notification writes sit inside the business transaction

In `transaction_set_status`, both `notify()` calls and `notify_customer` are
**inside** `transaction.atomic()`. A notification failure would roll back the
status change itself.

This is arguably the safe direction — no silent divergence between the order and
what anyone was told — but it puts notification delivery on the critical path of
a financial transition, with no outbox and no retry. Low real risk, since
`bulk_create` on the same connection is unlikely to fail independently.

**Recommended test:** patch `core.notifications.notify` to raise, POST a status
change, and assert `order_status` is unchanged (confirming the intended
all-or-nothing behaviour rather than a partial apply).

### B-5 — Scheduled commands are **UNVERIFIED** in the demo environment

`process_expired_stock`, `process_overdue_deliveries`, `dispatch_notifications`
and `check_batch_reconciliation` are referenced by `railway.cron.json`. **Whether
cron is actually running in the group's demo setup was not verified** and could
not be from the source alone.

**Do not claim during the defense that expired stock is swept automatically
unless the command has been run.** `Inventory.sellable()` protects every sale
path regardless, so nothing unsafe is sold in the meantime — say that instead.

---

## 5. Informational — verified safe, worth knowing

### D-1 — `CUSTOMER` carries `sales_pos` and `billing` in `ROLE_DEFAULT_MODULES`

```python
'CUSTOMER': [SALES_POS, BILLING],
```

This looks alarming. It is **inert**: `user_can_perform_module_action` returns
`True` for every role outside `('STAFF','BRANCH_MANAGER')`, so these entries are
never read as a grant, and `ModulePermissionMiddleware` returns early for a
customer. Actual customer access is decided entirely by the view-level role
checks — `billing_list` is `role_required(OWNER, BRANCH_MANAGER, STAFF)`, and
`transaction_detail` refuses any non-internal role. The file's own comment
explains the entries exist to *"keep irrelevant internal modules unchecked"* in
the role-switching UI.

**Be ready for this one** — it is the kind of line a panel member scanning the
code will stop on.

### D-2 — Two payment-proof endpoints with different role gates

| Endpoint | Decorators | Effective access |
|---|---|---|
| `billing_proof` (`/billing/<pk>/proof/`) | `@role_required(OWNER, BRANCH_MANAGER, STAFF)` + `user_can_access_billing_record` | Internal only |
| `payment_proof_download` (`/billing/<pk>/payment-proof/`) | `@login_required` + `@require_GET` + `user_can_access_billing_record` | Internal **and the owning customer** |

`user_can_access_billing_record` has a `CUSTOMER` branch
(`record.transaction.customer_id == user.id`), so a customer **can** fetch their
own order's proof from the second endpoint — while `billing_proof`'s docstring
asserts *"a customer's own proof of payment is not shown back to them anywhere in
this system."*

**Not a leak** (it is the customer's own upload, and only their own), and no
template links it for a customer. But the two endpoints disagree, and the
template comment in `_billing_payment_facts.html` claims they share *"the same
branch/record gate"* — true of the record gate, not of the role gate.

### D-3 — `Language` is a hand-rolled dictionary, not Django i18n

`{% t %}` (`accounts/templatetags/i18n_extra.py`) looks strings up in
`accounts/translations.py` — roughly 130 entries — and falls back to the English
text passed in. There is no `gettext`, no `.po` files, no `LocaleMiddleware`.

So Filipino coverage is **partial by construction**: any string not in the table
renders in English. The views' own section comment calls Language a placeholder
needing *"an actual i18n setup."*

**Describe it as "partial Filipino support", not "bilingual".** Two related
in-repo gotchas: `{% t %}` output is **unescaped**, and `{% load i18n_extra %}`
is per-file — adding `{% t %}` to a template that never used it breaks the page
without the load line.

### D-4 — Known not-built items, correctly hidden

| Item | State |
|---|---|
| **Payment Methods** rail row | Inside `{% comment %}` with *"page not yet built, placeholder link"* — **does not render**. Correct handling |
| Mobile storefront hero | Still hardcoded (outside Store Settings) |
| Address map picker | Leaflet / OpenStreetMap / Nominatim deliberately **removed** as out of scope; the picker is local Region/Province/City/Barangay dropdowns plus a typed address line. Reverse geocoding is optional and only used for a display label |
| Delivery fee | **No field anywhere.** The ₱500 "free delivery" meter is display only — do not claim a fee is charged or waived |
| Courier / routing / delivery radius | Not implemented |
| Payment gateway | Not implemented **by design** (Chapter 1, Limitation 1) |

---

## 6. Documentation drift (no behavioural impact)

| # | Location | Says | Reality |
|---|---|---|---|
| 1 | `sales/signals.py:17-21` | Creation is two saves, the second with `update_fields={'order_number'}` | One save, `update_fields=None`. **This stale comment is the cause of A-1** |
| 2 | `orders/customer_views.py:366, 367, 1125` + `orders/tests_branch_cart_decoupling.py` | `_start_shopping_at()` clears the cart on a branch change | Function does not exist (**C-6**) |
| 3 | `billing/models.py:25` | `sales.views._refundable_items` | It is `sales.refunds.refundable_lines` / `_units_not_refunded` |
| 4 | `core/views.py:35-38` | `accounts.views.about_view` is *"reached from there"* | Unreachable (**A-2**) |
| 5 | `templates/partials/_add_to_cart.html` | Four branches incl. "Choose a branch"; expects `selected_branch_id` | Three branches; never reads it (**C-4**) |
| 6 | `orders/customer_views.py` (`can_buy`) | Includes "a branch chosen" | It does not (**C-4**) |
| 7 | `templates/billing/_billing_payment_facts.html:215` | Both proof endpoints share *"the same branch/record gate"* | Same **record** gate, different **role** gate (**D-2**) |

---

## 7. Summary table

| ID | Finding | Severity | Status |
|---|---|---|---|
| **A-1** | Large-order notification never fires | Medium | **Confirmed defect** |
| **A-2** | Account rail "About" cannot reach its view; 2 orphaned templates | Medium | **Confirmed defect** |
| **A-3** | Notification preference toggles gate nothing | Medium | **Confirmed defect** |
| **C-1** | API registration diverges; creates no `Address` | Medium | **Confirmed inconsistency** |
| **C-2** | `/dashboard/customer/` lists unavailable products; drops hero + best sellers | Medium | **Confirmed inconsistency** |
| **C-3** | BULK-badge gating unresolved after the tier migration | Low | **Confirmed gap (in-code TODO)** |
| **C-4** | Two template/view contracts describe removed controls | Low | **Confirmed inconsistency** |
| **C-5** | No customer surface shows a refunded amount | Low–Medium | **Confirmed inconsistency** |
| **C-6** | `view_cart` claims a cleanup nothing performs | Low | **Confirmed (comment only)** |
| **B-1** | Price drift between the rendered cart and the POST | Medium? | **Needs testing** |
| **B-2** | Online order can fail at *Mark Completed* after POS drains stock | Low–Medium | **Needs testing** |
| **B-3** | Soft reservation is read-time only | Low | **Needs testing** |
| **B-4** | Notification writes inside the business transaction | Low | **Needs testing** |
| **B-5** | Cron scheduling in the demo environment | — | **UNVERIFIED** |
| **D-1** | `CUSTOMER` default modules look wrong, are inert | — | Informational |
| **D-2** | Two proof endpoints, different role gates | — | Informational |
| **D-3** | Language is a 130-entry dictionary, not i18n | — | Informational |
| **D-4** | Known not-built items, correctly hidden | — | Informational |

### What this means for the defense

**Nothing in this list breaks the money or the stock.** Exactly-once stock,
one-billing-per-order, payment-before-completion, branch isolation, maker/checker
separation, and the refund/void mutual exclusion all hold under inspection. The
three confirmed defects are a **dead notification trigger**, a **misrouted
navigation link**, and a **preference control with no effect** — all
user-experience and completeness issues, none of them data-integrity issues.

The honest framing: *"We audited our own Financials and Storefront flows and
found three real defects and a handful of inconsistencies. None of them affect
the correctness of a transaction, and we can show you exactly where each one is."*

**That is a stronger position than claiming the system is flawless** — and if the
panel finds any of these on their own, they will already be in your document.
