# GerlynPLUS QA Review — Katrina / Regression, Negative Testing & System Monitoring

> **Remediation status (2026-10-02, same day):** KATRINA-REG-001, KATRINA-ALERT-001, KATRINA-REPORT-001, KATRINA-REPORT-002, KATRINA-RBAC-001, KATRINA-BACKUP-001, KATRINA-BACKUP-002 and KATRINA-AUDIT-001 have all been fixed, with new/updated automated tests, per the business rules supplied by the team. KATRINA-REG-002 was traced (see its section below) and confirmed to be a **stale test**, not a live application defect — left unfixed as instructed. Business-rule decisions BR-KAT-001 and BR-KAT-002 were resolved by the team as: Net Procurement everywhere (Gross/Returns/Credits preserved in detailed reporting), and `receipt_reference` required only when reconciling against an imported/external POS transaction, respectively. Findings below are left as originally written (the as-found record); fix details are not retrofitted into the narrative.

Date: 2026-10-02 (Asia/Manila)
Scope: Dashboard, System & Monitoring (Reports, Alerts, Audit Log, Backups), notification badge/count accuracy, regression across all modules, negative/boundary testing, session/duplicate/permission behavior for the monitoring surfaces.
Method: source trace of the current working tree (includes uncommitted in-progress fixes), plus a live run of the existing automated suite (`manage.py test audit backups reports core --keepdb`). No production data was read, changed, or deleted. Nothing in this document was fixed — findings only, per the review brief.

## Executive result

The monitoring stack has solid foundations: the audit log is genuinely append-only (model, queryset, admin, *and* a MySQL trigger layer), alert/report math is centralized in `inventory.alerts` and `reports.services` so pages can't independently drift, and branch scoping on Dashboard/Reports/Alerts fails closed via `scope_queryset`/`branch_ids_for_user`.

The regression sweep nevertheless found **one Critical regression that broke 11 previously-passing acceptance tests today**, plus **6 definite technical defects** (1 High, 4 Medium, 1 Low) in Alerts/Reports/Audit/Backups, and **2 business-rule decisions**. Highest risk first:

1. the newest commit on this branch (`145ae7c`, same day) made `receipt_reference` a hard-required field on the walk-in/manual sales form without updating `core`'s acceptance tests — every manual sale in those tests now silently fails validation, so **stock is not deducted, billing is not created, and the PWD/Senior discount path is untestable as of right now**;
2. the sidebar/bottom-nav **Alerts badge undercounts**: it only counts floor low-stock rows, while the Alerts Center page it links to also surfaces warehouse low-stock, expired/expiring batches, and overdue deliveries — a badge of "0" can sit next to a page full of urgent items;
3. the Dashboard's "Procurement Cost" KPI is **gross** while the in-progress Procurement Report/API fix (uncommitted) now shows **net of supplier returns** for the same scope — the same label means two different numbers on two pages;
4. Procurement/Billing/Inventory CSV exports **ignore the active date-range filter** shown on the report page (Sales CSV honors it; the other three always export full history);
5. Staff's default permissions **block the expiry write-off action** that the view's own code explicitly describes as "open to Staff too as a safe operational fallback."

## 1. Monitoring surface map

| Area | Routes | Main handlers |
|---|---|---|
| Dashboard | `accounts:owner_dashboard` / `manager_dashboard` / `staff_dashboard` (Staff gets its own simpler view) | `accounts.views.owner_dashboard`/`manager_dashboard` → `reports.views.operational_dashboard`; `accounts.views.staff_dashboard` (separate, task-oriented) |
| Reports | `/reports/`, `/reports/sales/`, `/reports/inventory/`, `/reports/procurement/`, `/reports/billing/`, `/reports/export/<kind>.csv` | `reports_dashboard`, `sales_report`, `inventory_report`, `procurement_report`, `billing_report`, `export_csv` |
| Alerts Center | `/inventory/alerts/`, `/inventory/alerts/write-off/<pk>/` | `inventory.views.alerts_center`, `expiry_write_off` |
| Notification Center (cross-department — distinct from Alerts Center) | `/internal/notifications/`, `/read/`, `/resolve/`, `/read-all/` | `core.views.notification_center_view` + 3 POST actions |
| Audit Log | `/monitoring/audit-log/`, `/monitoring/audit-log/action/<group_key>/` | `audit.views.audit_log_list`, `audit_action_detail` |
| Backups | `/monitoring/backups/`, `run-now/`, `restore-test/`, `trigger/` | `backups.views.backup_status`, `backup_run_now`, `backup_record_restore_test`, `backup_trigger` |

