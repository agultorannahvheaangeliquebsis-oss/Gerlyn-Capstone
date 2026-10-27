"""Give existing Branch Managers the new Expenses module.

Accounts created from now on get it from core.module_permissions' role
defaults (accounts.signals). Accounts that already exist store their grants as
plain lists, so without this the module would be closed to every one of them.
The grant is the role default and nothing more: a Branch Manager can record,
edit and delete expenses for their own branch. Staff get nothing -- the module
is not theirs, and the views and middleware refuse the role regardless of any
grant -- so no Staff account is touched. The Owner needs no grant.

Values are written out here rather than imported, so later changes to the role
defaults cannot rewrite what this migration did.
"""
from django.db import migrations

CODE = 'expenses'
DEFAULT_ACTIONS = {
    'BRANCH_MANAGER': ['add', 'edit', 'delete'],
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
        ('accounts', '0030_notification_event_damage_reports'),
        ('expenses', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
