"""Database dump and restore, using the standard mysqldump / mysql tools.

The flags are the subset that works on both MySQL and MariaDB (local
development runs MariaDB, production runs MySQL). The password is handed over
in the environment (MYSQL_PWD), never on the command line, where any other
process on the machine could read it.
"""
import gzip
import os
import shlex
import shutil
import subprocess
import tempfile

from django.conf import settings


class BackupToolError(Exception):
    """mysqldump / mysql is missing, or failed."""


def _tool(configured, name):
    """The argv prefix for a configured tool (a path, or a command name)."""
    configured = (configured or name).strip().strip('"')
    if os.path.isfile(configured):
        # A plain path, spaces and all (C:\Program Files\...\mysqldump.exe).
        return [configured]
    parts = shlex.split(configured, posix=(os.name != 'nt'))
    parts = [p.strip('"') for p in parts] or [name]
    resolved = shutil.which(parts[0]) or (parts[0] if os.path.isfile(parts[0]) else None)
    if not resolved:
        raise BackupToolError(
            f'{name} was not found. Install the MySQL client tools, or set '
            f'{"BACKUP_MYSQLDUMP" if name == "mysqldump" else "BACKUP_MYSQL"} '
            f'to its full path.'
        )
    return [resolved] + parts[1:]


def tool_version(configured, name):
    """First line of `<tool> --version`, for the setup check."""
    argv = _tool(configured, name)
    out = subprocess.run(argv + ['--version'], capture_output=True, text=True, timeout=20)
    return (out.stdout or out.stderr).strip().splitlines()[0]


def _supports(argv, flag):
    try:
        out = subprocess.run(argv + ['--help'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return flag in (out.stdout + out.stderr)


def _db():
    return settings.DATABASES['default']


def _connection_args():
    db = _db()
    args = []
    if db.get('HOST'):
        args.append(f'--host={db["HOST"]}')
    if db.get('PORT'):
        args.append(f'--port={db["PORT"]}')
    args.append(f'--user={db["USER"]}')
    return args


def _env():
    return {**os.environ, 'MYSQL_PWD': str(_db().get('PASSWORD') or '')}


def dump_command():
    argv = _tool(settings.BACKUP_MYSQLDUMP, 'mysqldump')
    args = argv + [
        '--single-transaction', '--routines', '--triggers', '--hex-blob',
        '--default-character-set=utf8mb4',
    ]
    # MySQL 8 needs this for a non-admin user; MariaDB's dump tool has no such flag.
    if _supports(argv, '--no-tablespaces'):
        args.append('--no-tablespaces')
    return args + _connection_args() + [_db()['NAME']]


def dump_database(destination_gz):
    """Write a consistent, gzip-compressed SQL dump of the whole database."""
    args = dump_command()
    tail = b''
    with tempfile.TemporaryFile() as errors, gzip.open(destination_gz, 'wb') as out:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors, env=_env())
        for chunk in iter(lambda: proc.stdout.read(1024 * 1024), b''):
            out.write(chunk)
            tail = (tail + chunk)[-512:]
        code = proc.wait()
        if code != 0:
            errors.seek(0)
            raise BackupToolError(
                f'mysqldump failed (exit {code}): '
                f'{errors.read().decode("utf-8", "replace").strip()[:500]}'
            )
    # mysqldump ends a complete dump with this marker; without it the output
    # was cut short even though the tool exited cleanly.
    if b'Dump completed' not in tail:
        raise BackupToolError('The database dump looks incomplete (no completion marker).')


def restore_sql(sql_path, database):
    """Load a plain SQL dump into `database` with the mysql client."""
    argv = _tool(settings.BACKUP_MYSQL, 'mysql') + _connection_args() + [
        '--default-character-set=utf8mb4', database,
    ]
    with open(sql_path, 'rb') as source, tempfile.TemporaryFile() as errors:
        result = subprocess.run(argv, stdin=source, stderr=errors, env=_env())
        if result.returncode != 0:
            errors.seek(0)
            raise BackupToolError(
                f'mysql restore failed (exit {result.returncode}): '
                f'{errors.read().decode("utf-8", "replace").strip()[:500]}'
            )


# --- row counts and scratch databases ----------------------------------------

def table_counts(cursor):
    """{table: exact row count} for every base table visible to `cursor`."""
    cursor.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
    names = [row[0] for row in cursor.fetchall()]
    counts = {}
    for name in names:
        if '`' in name:
            continue
        cursor.execute(f'SELECT COUNT(*) FROM `{name}`')
        counts[name] = cursor.fetchone()[0]
    return counts


def _admin_connection(database=None):
    import pymysql

    db = _db()
    return pymysql.connect(
        host=db.get('HOST') or 'localhost', port=int(db.get('PORT') or 3306),
        user=db['USER'], password=db.get('PASSWORD') or '', database=database,
        charset='utf8mb4', autocommit=True,
    )


def database_exists(name):
    conn = _admin_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT 1 FROM information_schema.schemata WHERE schema_name = %s', (name,))
            return cursor.fetchone() is not None
    finally:
        conn.close()


def create_database(name):
    if '`' in name:
        raise BackupToolError('Invalid database name.')
    conn = _admin_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    finally:
        conn.close()


def drop_database(name):
    if '`' in name:
        raise BackupToolError('Invalid database name.')
    conn = _admin_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS `{name}`')
    finally:
        conn.close()


def counts_for_database(name):
    conn = _admin_connection(name)
    try:
        with conn.cursor() as cursor:
            return table_counts(cursor)
    finally:
        conn.close()
