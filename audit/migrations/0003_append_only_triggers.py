import sys

from django.conf import settings
from django.db import OperationalError, migrations

from audit import triggers


def install_triggers(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'mysql' or not getattr(settings, 'AUDIT_DB_TRIGGERS', True):
        return
    try:
        with connection.cursor() as cursor:
            triggers.install(cursor)
    except OperationalError as exc:
        # Creating triggers needs the TRIGGER privilege (and, with binary
        # logging on, SUPER or log_bin_trust_function_creators). Failing the
        # whole migration would take a deploy down over a defence-in-depth
        # layer, so warn loudly and carry on: the model, queryset and admin
        # still block edits. Re-run `migrate audit 0002` then `migrate audit`
        # after fixing the privilege to install them.
        sys.stderr.write(
            '\n*** WARNING: could not install the audit-log append-only triggers '
            f'({exc}). The log is still protected at the application level, but '
            'not by the database itself. ***\n'
        )


def remove_triggers(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'mysql':
        return
    with connection.cursor() as cursor:
        triggers.uninstall(cursor)


class Migration(migrations.Migration):
    dependencies = [('audit', '0002_utf8mb4')]
    operations = [migrations.RunPython(install_triggers, remove_triggers)]
