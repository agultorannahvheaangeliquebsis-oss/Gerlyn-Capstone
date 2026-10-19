"""Give existing Manager/Staff accounts the new Stock Counts module.

Accounts created from now on get it from core.module_permissions' role
defaults (accounts.signals). Accounts that already exist store their grants
as plain lists, so without this the new module would be closed to every one
of them. The grant is the role default and nothing more. Counting a location
also needs View on that location's own module (Floor/Warehouse Stock), so an
Owner's earlier narrowing of those modules still holds.

Values are written out here rather than imported, so later changes to the
role defaults cannot rewrite what this migration did.
"""
from django.db import migrations

CODE = 'stock_count'
DEFAULT_ACTIONS = {
    'BRANCH_MANAGER': ['add', 'edit', 'delete'],
    'STAFF': ['add', 'delete'],
}


def grant(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    for user in User.objects.filter(role__in=list(DEFAULT_ACTIONS)):
        modules = list(user.allowed_modules or [])
        actions = dict(user.module_actions) if isinstance(user.module_actions, dict) else {}
        changed = False
        if CODE not in modules:
            modules.append(CODE)
            changed = True
        if CODE not in actions:
            actions[CODE] = list(DEFAULT_ACTIONS[user.role])
            changed = True
        if changed:
            user.allowed_modules = modules
            user.module_actions = actions
            user.save(update_fields=['allowed_modules', 'module_actions'])


def revoke(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    for user in User.objects.filter(role__in=list(DEFAULT_ACTIONS)):
        modules = [m for m in (user.allowed_modules or []) if m != CODE]
        actions = dict(user.module_actions) if isinstance(user.module_actions, dict) else {}
        actions.pop(CODE, None)
        user.allowed_modules = modules
        user.module_actions = actions
        user.save(update_fields=['allowed_modules', 'module_actions'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0028_private_avatar_storage'),
        ('inventory', '0033_stock_count_sessions'),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
