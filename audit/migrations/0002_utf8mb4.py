from django.db import migrations


def to_utf8mb4(apps, schema_editor):
    """The log records values people typed (product names, notes) and must never
    silently drop an entry because the database's default charset can't hold a
    character — a log that loses rows is worse than none. So the table is made
    utf8mb4 explicitly rather than inheriting whatever the server defaults to."""
    if schema_editor.connection.vendor != 'mysql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            'ALTER TABLE audit_auditlog CONVERT TO CHARACTER SET utf8mb4 '
            'COLLATE utf8mb4_unicode_ci'
        )


class Migration(migrations.Migration):
    dependencies = [('audit', '0001_initial')]
    operations = [migrations.RunPython(to_utf8mb4, migrations.RunPython.noop)]