### Shared engines (single source of truth, by design)

- `inventory.alerts`: `low_stock_alerts`, `low_warehouse_stock_alerts`, `expiry_alerts`, `alert_summary` — feeds Alerts Center **and** the Dashboard's restock/needs-attention panels.
- `reports.services`: `revenue_sales`, `gross_sales_total`, `refund_value_total`, `procurement_cost_total`, `procurement_return_totals` (new, uncommitted), `scoped_report_querysets` — feeds `reports/views.py` (templates) and `reports/api_views.py` (DRF) so the two surfaces can't independently drift.
- `audit.capture` + `audit.registry`: signal-driven, fail-open capture into `AuditLog`, append-only at the model, queryset, admin, and MySQL-trigger layers.

### Permission gates

- Dashboard: `role_required`/direct role check — Owner, Branch Manager, Staff (Staff gets a separate, simpler view).
- Reports: gated by the `REPORTS` module — **Staff does not get it by default** (`ROLE_DEFAULT_MODULES['STAFF']` omits `REPORTS`); this is consistent with the sidebar and appears intentional, not a bug.
- Alerts Center: gated by the `ALERTS` module, granted to Staff by default (`VIEW` only; `DELETE` — write-off — is not granted by default, see KATRINA-RBAC-001).
- Audit Log / Backups: **not** module-gated — hard-coded `request.user.role == OWNER or is_superuser` inside each view (`audit.views.can_view_audit_log`, `backups.views.can_manage_backups`). Correct and simple, since there is nothing to grant per employee, but it does mean these two pages have no per-account delegation path at all — any trusted non-Owner helper would need actual Owner credentials.

## 2. Definite technical defects

### KATRINA-REG-001 — Same-day regression: manual sales entry silently fails without a receipt/reference number, breaking 11 acceptance tests

- **Role:** Staff (`ana`), any branch.
- **Preconditions:** Current working tree at commit `145ae7c` ("feat: prioritize POS imports for sales entry", committed today).
- **Exact steps:**
  1. `venv\Scripts\python.exe manage.py test audit backups reports core --keepdb -v 1`
  2. Observe 11 failures, all in `core.tests.AcceptanceTests` and `core.tests_feature_update.FeatureUpdateTests` — stock-deduction, bulk/tier pricing, and PWD/Senior-discount scenarios.
  3. Trace: `sales/views.py:transaction_add` (`git show 145ae7c -- sales/views.py`) now runs, unconditionally, before any transaction type/branch check:
     ```python
     if not receipt_reference:
         errors.append('Enter the receipt/reference number from the original transaction.')
     ```
     `receipt_reference` comes only from `request.POST.get('receipt_reference')`.
  4. None of `core/tests.py`'s or `core/tests_feature_update.py`'s POSTs to `/sales/add/` include `receipt_reference` — the view now rejects every one of them with a form error and creates no `SalesTransaction`.
  5. Each test's `SalesTransaction.objects.order_by('-id').first()` then silently returns a **pre-existing seed_demo transaction** instead of a new one, which is why unrelated tests converge on the same wrong numbers (e.g. unit_price reading `12.00` instead of the test's own `41`/`48`; `total_amount` reading `156.00` regardless of the scenario).
