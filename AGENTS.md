# AGENTS.md

## Scope

These instructions apply to the entire repository. Keep changes focused on the requested task and preserve unrelated user work.

## Project overview

GerlynPLUS is a Django 6 and MySQL information system for a multi-branch retail business. It uses server-rendered Django templates for the main UI and retains a same-origin Django REST Framework API under `/api/`.

Key applications:

- `core`: shared permissions, module access, branch scoping, notifications, and cross-app helpers
- `accounts`: custom user model, authentication, roles, profiles, and sessions
- `branches`: branch records and branch management
- `inventory`: products, categories, stock, batches, transfers, and stock movements
- `sales`: POS transactions and the internal order console
- `orders`: customer storefront, cart, and checkout
- `billing`: billing records and payment verification
- `procurement`: suppliers, purchase orders, deliveries, and receiving
- `reports`: dashboards, reports, and exports
- `templates` and `static`: shared HTML, CSS, and JavaScript

## Setup and common commands

- Use Python 3.13 and the repository virtual environment when available.
- Install dependencies with `python -m pip install -r requirements.txt`.
- Copy `.env.example` to `.env` and provide a local MySQL database and credentials. Never commit `.env` or real secrets.
- Apply schema changes with `python manage.py migrate`.
- Start locally with `python manage.py runserver`.
- Rebuild demo data only when explicitly intended: `python manage.py seed_demo --reset` is destructive to local application data.

On Windows, prefer `venv\Scripts\python.exe manage.py ...` for the checked-in local environment. On Unix-like systems, `./run.sh` provides `start`, `fresh`, `stop`, `test`, and `test-all` helpers.

## Validation

Run the narrowest relevant tests while developing, then broaden coverage in proportion to the change.

- Single app: `python manage.py test <app>`
- Single test module or class: `python manage.py test app.tests_module.TestClass`
- Fast acceptance suite: `python manage.py test core`
- Full suite: `python manage.py test accounts core sales orders billing inventory procurement reports`
- Configuration check: `python manage.py check`
- Migration drift check after model edits: `python manage.py makemigrations --check --dry-run`

Add or update regression tests for behavior changes. Keep tests deterministic and avoid dependence on pre-existing demo data or external services.

## Architecture and business invariants

- Enforce authorization and branch scoping on the server. UI visibility, query parameters, and branch selectors are not security boundaries. Reuse `core.permissions`, `core.module_permissions`, and `core.scoping` instead of duplicating their logic.
- Managers and staff are restricted to their assigned branch; owners may access all branches. Unassigned restricted users must fail closed.
- Preserve stock exactly-once behavior: walk-in sales deduct at sale time, online orders deduct on completion, and deliveries add stock only after validated receipt. Record every stock change as a `StockMovement` with actor, reason, and before/after quantities.
- Keep template views and DRF endpoints aligned on permissions, scoping, validation, state transitions, and pricing rules.
- Treat financial and inventory transitions as atomic operations. Use `transaction.atomic()` and row locking where concurrent requests could double-process an action.
- Payments are recorded and verified, not processed through a payment gateway.
- Maintain the custom `accounts.User` model; do not introduce direct dependencies on Django's default user model.

## Implementation conventions

- Follow the existing app boundaries and local coding style. Prefer shared helpers or service functions when a rule is used by multiple views or APIs.
- Keep views thin where practical; put pricing, inventory, transition, and other domain rules in the existing service/helper modules.
- Use Django ORM parameterization and forms/serializers for validation. Do not trust client-supplied branch IDs, totals, prices, roles, or statuses.
- Use `Decimal` for money and follow the project's existing rounding behavior.
- For model changes, create a new migration; do not rewrite applied migrations unless the task explicitly requires it.
- Preserve server-rendered templates and the current offline-first asset approach. Do not add a Node build step, CDN asset, external font, or third-party browser dependency without explicit approval.
- Reuse shared template partials and the existing CSS design system before adding duplicate markup or styles.
- Keep user-facing strings and status labels consistent with existing terminology.

## Files and data to avoid

- Do not edit generated or local-runtime directories: `venv/`, `.venv/`, `node_modules/`, `.vite/`, `dist/`, `staticfiles/`, `media/`, or `tmp_uploads/`.
- Do not commit database dumps, uploaded files, logs, caches, credentials, or machine-specific configuration.
- Do not modify or delete local `.env` values. When adding configuration, document safe placeholders in `.env.example`.
- Treat `seed_demo --reset`, bulk imports, hard deletes, and migration rollbacks as destructive; run them only when the task calls for them.

## Documentation and handoff

Update `README.md` or other relevant documentation when setup, configuration, commands, or user-visible workflows change. In the final handoff, summarize changed files and report the exact validation commands run, including any tests that could not be run and why.
