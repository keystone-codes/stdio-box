import logging
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stdio_box.store.errors import StoreSchemaNewer
from stdio_box.store.migrate import migrate, migrations_dir, schema_version


def test_clean_database_reaches_the_latest_version_and_restart_is_a_no_op(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    path = data / "box.sqlite3"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = sqlite3.connect(path)
    assert migrate(conn, data, now) == 3
    conn.close()

    conn = sqlite3.connect(path)
    before = schema_version(conn)
    assert migrate(conn, data, now) == before == 3
    conn.close()
    backup = data / "backup"
    assert not backup.exists() or list(backup.iterdir()) == []


def test_newer_database_is_left_unchanged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    data = tmp_path / "data"
    data.mkdir()
    path = data / "box.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '99')")
    conn.commit()
    before = path.read_bytes()
    with caplog.at_level(logging.ERROR, logger="stdio_box.store"):
        with pytest.raises(StoreSchemaNewer) as raised:
            migrate(conn, data, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert raised.value.store_version == 99
    conn.close()
    assert path.read_bytes() == before
    assert "store_schema_newer" in caplog.text
    assert not (data / "backup").exists()


def test_backup_before_migration_keeps_three(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    shutil.copy(migrations_dir() / "0001_init.sql", migrations / "0001_init.sql")
    data = tmp_path / "data"
    data.mkdir()
    path = data / "box.sqlite3"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    conn = sqlite3.connect(path)
    assert migrate(conn, data, base, migrations) == 1
    conn.close()

    for number in range(2, 6):
        (migrations / f"{number:04d}_step.sql").write_text(
            f"CREATE TABLE step_{number} (id INTEGER);\n", encoding="utf-8"
        )
        conn = sqlite3.connect(path)
        assert migrate(conn, data, base + timedelta(days=number), migrations) == number
        conn.close()

    copies = sorted(path.name for path in (data / "backup").iterdir())
    assert len(copies) == 3
    assert not any(".1-2." in name for name in copies)
    assert any(".2-3." in name for name in copies)
    assert any(".3-4." in name for name in copies)
    assert any(".4-5." in name for name in copies)
    conn = sqlite3.connect(path)
    assert schema_version(conn) == 5
    conn.close()


def test_version_2_profiles_get_an_empty_managed_by(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name in ("0001_init.sql", "0002_tool_snapshots.sql"):
        shutil.copy(migrations_dir() / name, migrations / name)
    data = tmp_path / "data"
    data.mkdir()
    path = data / "box.sqlite3"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = sqlite3.connect(path)
    assert migrate(conn, data, now, migrations) == 2
    conn.execute(
        "INSERT INTO profiles (id, provider, name, variant, state, public_fields, version, "
        "created_at, created_by) VALUES ('echo/a', 'echo', 'a', 'one', 'awaiting_secrets', "
        "'{}', 0, '2026-01-01T00:00:00Z', 'cli:credsctl')"
    )
    assert migrate(conn, data, now) == 3
    row = conn.execute("SELECT managed_by FROM profiles WHERE id = 'echo/a'").fetchone()
    assert row == (None,)
    conn.close()
