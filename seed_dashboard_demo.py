# seed_dashboard_demo.py
#
# One-off seed script for eyeballing the new dashboard sparklines/deltas
# against real (non-empty) data. Run via:
#
#   venv/Scripts/python.exe manage.py shell -c "exec(open('seed_dashboard_demo.py').read())"
#
# Uses whatever Branch / STAFF user / Product / SUPPLIER user / OWNER user
# already exist in the DB — creates none of those, only SalesTransaction +
# SalesItem + Procurement + ProcurementItem rows, all tagged with the
# 'DEMO-SEED' marker below so they're trivial to find and delete afterward.
#
# Cleanup, when you're done looking:
#   from sales.models import SalesTransaction
#   from procurement.models import Procurement
#   SalesTransaction.objects.filter(order_number__startswith='ORD-DEMO').delete()
#   Procurement.objects.filter(po_number__startswith='PO-DEMO').delete()

import random
from datetime import timedelta, datetime, time
from decimal import Decimal

from django.utils import timezone
from django.db import connection

from branches.models import Branch
from accounts.models import User
from billing.models import BillingRecord
from inventory.models import Product
from sales.models import SalesTransaction, SalesItem
from procurement.models import Procurement, ProcurementItem

MARKER = 'DEMO-SEED'

branch = Branch.objects.first()
staff = User.objects.filter(role='STAFF').first()
supplier = User.objects.filter(role='SUPPLIER').first()
owner = User.objects.filter(role='OWNER').first()
products = list(Product.objects.all()[:5])

if not branch:
    raise SystemExit('No Branch found — create at least one branch first, then re-run.')
if not products:
    raise SystemExit('No Product found — create at least one product first, then re-run.')
if not staff:
    print('WARNING: no STAFF user found — sales rows will have staff=None (allowed).')
if not supplier:
    print('WARNING: no SUPPLIER user found — procurement rows will have supplier=None (allowed).')

print(f'Using branch={branch}, staff={staff}, supplier={supplier}, owner={owner}, products={[p.product_name for p in products]}')

today = timezone.localtime().date()

created_sales = 0
created_proc = 0

# --- Sales: spread across the last 20 days so both the 7-day and 30-day ---
# sparklines/deltas have real, uneven data to render (some zero days too,
# so the flat-fallback path and the real-curve path both still get exercised).
for days_ago in range(20, -1, -1):
    day = today - timedelta(days=days_ago)
    # Skip some days entirely (uneven series, more realistic than 1/day)
    if random.random() < 0.25:
        continue
    num_orders = random.randint(1, 3)
    for _ in range(num_orders):
        channel = random.choice(['Online', 'Physical'])
        payment = random.choice(['Cash', 'GCash', 'Card'])

        # Pick the line items and total them BEFORE creating the transaction.
        # billing.signals stamps BillingRecord.amount_due from
        # instance.total_amount at post_save(created=True) — i.e. the moment
        # the row is INSERTed — and never re-syncs. Creating with
        # total_amount=0 and patching it afterwards (especially via a queryset
        # .update(), which emits no signals at all) leaves every billing record
        # at ₱0.00 forever. sales/serializers.py builds the total up front for
        # exactly this reason; do the same here.
        chosen = [
            (product, random.randint(1, 5))
            for product in random.sample(products, k=min(len(products), random.randint(1, 2)))
        ]
        total = sum(qty * product.unit_price for product, qty in chosen)

        txn = SalesTransaction(
            branch=branch,
            staff=staff,
            customer=None,
            total_amount=total,          # complete before the signal sees it
            transaction_type=channel,
            mode_of_payment=payment,
            order_status='Completed',
        )
        txn.save()  # auto_now_add sets transaction_date = now(); fixed below
        txn.order_number = f'ORD-DEMO-{txn.pk:04d}'

        for product, qty in chosen:
            SalesItem.objects.create(
                transaction=txn, product=product,
                quantity_sold=qty, unit_price=product.unit_price,
            )

        # Backdate transaction_date (auto_now_add ignores assigned values on
        # save(), so this has to go through a queryset .update() instead).
        fake_dt = timezone.make_aware(
            datetime.combine(day, time(hour=random.randint(8, 19), minute=random.randint(0, 59)))
        )
        SalesTransaction.objects.filter(pk=txn.pk).update(
            transaction_date=fake_dt, order_number=txn.order_number
        )

        # Settle the auto-created billing record the way the real flows do, so
        # the Billing Report shows a believable Collected vs Pending split
        # instead of 100% Pending:
        #   in-store        -> money changed hands at the counter  -> Paid
        #   online prepaid  -> GCash/Card settles at checkout      -> Paid
        #   online cash     -> Cash on Delivery, not yet collected -> Pending
        billing = txn.billing_record
        if channel == 'Physical' or payment in ('GCash', 'Card'):
            billing.status = BillingRecord.PaymentStatus.PAID
            billing.confirmed_by = owner or staff
            billing.date_confirmed = fake_dt
        else:
            billing.status = BillingRecord.PaymentStatus.PENDING
        billing.payment_method = payment
        billing.save(update_fields=['status', 'confirmed_by', 'date_confirmed', 'payment_method'])
        BillingRecord.objects.filter(pk=billing.pk).update(date_recorded=fake_dt)

        created_sales += 1

# --- Procurement: spread across the last 14 days (order_date is a plain ---
# DateField, no auto_now_add quirk, so it can be set directly).
for days_ago in range(14, -1, -1):
    day = today - timedelta(days=days_ago)
    if random.random() < 0.4:
        continue
    proc = Procurement(
        branch=branch,
        supplier=supplier,
        owner=owner,
        order_date=day,
        delivery_date=day + timedelta(days=random.randint(1, 5)),
        total_cost=0,
        status=random.choice(['Incoming', 'Received']),
    )
    proc.save()
    proc.po_number = f'PO-DEMO-{proc.pk:04d}'

    total = 0
    for product in random.sample(products, k=min(len(products), random.randint(1, 2))):
        qty = random.randint(5, 20)
        # Decimal('0.7'), not 0.7 — prices are Decimal and `Decimal * float`
        # raises TypeError, which aborted this script before it finished.
        cost = product.bulk_floor_price * Decimal('0.7')
        ProcurementItem.objects.create(
            procurement=proc, product=product,
            quantity_ordered=qty, quantity_received=qty, cost_price=cost,
        )
        total += qty * cost
    proc.total_cost = total
    proc.save(update_fields=['po_number', 'total_cost'])
    created_proc += 1

print(f'OK — created {created_sales} SalesTransaction rows and {created_proc} Procurement rows.')
print('To remove them later:')
print("  SalesTransaction.objects.filter(order_number__startswith='ORD-DEMO').delete()")
print("  Procurement.objects.filter(po_number__startswith='PO-DEMO').delete()")
