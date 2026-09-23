"""MySQL triggers that make the audit table refuse UPDATE and DELETE.

The model, queryset and admin already block edits, but only for code that goes
through them. These make the database itself say no, including to a raw SQL
session or a future bug. Kept in one place so the migration and the test that
proves them share the same statements.
"""
TABLE = 'audit_auditlog'
TRIGGERS = {
    'audit_auditlog_no_update': 'UPDATE',
    'audit_auditlog_no_delete': 'DELETE',
}


def install(cursor):
    for name, event in TRIGGERS.items():
        cursor.execute(f'DROP TRIGGER IF EXISTS {name}')
        cursor.execute(
            f'CREATE TRIGGER {name} BEFORE {event} ON {TABLE} FOR EACH ROW '
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'The audit log is append-only.'"
        )


def uninstall(cursor):
    for name in TRIGGERS:
        cursor.execute(f'DROP TRIGGER IF EXISTS {name}')
