from django.db import migrations

from core.module_permissions import DEFAULT_STAFF_MODULES


def backfill_staff_defaults(apps, schema_editor):
    """The previous migration's AddField gave every existing row the field
    default (every module) — correct for Branch Manager, who already had
    full access, but wrong for Staff: it would silently hand every existing
    Staff account access to sections (Product Master List, Withdrawals,
    Alerts, Billing, Procurement, Reports) that role never permitted before
    this feature existed. This narrows existing Staff rows back to exactly
    what Staff could already reach — the same thing accounts/signals.py now
    does for any Staff account created from here on."""
    User = apps.get_model('accounts', 'User')
    User.objects.filter(role='STAFF').update(allowed_modules=DEFAULT_STAFF_MODULES)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0015_user_allowed_modules'),
    ]

    operations = [
        migrations.RunPython(backfill_staff_defaults, noop_reverse),
    ]
