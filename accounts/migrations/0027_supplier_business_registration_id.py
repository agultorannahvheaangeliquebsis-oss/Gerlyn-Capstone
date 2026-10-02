from django.db import migrations, models


def align_manager_procurement_permissions(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    for user in User.objects.filter(role='BRANCH_MANAGER'):
        actions = dict(user.module_actions or {})
        actions['procurement'] = ['add', 'edit', 'delete']
        actions['deliveries'] = ['add', 'edit']
        user.module_actions = actions
        user.save(update_fields=['module_actions'])


class Migration(migrations.Migration):
    dependencies = [('accounts', '0026_customerprofile_terms_consent')]

    operations = [
        migrations.AddField(
            model_name='supplierprofile',
            name='business_registration_id',
            field=models.CharField(
                blank=True, null=True, unique=True, max_length=100,
                help_text='Official business or tax registration identifier. Blank when not provided.',
            ),
        ),
        migrations.RunPython(align_manager_procurement_permissions, migrations.RunPython.noop),
    ]
