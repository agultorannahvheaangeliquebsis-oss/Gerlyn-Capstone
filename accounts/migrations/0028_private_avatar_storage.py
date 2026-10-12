from django.db import migrations, models

import accounts.models
import core.storage


class Migration(migrations.Migration):
    dependencies = [('accounts', '0027_supplier_business_registration_id')]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='avatar',
            field=models.ImageField(
                blank=True,
                help_text='Profile picture. JPEG/PNG/WEBP, max 2 MB.',
                null=True,
                storage=core.storage.PrivateMediaStorage(),
                upload_to=accounts.models.avatar_upload_path,
            ),
        ),
    ]
