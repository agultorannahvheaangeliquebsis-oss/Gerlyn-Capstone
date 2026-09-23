"""Convert the whole database, and every existing table, to utf8mb4.

Found while chasing a live bug: cancelling a completed order (or anything
else that ever calls core.notifications.notify with a peso amount in the
text) raised DataError, because accounts_notification was latin1_swedish_ci
— which cannot hold '₱' (U+20B1 PESO SIGN) or any other character
outside Latin-1. That turned out not to be a notifications-specific bug:
the database's own default character set was latin1, so EVERY table Django
has ever created here inherited it (Django's CREATE TABLE does not specify
a charset itself, only the connection encoding — storage follows the
database default). 51 of 52 tables were latin1 before this migration; only
audit_auditlog was already utf8mb4 (audit/migrations/0002_utf8mb4.py, fixed
early because that table was built to hold arbitrary free text on purpose).

Two steps, both idempotent and safe to re-run:
  1. ALTER DATABASE ... CHARACTER SET utf8mb4 — so every table any migration
     creates from here on (in any app, including ones that don't exist yet)
     inherits the right default automatically, the way it should have from
     the start, instead of needing its own explicit fix like audit's did.
  2. CONVERT TO CHARACTER SET utf8mb4 on every table that is not already
     utf8mb4 — existing latin1 data is a strict subset of utf8mb4 (every
     byte sequence valid in Latin-1 is valid, unchanged, in utf8mb4), so
     this never loses or corrupts anything already stored; it only makes
     room for what could not be stored before.

MySQL/MariaDB only (mirrors audit/migrations/0002_utf8mb4.py's guard) — a
no-op on any other backend, and on a database that is already all-utf8mb4
(a fresh install created after this migration exists).
"""
from django.db import migrations


def to_utf8mb4(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'mysql':
        return
    db_name = connection.settings_dict['NAME']
    with connection.cursor() as cursor:
        cursor.execute(
            f'ALTER DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'
        )
        cursor.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_COLLATION NOT LIKE 'utf8mb4%%'",
            [db_name],
        )
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            cursor.execute(
                f'ALTER TABLE `{table}` CONVERT TO CHARACTER SET utf8mb4 '
                f'COLLATE utf8mb4_unicode_ci'
            )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_storesetting_receipt_letterhead'),
    ]

    operations = [
        migrations.RunPython(to_utf8mb4, migrations.RunPython.noop),
    ]