- **Expected:** A behavior change to an existing, documented, acceptance-tested workflow should not land without updating every test exercising that workflow — `core/tests.py`'s own docstring states these are "the checks the ISO 25010 evaluation leans on for Functional Suitability, Reliability, and Security... they must stay green."
- **Actual:** `sales/tests.py`, `sales/tests_pos_friction.py`, `sales/tests_pos_import.py`, `sales/tests_online_pricing.py`, `sales/tests_pos_payment_evidence.py` were all updated in the same commit; `core/tests.py` and `core/tests_feature_update.py` (different app, same view) were not. The commit's own stock-deduction/pricing/discount coverage inside `sales/` may still be green — this review did not re-run `sales` — but the project-wide acceptance suite is now red, and a real cashier hitting `/sales/add/` without typing something into a `receipt_reference` field the UI may not yet even surface prominently gets the same silent-looking rejection.
- **Severity:** **Critical** as a regression (breaks 11 previously-green acceptance tests across stock deduction, tiered pricing, and the statutory PWD/Senior discount path in one sitting); **High** as a standing defect once the test suite is realigned, because it is unclear whether every legitimate walk-in cash sale always has a "receipt/reference number from the original transaction" to type in, or whether this should only be required for a specific entry mode.
- **Affected:** `sales/views.py:transaction_add` (lines ~2033–2044); `sales/models.py` (`pos_invoice_number` repurposed as the shared dedupe key, per the commit's own model-comment change); `core/tests.py`, `core/tests_feature_update.py`.
- **Reproducible:** **Yes**, deterministically — see the test run below.
- **Regression:** **Yes**, introduced by commit `145ae7c`, same day as this review.

### KATRINA-ALERT-001 — Sidebar/bottom-nav Alerts badge undercounts what the Alerts Center actually shows

- **Role:** Owner, Branch Manager, or Staff — any branch.
- **Preconditions:** A branch with 0 floor-low-stock rows but ≥1 warehouse-low-stock row, expired/expiring batch, or overdue delivery.
- **Exact steps:**
  1. Compare `core/context_processors.py::alerts()` — `count = len(low_stock_alerts(branch_ids_for_user(user)))` — against `inventory/views.py::alerts_center`, which renders `alert_summary()`'s full set: `critical_count`, `low_count` (floor), `warehouse_critical_count`, `warehouse_low_count`, `expired_count`, `expiring_soon_count`, `approaching_expiry_count`, plus a separate `overdue_deliveries` list.
  2. Set up a branch with, say, 2 expired batches and 0 floor low-stock rows.
  3. Load any internal page and read the sidebar/bottom-nav Alerts badge.
- **Expected:** The badge is a faithful count of "what's waiting on this screen" — either every urgent item the Alerts Center shows, or an explicit, documented subset.
- **Actual:** The badge reads `0` while the Alerts Center it links to shows 2 expired-batch rows needing a write-off decision. The `operational_dashboard`'s own "Restock Suggestions"/`low_stock_count` tile has the identical narrowing (`restock = summary['low_stock']`, floor only), so the Dashboard quietly agrees with the under-counting badge rather than with the fuller `needs_attention` panel it also renders from the same `summary`.
- **Severity:** **Medium** (misleading monitoring UI, not data loss — the full picture is still correct and reachable on the Alerts Center page itself; no workflow is blocked). Would be **High** if the business intent is that "0" on this badge should mean nothing needs attention anywhere.
- **Affected:** `core/context_processors.py::alerts()`; `inventory/alerts.py::alert_summary()`/`low_stock_alerts()`; `reports/views.py::operational_dashboard` (`restock`, `low_stock_count`).
- **Reproducible:** **Yes**, deterministically from the two code paths.
- **No automated coverage:** `alert_count` has zero test references anywhere in the repo (`grep -rl alert_count` finds only its own definition).

### KATRINA-REPORT-001 — Dashboard "Procurement Cost" KPI disagrees with the Procurement Report/API's new Net figure for the same scope

- **Role:** Owner or scoped Manager/Staff.
- **Preconditions:** A branch/date range with at least one `Shipped` or `Settled` supplier return.
- **Exact steps:**
  1. Open Reports → Procurement for a range with a settled/shipped supplier return. Observe the new **Gross Procurement / Supplier Returns / Supplier Credits / Net Procurement** KPI row (uncommitted change to `reports/views.py::procurement_report` + `templates/reports/procurement.html`), and the DRF `DashboardSummaryAPIView`'s matching `grossProcurement`/`netProcurement` fields (uncommitted change to `reports/api_views.py`).
  2. Open the Dashboard for the same branch/range and read the "Procurement Cost" KPI card.
  3. Trace the Dashboard figure to `reports/views.py::_dashboard_daily_series` (unchanged by the in-progress fix): `buckets[order_date]['procurement_cost'] += cost or 0`, where `cost` is `total_cost` straight off `Procurement` — gross, no return/credit deduction anywhere in `_dashboard_metrics`/`operational_dashboard`.
- **Expected:** One figure, one definition, across the Dashboard, the Procurement Report, and the Dashboard API — exactly the problem `reports/services.py`'s own module docstring says this architecture exists to prevent ("reports/views.py and reports/api_views.py used to each compute their own version... which is how they drifted apart").
- **Actual:** The Procurement Report page and the Dashboard API now explicitly separate Gross from Net; the server-rendered Dashboard's "Procurement Cost" card, its trend chart, and its period-over-period delta pill are still unconditionally gross. A reconciliation pass comparing Dashboard vs. Reports for a branch with any supplier return in the period will not match either the Report's Gross or (by design) its Net figure's intent.
- **Severity:** **Medium** (reporting inconsistency, not data corruption — both numbers are individually correct for what they measure; nothing is silently wrong, but nothing says the Dashboard figure is gross either).
- **Affected:** `reports/views.py::_dashboard_daily_series`, `_dashboard_metrics`, `operational_dashboard`; contrast `reports/views.py::procurement_report` and `reports/api_views.py::DashboardSummaryAPIView` (both already updated, uncommitted).
- **Reproducible:** **Yes**, deterministically from the code path; not executed against live data.
- **Business-rule link:** This is the partial landing of BR-JP-001 from the 2026-10-02 Joshua/Procurement review ("should procurement reports be gross or net of supplier returns?") — see BR-KAT-001 below.

### KATRINA-REPORT-002 — Procurement/Billing/Inventory CSV exports ignore the report page's active date-range filter

- **Role:** Owner or Branch Manager (Staff cannot export — confirmed by `export_csv`'s own role check).
- **Preconditions:** Procurement (or Billing) Report filtered to a narrow date range with fewer POs/records than the branch's full history.
- **Exact steps:**
  1. Open Reports → Procurement, pick a short date range (e.g. "Last 7 days").
  2. Click Export CSV (`templates/reports/procurement.html`'s link is a bare `{% url 'export_csv' 'procurement' %}` — no query string at all).
  3. Compare the CSV's row count/date spread against the page's own filtered table.
  4. Repeat for Billing; repeat the Sales tab for contrast.
- **Expected:** Either the export matches what the filtered page is showing, or the UI makes clear that export is always a full-history dump.
- **Actual:** `reports/views.py::export_csv` only reads/applies a date range for `kind == 'sales'` (`sales_range`, gated on `any(key in request.GET for key in ('range','date','date_from','date_to','month'))`). For `kind in ('inventory', 'procurement', 'billing')` the queryset is branch-scoped only — full history, every time — and the templates don't even try to pass the range through. A user who filters Procurement to "Last 7 days" and exports gets every PO ever raised for their branch(es), silently.
- **Severity:** **Medium** (data-accuracy-of-expectation defect: no financial figure is wrong, but the export does not reflect the screen it was launched from, and nothing on the page warns of that). Inventory's case is defensible (it is a live snapshot with no date axis, matching the Inventory Report tab itself), but Billing and Procurement both filter their on-page totals by date and then export unfiltered.
- **Affected:** `reports/views.py::export_csv`; `templates/reports/procurement.html`, `templates/reports/billing.html`.
- **Reproducible:** **Yes**, deterministically from the code path.

### KATRINA-RBAC-001 — Staff's default permissions block the expiry write-off the view says Staff should have

- **Role:** Staff, own branch, default permissions (no custom grants).
- **Preconditions:** An expired batch at Staff's branch (Expired tab of Alerts Center).
- **Exact steps:**
  1. As default Staff, open Alerts Center → Expired tab and submit the write-off action (`POST /inventory/alerts/write-off/<pk>/`).
  2. Trace: `inventory/views.py::expiry_write_off` is decorated `@role_required(OWNER, BRANCH_MANAGER, STAFF)` and its own docstring says: "Distinct from inventory_edit... this only ever zeroes a lot that is ALREADY expired, so it is open to Staff too as a safe operational fallback."
  3. But `core/module_permissions.py`'s registry maps this URL to `ALERTS: DELETE`, and `ROLE_DEFAULT_ACTIONS['STAFF'][ALERTS] = []` (no actions granted by default) — `accounts/middleware.py::ModulePermissionMiddleware` runs `required_action_for_request` → `DELETE`, then `user_can_perform_module_action(user, 'alerts', 'delete')` → `False` for default Staff, and the request is bounced with "You don't have Delete permission for that section."
- **Expected:** A view explicitly written to make an action available to Staff by default should actually be reachable by Staff by default, or the default permission matrix should say otherwise on purpose.
- **Actual:** Default Staff is blocked; the Owner must manually grant Alerts → Delete per account before the "safe fallback" the code describes actually works for anyone but Owner/Manager.
- **Severity:** **Medium** (workaround exists — Owner grants the permission — but out of the box, documented intended behavior doesn't match actual behavior; same class of defect as JP-RBAC-002 in the 2026-10-02 Procurement review, different module).
- **Affected:** `core/module_permissions.py::ROLE_DEFAULT_ACTIONS['STAFF'][ALERTS]`; `inventory/views.py::expiry_write_off`.
- **Reproducible:** **Yes**, deterministically from the registry + middleware.

### KATRINA-BACKUP-001 — Duplicate/concurrent "Run Backup Now" requests are not atomically prevented

- **Role:** Owner.
- **Preconditions:** No backup currently `RUNNING`.
- **Exact steps:**
  1. Double-click "Run Backup Now" (or open two tabs and submit both within the same instant), or send the external `backup_trigger` POST at the same moment as a manual click.
  2. Trace: `backups/views.py::backup_run_now` calls `service.active_run_exists()` then, if `False`, `service.spawn_backup_process(...)` (a detached `subprocess.Popen`). `backups/service.py::run_backup` (inside that subprocess) re-checks `active_run_exists()` at its own top — but both checks read `BackupRun.objects.filter(status=RUNNING).exists()` with no row lock, so two near-simultaneous requests can each see "nothing running" before either has written its first `RUNNING` row.
- **Expected:** A second rapid "Run Backup Now" either no-ops with "already running" (the documented behavior) or is otherwise serialized.
- **Actual:** No `select_for_update()`/unique-constraint/advisory lock backs either check — the guard is correct for the common case (one click, or clicks spaced more than a few hundred ms apart) but not for a true double-click or two-tab race, which can start two concurrent `run_backup()` processes.
- **Severity:** **Medium** (wasted resources/duplicate uploads and a confusing Backup Status page with two concurrent `RUNNING` rows, not data loss — each run is still internally self-consistent and `reap_stuck_runs` cleans up anything abandoned).
- **Affected:** `backups/service.py::active_run_exists`, `run_backup`; `backups/views.py::backup_run_now`, `backup_trigger`.
- **Reproducible:** Plausible from the code path (TOCTOU race); not executed live (would actually spawn backup subprocesses against the dev DB/storage).

### KATRINA-BACKUP-002 — Backup runs and restore tests are not captured in the Audit Log, and a manual run doesn't record who triggered it

- **Role:** Owner.
- **Preconditions:** None.
- **Exact steps:**
  1. Trigger a manual backup, or record a restore test.
  2. Check `audit.registry.REGISTRY` — neither `backups.BackupRun` nor `backups.RestoreTestRecord` is registered, so neither produces `AuditLog` rows.
  3. Check `BackupRun` itself — its only actor-adjacent field is `trigger` (`'manual'`/`'cron'`), not a user FK; a manual "Run Now" doesn't record *which* Owner/superuser clicked it.
- **Expected:** Per the review brief's own checklist — "whether backup actions are audited."
- **Actual:** `RestoreTestRecord.recorded_by` does capture who logged a restore test (good), but there is no record anywhere of which account started a given manual `BackupRun`, and neither model appears in the Audit Log at all.
- **Severity:** **Medium** (accountability/completeness gap on a security-adjacent surface, not an active vulnerability — only Owner/superuser can trigger a backup in the first place).
- **Affected:** `audit/registry.py::REGISTRY`; `backups/models.py::BackupRun`; `backups/views.py::backup_run_now`.
- **Reproducible:** **Yes**, by inspection — no audited model covers these two tables.

### KATRINA-AUDIT-001 — Audit Log page has no branch filter

- **Role:** Owner (the only role with access).
- **Preconditions:** Multiple branches with activity.
- **Exact steps:** Open `/monitoring/audit-log/`; compare available filters (`q`, `entity`, `action`, date range) against the model's own `branch_id` field, which is stored and displayed per row but never filterable.
- **Expected:** Per the review brief — "Audit Log: verify user, action, record, branch, timestamp" as filterable/checkable fields, matching the branch filters present on Reports, Alerts Center, and the Notification Center (Owner view).
- **Actual:** No `branch` query param exists on `audit_log_list`; an Owner triaging one branch's activity must read every row and check the Branch column by eye.
- **Severity:** **Low** (usability/completeness gap, not a security issue — Owner already sees every branch's data here by design, so nothing is exposed that shouldn't be; it is simply harder to narrow).
- **Affected:** `audit/views.py::audit_log_list`; `templates/audit/audit_log_list.html`.
- **Reproducible:** **Yes**, by inspection.

## 3. Regression test it flagged, needs triage (not yet asserted as a confirmed app defect)

### KATRINA-REG-002 — `test_withdrawal_discrepancy_corrects_stock_and_audits` fails against the current Stock Transfer code

- **Steps:** `core.tests.AcceptanceTests.test_withdrawal_discrepancy_corrects_stock_and_audits` hand-builds a `StockTransfer(status='Released')` + `StockTransferItem` directly via the ORM (bypassing the normal create→approve→release workflow), then posts to `/inventory/withdrawals/<pk>/flag/` expecting `status == 'Discrepancy'`. Actual: status stays `'Released'`.
- **Likely cause:** Two very recent commits on this branch — `b64eab7` ("Separate transfer and write-off quantity domains") and `23e3a1b` ("fix: enforce source approval for stock transfers") — both touch Stock Transfer preconditions/fields. The test's hand-built fixture likely no longer satisfies a precondition `transfer_flag_discrepancy` now enforces (e.g. a new quantity-domain or source-approval field the direct ORM `create()` doesn't set).
- **Why flagged separately from KATRINA-REG-001:** Unlike the sales regression, this review did not trace the exact line that rejects the fixture — it's reported as a confirmed-failing regression test needing a short follow-up trace, not yet a root-caused defect.
- **Severity if confirmed as a real behavior change:** High (this test also asserts a `StockMovement` "Adjustment" audit row is written — i.e., it is one of the few tests exercising the stock-transfer-discrepancy → audit-trail path end to end).
- **Affected:** `inventory/views.py::transfer_flag_discrepancy`; `core/tests.py` (lines 197–227); `inventory/models.py::StockTransfer`.
- **Reproducible:** **Yes** (see test run below).

## 4. Business-rule decisions (not asserted as definite defects)

### BR-KAT-001 — Should every "Procurement Cost" figure (Dashboard included) switch to Net of supplier returns?

- **Context:** The uncommitted work in this tree already answers this for the Procurement Report page and the Dashboard API (`DashboardSummaryAPIView`) — both now show Net. The server-rendered Dashboard (`operational_dashboard`) does not. This is BR-JP-001 from the 2026-10-02 Joshua/Procurement review, now half-landed.
- **Decision needed:** If Net is the intended definition everywhere, `_dashboard_daily_series`/`_dashboard_metrics` need the same `procurement_return_totals` deduction (and the KPI card's label/tooltip should say which it is). If Gross is intentionally kept on the Dashboard as a "committed spend" headline with Net reserved for the detailed Procurement Report, that should be stated once (e.g. in the KPI card's own copy) so Dashboard and Reports don't quietly disagree without explanation.
- **Affected:** `reports/views.py` (`_dashboard_daily_series`, `operational_dashboard`, `procurement_report`); `reports/api_views.py`.

### BR-KAT-002 — Is a receipt/reference number mandatory for every manual sale, including a first walk-in cash sale with nothing to reference?

- **Context:** KATRINA-REG-001's root cause. The commit message ("prioritize POS imports for sales entry") and the view's renamed section comment ("fallback for sales absent from the imported POS file") suggest the intended primary path is now a bulk POS import, with `/sales/add/` reserved for reconciling a sale the import missed — in which case requiring a receipt/reference to dedupe against the import makes sense.
- **Decision needed:** If a branch has no POS import workflow in use yet, or a cashier needs to record a sale with no separate receipt number (e.g. no external POS exists at that register), is manual entry supposed to be unusable for them? If yes, say so explicitly and update `core`'s acceptance tests to match (they currently assert the opposite). If no, `receipt_reference` needs to become conditional (e.g. only required when `source` indicates reconciliation against an import), and the acceptance tests stay as the regression baseline.
- **Affected:** `sales/views.py::transaction_add`; `core/tests.py`; `core/tests_feature_update.py`.

## 5. Negative-test summary (forms/actions covered by this review)

| Form/action | Blank/empty | Invalid | Duplicate | Permission | Notes |
|---|---|---|---|---|---|
| Manual sale (`/sales/add/`) | **Now always rejected** (see KATRINA-REG-001) | N/A — blocked earlier | Receipt/reference duplicate is explicitly checked (`pos_invoice_number__iexact`) under a `select_for_update()` branch lock — this part is solid | Branch access re-validated server-side for every role, not just Owner's dropdown | A deleted-branch race is explicitly handled with a friendly error instead of a 500 |
| Backup "Run Now" | N/A | N/A | **Not atomically prevented** (KATRINA-BACKUP-001) | Owner/superuser only, enforced | — |
| Restore test record | Date/backup-used/outcome all validated, future date rejected | `date.fromisoformat` failure handled | No duplicate check — intentional, it's a log | Owner/superuser only | Empty-history state handled (`restore_overdue` defaults True) |
| Audit Log filters | Empty query returns everything | Non-registry `entity`/`action` values silently ignored (fail safe, not an error) | N/A | Owner/superuser only, hard-coded (not module-gated) | No branch filter (KATRINA-AUDIT-001) |
| Alerts Center / badge | Empty-branch state handled (counts are 0, no crash) | Non-digit `branch` query param discarded, not passed to ORM | N/A | Owner/Manager/Staff view; Staff write-off blocked by default (KATRINA-RBAC-001) | Badge math diverges from page math (KATRINA-ALERT-001) |
| CSV export | N/A | N/A | N/A | Staff cannot export (role check); branch scoping fails closed (`scope_queryset`, not a truthy `if user_branch` check) | Date range honored only for `sales` (KATRINA-REPORT-002) |

## 6. Dashboard/report reconciliation mismatches found

- Dashboard "Procurement Cost" (gross) vs. Procurement Report "Net Procurement" (gross − returns) for the same branch/range — KATRINA-REPORT-001.
- Sidebar/bottom-nav Alerts badge vs. Alerts Center page vs. Dashboard's own `needs_attention` panel — KATRINA-ALERT-001.
- Procurement/Billing Report page (date-filtered) vs. their CSV exports (unfiltered) — KATRINA-REPORT-002.

No mismatch was found between the Sales Report page, its CSV export, the Dashboard's sales KPIs, or the Billing figures for a given scope — all read through the same `reports.services` definitions (`revenue_sales`, `gross_sales_total`, `refund_value_total`), which is exactly the single-source-of-truth pattern working as intended there.

## 7. Alert/audit/backup permission or consistency issues found

- KATRINA-RBAC-001 (Staff blocked from expiry write-off by default).
- KATRINA-BACKUP-002 (backup actions unaudited, manual trigger not attributed to a user).
- KATRINA-AUDIT-001 (no branch filter).
- Positive finding: the audit log's append-only guarantee is genuinely defense-in-depth — model `save()`/`delete()` overrides, a custom `QuerySet` that blocks `.update()`/`.delete()`/`bulk_update()`, a read-only Django admin, **and** MySQL `BEFORE UPDATE`/`BEFORE DELETE` triggers (`audit/triggers.py`, installed by migration `0003_append_only_triggers.py`). The migration fails open with a loud stderr warning (not a blocked deploy) if the DB user lacks `TRIGGER` privilege — worth a one-time check that this actually installed on the production MySQL instance, since a missing trigger layer would not be visible from the app itself.

## 8. Existing automated tests — passed/failed

Command run:

```text
venv\Scripts\python.exe manage.py test audit backups reports core --keepdb -v 1
```

Result: **396 tests, 11 failures, 0 errors, 1 skipped** (the skip is the MySQL-trigger test, correctly self-skipping on a non-MySQL test backend), ~225s.

- **`audit`, `backups`, `reports` test modules: all passing.** Every failure is in `core.tests.AcceptanceTests` or `core.tests_feature_update.FeatureUpdateTests` (sales/pricing/discount/stock-transfer scenarios), not in the monitoring apps' own suites.
- Failing: `test_a_posted_order_type_cannot_buy_a_tier_price`, `test_a_product_with_no_tiers_always_pays_retail`, `test_a_quantity_below_the_first_tier_pays_retail`, `test_reaching_a_tier_pays_the_tier_price`, `test_two_rows_of_one_product_earn_the_same_break_as_one_row`, `test_walk_in_pos_deducts_stock_and_records_payment` — all KATRINA-REG-001.
- Failing: `test_a_senior_discount_is_applied_and_recorded`, `test_a_senior_only_product_is_not_discounted_under_a_pwd_claim`, `test_an_ineligible_product_stays_at_full_price_under_a_claim`, `test_an_undiscounted_sale_bills_exactly_as_before` — all KATRINA-REG-001 (same root cause, PWD/Senior discount path uses the same view).
- Failing: `test_withdrawal_discrepancy_corrects_stock_and_audits` — KATRINA-REG-002, needs a short follow-up trace.
- All other `core` acceptance tests (branch scoping, CSV export branch-scoping, staff export restriction, price-over-1000 editing, template hygiene, etc.) **pass**.

## 9. Katrina-scope scenarios with no automated coverage today

- `core.context_processors.alerts()` (the sidebar/bottom-nav badge count) — zero references anywhere outside its own definition.
- Alerts Center branch-scoping/cross-branch-leakage (the Low Stock test fixture in `inventory/tests.py` only covers one branch; no test asserts a Manager/Staff at Branch A cannot see Branch B's alert counts).
- `backup_run_now` double-submit/race behavior (no test spawns two concurrent calls).
- Whether backup/restore-test actions appear in — or are correctly absent from — the Audit Log.
- Audit Log branch-filter behavior (none exists to test).
- Dashboard vs. Procurement Report Net/Gross parity (no test asserts the two agree).
- Procurement/Billing CSV export vs. the report page's active date filter.

## 10. Scope audit

No online-payment-gateway processing, courier/logistics integration, HR/payroll, or external-enterprise-system integration was found anywhere in the Dashboard/Reports/Alerts/Audit/Backups surfaces reviewed. Backblaze B2 is used purely as encrypted off-site backup storage, not a third-party business-process integration, and is not flagged as scope creep. Backup/restore functionality is a defensible "system control & monitoring" feature under the BPR framework the capstone is evaluated against, not an addition outside the declared Sales/Inventory/Procurement/Billing/Reporting scope.

## 11. Recommended remediation order

1. **Resolve BR-KAT-002, then fix KATRINA-REG-001.** This is blocking the project's own ISO 25010 acceptance-test baseline today; everything else here is secondary until this is green again.
2. Triage and fix or re-baseline KATRINA-REG-002 (stock-transfer discrepancy test).
3. Resolve BR-KAT-001, then make the Dashboard's Procurement Cost KPI consistent with the Procurement Report (KATRINA-REPORT-001).
4. Either filter Procurement/Billing CSV export by the active date range, or label the export as "full history" on those two tabs (KATRINA-REPORT-002).
5. Grant Staff `ALERTS: [DELETE]` by default, or change the view/docstring to match actual intended access (KATRINA-RBAC-001).
6. Add a branch filter to the Audit Log (KATRINA-AUDIT-001); add `backups.BackupRun`/`RestoreTestRecord` to the audit registry and an actor field on `BackupRun` for manual triggers (KATRINA-BACKUP-002); add a lock/guard against concurrent backup runs (KATRINA-BACKUP-001).
7. Decide whether the sidebar Alerts badge should count everything Alerts Center shows, or stay floor-low-stock-only with that scope stated in the UI (KATRINA-ALERT-001).
