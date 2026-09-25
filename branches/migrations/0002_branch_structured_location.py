from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('branches', '0001_initial')]

    operations = [
        migrations.AddField(model_name='branch', name='region', field=models.CharField(blank=True, max_length=10)),
        migrations.AddField(model_name='branch', name='province', field=models.CharField(blank=True, max_length=10)),
        migrations.AddField(model_name='branch', name='city', field=models.CharField(blank=True, max_length=10)),
        migrations.AddField(model_name='branch', name='barangay', field=models.CharField(blank=True, max_length=15)),
        migrations.AddField(model_name='branch', name='street_detail', field=models.CharField(blank=True, max_length=255)),
        migrations.AddField(model_name='branch', name='latitude', field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True)),
        migrations.AddField(model_name='branch', name='longitude', field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True)),
    ]
