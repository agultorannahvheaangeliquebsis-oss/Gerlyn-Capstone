from django.db import migrations, models
import django.db.models.deletion
import django.db.models.expressions


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0015_remove_product_is_pwd_senior_eligible_and_more'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='batch',
            options={'ordering': [django.db.models.expressions.OrderBy(django.db.models.expressions.F('expiry_date'), nulls_last=True)], 'verbose_name_plural': 'Batches'},
        ),
        migrations.AddField(
            model_name='batch',
            name='location',
            field=models.CharField(choices=[('Warehouse', 'Warehouse'), ('Floor', 'Floor')], default='Warehouse', max_length=10, help_text="Which stock this batch is part of — Warehouse (backroom) or Floor (shelf). A restock moves quantity from a Warehouse batch into a new or matching Floor batch; it never edits the Warehouse batch's location in place."),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='batch',
            name='source_batch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='descendant_batches', to='inventory.batch'),
        ),
        migrations.AlterField(
            model_name='batch',
            name='expiry_date',
            field=models.DateField(blank=True, null=True, help_text="Expiry date printed on the product lot. Blank means not tracked (e.g. a manual stock adjustment with no expiry entered) — FEFO treats it as expiring last, never first, so an unknown date can't jump the queue ahead of stock that IS known to be expiring soon."),
        ),
    ]
