from django.db import migrations, models

import core.module_permissions


def backfill_module_actions(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    actions = core.module_permissions.default_module_actions()
    User.objects.all().update(module_actions=actions)


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0024_supplier_category_fk'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='allowed_modules',
            field=models.JSONField(
                blank=True,
                default=core.module_permissions.default_allowed_modules,
                help_text=(
                    "View permission for internal modules. Owner is exempt and "
                    "always sees everything."
                ),
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='module_actions',
            field=models.JSONField(
                blank=True,
                default=core.module_permissions.default_module_actions,
                help_text=(
                    "Per-module Add/Edit/Delete permissions. Every action also "
                    "requires the module's View permission."
                ),
            ),
        ),
        migrations.RunPython(backfill_module_actions, migrations.RunPython.noop),
    ]
