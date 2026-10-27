"""Give existing products the cost price the Profitability report needs.

Product.cost_price is new. Rather than leave every product "unknown" until
someone retypes the catalogue, take what the system already knows:

  1. the supplier price on the most recent purchase-order line that actually
     delivered stock (ProcurementItem.unit_cost_snapshot, quantity_received > 0);
  2. otherwise the preferred (then cheapest) approved supplier offer.

A product with neither stays blank and shows up in the report's "no cost price"
note, which is the honest answer. Only blanks are filled, so re-running it, or
running it after someone has typed a price in, changes nothing they set.

Not reversible in a meaningful way (the column is dropped by the schema
migration it follows), so the reverse is a no-op.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Product = apps.get_model('inventory', 'Product')
    ProcurementItem = apps.get_model('procurement', 'ProcurementItem')
    SupplierProduct = apps.get_model('procurement', 'SupplierProduct')

    for product in Product.objects.filter(cost_price__isnull=True).iterator(chunk_size=500):
        cost = (
            ProcurementItem.objects
            .filter(
                product_id=product.pk, quantity_received__gt=0,
                unit_cost_snapshot__isnull=False, unit_cost_snapshot__gt=0,
            )
            .order_by('-procurement__order_date', '-procurement_id', '-pk')
            .values_list('unit_cost_snapshot', flat=True)
            .first()
        )
        if cost is None:
            cost = (
                SupplierProduct.objects
                .filter(
                    product_id=product.pk, status='Approved',
                    current_cost_price__isnull=False, current_cost_price__gt=0,
                )
                .order_by('-is_preferred', 'current_cost_price', 'pk')
                .values_list('current_cost_price', flat=True)
                .first()
            )
        if cost is not None:
            Product.objects.filter(pk=product.pk).update(cost_price=cost)


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0036_product_cost_price'),
        ('procurement', '0018_enforce_input_limits'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
