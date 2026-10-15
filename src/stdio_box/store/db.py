"""SQLite connection for the on-volume profile store."""

import os
import sqlite3
from pathlib import Path

DB_NAME = "box.sqlite3"


def connect(path: Path) -> sqlite3.Connection:
    """Open `path`, creating it mode 0600.

    The file is created under umask 077 so the default 0666 permission lands
    as 0600. WAL side files are created later and are chmod'd by the caller.
    """
    previous = os.umask(0o077)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    finally:
        os.umask(previous)
    conn.row_factory = sqlite3.Row
    # These pragmas are not stored in the file. journal_mode is, so the caller
    # sets WAL only after it knows the schema is one this process may write.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def enable_wal(conn: sqlite3.Connection, path: Path) -> None:
    """Turn on WAL and force the database files to mode 0600."""
    previous = os.umask(0o077)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        os.umask(previous)
    secure_files(path)


def secure_files(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            os.chmod(candidate, 0o600)
