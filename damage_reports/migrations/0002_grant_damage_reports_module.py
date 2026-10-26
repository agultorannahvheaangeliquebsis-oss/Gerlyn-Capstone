"""Give existing Manager/Staff accounts the new Damage Reports module.

Accounts created from now on get it from core.module_permissions' role
defaults (accounts.signals). Accounts that already exist store their grants as
plain lists, so without this the module would be closed to every one of them.
The grant is the role default and nothing more: a Branch Manager can file and
decide reports, Staff can file them. Deciding is a Manager/Owner job regardless
(the views check the role), so no Staff account gets the Edit action.

Values are written out here rather than imported, so later changes to the role
defaults cannot rewrite what this migration did.
"""
from django.db import migrations

CODE = 'damage_reports'
DEFAULT_ACTIONS = {
    'BRANCH_MANAGER': ['add', 'edit'],
    'STAFF': ['add'],
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
        ('damage_reports', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
