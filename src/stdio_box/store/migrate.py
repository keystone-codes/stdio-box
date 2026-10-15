"""Forward-only SQL migrations.

A database newer than this process raises StoreSchemaNewer and is not
modified. The wrapper maps that to HTTP 503 and the log line
`store_schema_newer`.
"""

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from stdio_box.store.errors import StoreSchemaNewer

logger = logging.getLogger("stdio_box.store")

_MIGRATION_NAME = re.compile(r"^(\d{4})_.*\.sql$")
_BACKUP_NAME = re.compile(r"^box\.sqlite3\.(\d+)-(\d+)\.(\d{8}T\d{6}Z)$")
_BACKUPS_KEPT = 3


def migrations_dir() -> Path:
    return Path(__file__).resolve().parent / "migrations"


def migrate(
    conn: sqlite3.Connection,
    data_dir: Path | str,
    now: datetime,
    migrations: Path | None = None,
) -> int:
    """Apply pending `NNNN_*.sql` files in order. Return the schema version.

    There is no `meta.schema_version` until the first migration, and that
    counts as version 0. A non-empty database is copied before the first
    statement of a migration. At most three copies are kept.
    """
    # Callers may hand over a connection that still has the default implicit
    # transaction. Autocommit lets BEGIN IMMEDIATE be the only transaction.
    conn.isolation_level = None
    directory = migrations if migrations is not None else migrations_dir()
    files = _migration_files(directory)
    code_version = files[-1][0] if files else 0
    current = schema_version(conn)
    if current > code_version:
        logger.error("store_schema_newer")
        raise StoreSchemaNewer(current, code_version)
    pending = [(number, path) for number, path in files if number > current]
    if not pending:
        return current
    if _non_empty(conn):
        _backup(conn, Path(data_dir), current, pending[-1][0], now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for number, path in pending:
            _exec_script(conn, path.read_text(encoding="utf-8"))
            _set_version(conn, number)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return pending[-1][0]


def schema_version(conn: sqlite3.Connection) -> int:
    """Return `meta.schema_version`, or 0 when the database has no such row."""
    master = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if master is None:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return 0
    return int(row[0])


def _migration_files(directory: Path) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    if not directory.is_dir():
        return found
    for path in directory.iterdir():
        match = _MIGRATION_NAME.match(path.name)
        if match is None or not path.is_file():
            continue
        found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    numbers = [number for number, _path in found]
    if len(numbers) != len(set(numbers)):
        raise RuntimeError("duplicate migration numbers")
    return found


def _non_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return row is not None


def _backup(
    conn: sqlite3.Connection, data_dir: Path, from_version: int, to_version: int, now: datetime
) -> None:
    stamp = _stamp(now)
    backup_dir = data_dir / "backup"
    previous = os.umask(0o077)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / f"box.sqlite3.{from_version}-{to_version}.{stamp}"
        target = sqlite3.connect(dest)
        try:
            conn.backup(target)
        finally:
            target.close()
        os.chmod(dest, 0o600)
    finally:
        os.umask(previous)
    _prune(backup_dir)


def _prune(backup_dir: Path) -> None:
    copies = []
    for path in backup_dir.iterdir():
        match = _BACKUP_NAME.match(path.name)
        if match is None or not path.is_file():
            continue
        copies.append((match.group(3), path.name, path))
    copies.sort()
    while len(copies) > _BACKUPS_KEPT:
        copies[0][2].unlink()
        copies.pop(0)


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


def _exec_script(conn: sqlite3.Connection, sql: str) -> None:
    # executescript() commits first, which would break the single migration
    # transaction. Statements in these files do not contain embedded semicolons.
    for statement in _statements(sql):
        conn.execute(statement)


def _statements(sql: str) -> list[str]:
    buffered: list[str] = []
    statements: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buffered.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffered).strip()
            if statement.endswith(";"):
                statement = statement[:-1].strip()
            if statement:
                statements.append(statement)
            buffered = []
    tail = "\n".join(buffered).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


def _stamp(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")
