from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0025_user_module_actions'),
    ]

    operations = [
        migrations.AddField(
            model_name='customerprofile',
            name='terms_accepted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='customerprofile',
            name='terms_version',
            field=models.CharField(blank=True, max_length=20),
        ),
    ]
