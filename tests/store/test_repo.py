import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stdio_box.store.errors import (
    BindingExists,
    LinkNotFound,
    ProfileInUse,
    ProfileNotFound,
)
from stdio_box.store.repo import Store

PROFILE = "atlassian/shared"
PUBLIC = {"JIRA_URL": "https://example.atlassian.net", "JIRA_USERNAME": "person@example.com"}


def _open(tmp_path: Path, store_key: str, **kwargs) -> Store:
    return Store.open(tmp_path / "data", store_key, **kwargs)


def _create(store: Store, profile_id: str = PROFILE) -> None:
    provider, _name = profile_id.split("/", 1)
    store.create_profile(profile_id, provider, "jira_cloud", dict(PUBLIC), None, "tester")


def test_create_read_and_rotate(tmp_path: Path, store_key: str) -> None:
    store = _open(tmp_path, store_key)
    assert (store.path.stat().st_mode & 0o777) == 0o600
    before = store.revision()
    _create(store)
    assert store.revision() == before + 1
    created = store.get_profile(PROFILE)
    assert created is not None
    assert created.state == "awaiting_secrets"
    assert created.public_fields == PUBLIC

    store.put_secrets(PROFILE, {"JIRA_API_TOKEN": "first-token"}, "tester", version=1)
    assert store.read_secrets(PROFILE) == {"JIRA_API_TOKEN": "first-token"}
    ready = store.get_profile(PROFILE)
    assert ready is not None and ready.state == "ready" and ready.version == 1

    store.put_secrets(PROFILE, {"JIRA_API_TOKEN": "second-token"}, "tester", version=2)
    assert store.read_secrets(PROFILE) == {"JIRA_API_TOKEN": "second-token"}
    rotated = store.get_profile(PROFILE)
    assert rotated is not None and rotated.version == 2 and rotated.rotated_by == "tester"
    raw = sqlite3.connect(store.path)
    versions = raw.execute(
        "SELECT version FROM profile_secrets WHERE profile_id = ?", (PROFILE,)
    ).fetchall()
    raw.close()
    assert [row[0] for row in versions] == [2]
    assert store.revision() > before
    store.close()


def test_delete_profile_refuses_while_bound(tmp_path: Path, store_key: str) -> None:
    store = _open(tmp_path, store_key)
    _create(store)
    store.put_secrets(PROFILE, {"JIRA_API_TOKEN": "first-token"}, "tester", version=1)
    store.bind("atlassian", "user:svc:backend", PROFILE, "tester")
    before = store.revision()
    with pytest.raises(ProfileInUse) as raised:
        store.delete_profile(PROFILE)
    assert raised.value.subjects == ["user:svc:backend"]
    assert store.revision() == before
    store.unbind("atlassian", "user:svc:backend")
    store.delete_profile(PROFILE)
    assert store.get_profile(PROFILE) is None
    with pytest.raises(ProfileNotFound):
        store.read_secrets(PROFILE)
    store.close()


def test_bind_replace_and_subject_case(tmp_path: Path, store_key: str) -> None:
    store = _open(tmp_path, store_key)
    _create(store)
    other = "atlassian/other"
    _create(store, other)
    store.bind("atlassian", "user:Alice@Example.com", PROFILE, "tester")
    found = store.bindings("atlassian")
    assert [item.subject for item in found.user] == ["user:alice@example.com"]
    before = store.revision()
    with pytest.raises(BindingExists) as raised:
        store.bind("atlassian", "user:alice@example.com", other, "tester")
    assert raised.value.current == PROFILE
    assert store.revision() == before
    replaced = store.bind("atlassian", "user:alice@example.com", other, "tester", replace=True)
    assert replaced.replaced is True
    assert store.bindings("atlassian").user[0].profile_id == other
    store.bind("atlassian", "user:svc:backend", PROFILE, "tester")
    subjects = [item.subject for item in store.bindings("atlassian").user]
    assert "user:svc:backend" in subjects
    store.close()


def test_links_expire_fail_and_do_not_store_the_token(tmp_path: Path, store_key: str) -> None:
    class Clock:
        def __init__(self) -> None:
            self.moment = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

        def __call__(self) -> datetime:
            return self.moment

    clock = Clock()
    store = Store.open(tmp_path / "data", store_key, clock=clock)
    _create(store)
    before = store.revision()
    first = store.create_link(PROFILE, "create", 60, "tester")
    assert store.revision() == before + 1
    assert store.peek_link(first) is not None
    second = store.create_link(PROFILE, "rotate", 60, "tester")
    assert store.peek_link(first) is None
    assert store.peek_link(second) is not None

    consumed = store.create_link(PROFILE, "create", 60, "tester")
    assert store.peek_link(second) is None
    store.consume_link(consumed)
    assert store.peek_link(consumed) is None
    with pytest.raises(LinkNotFound):
        store.consume_link(consumed)

    failing = store.create_link(PROFILE, "create", 60, "tester")
    for attempt in range(1, 5):
        assert store.fail_link(failing) == attempt
        assert store.peek_link(failing) is not None
    assert store.fail_link(failing) == 5
    assert store.peek_link(failing) is None

    expiring = store.create_link(PROFILE, "create", 60, "tester")
    clock.moment += timedelta(seconds=61)
    assert store.peek_link(expiring) is None

    store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = store.path.read_bytes()
    wal = Path(str(store.path) + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    for token in (first, second, consumed, failing, expiring):
        assert token.encode("utf-8") not in blob
    store.close()


def test_revision_grows_on_each_write(tmp_path: Path, store_key: str) -> None:
    store = _open(tmp_path, store_key)
    seen = [store.revision()]
    _create(store)
    seen.append(store.revision())
    store.put_secrets(PROFILE, {"JIRA_API_TOKEN": "first-token"}, "tester", version=1)
    seen.append(store.revision())
    store.bind("atlassian", "default", PROFILE, "tester")
    seen.append(store.revision())
    store.unbind("atlassian", "default")
    seen.append(store.revision())
    store.record("admin", sub="cli:credsctl", outcome="ok", profile_id=PROFILE)
    seen.append(store.revision())
    assert seen == list(range(seen[0], seen[0] + len(seen)))
    store.close()
