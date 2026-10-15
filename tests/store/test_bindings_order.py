import secrets
import sqlite3
from pathlib import Path

import pytest

from stdio_box.store.repo import Store

PROVIDER = "atlassian"
PROFILE = "atlassian/shared"


def _store(tmp_path: Path, store_key: str) -> Store:
    store = Store.open(tmp_path / "data", store_key)
    store.create_profile(
        PROFILE,
        PROVIDER,
        "jira_cloud",
        {"JIRA_URL": "https://example.atlassian.net"},
        None,
        "tester",
    )
    store.put_secrets(PROFILE, {"JIRA_API_TOKEN": "first-token"}, "tester", version=1)
    return store


def _roles(store: Store) -> list[tuple[str, int]]:
    return [(item.subject, item.position or 0) for item in store.bindings(PROVIDER).role]


def test_role_positions_insert_and_close_gaps(tmp_path: Path, store_key: str) -> None:
    store = _store(tmp_path, store_key)
    for name in ("a", "b", "c"):
        store.bind(PROVIDER, f"role:{name}", PROFILE, "tester")
    assert _roles(store) == [("role:a", 1), ("role:b", 2), ("role:c", 3)]

    store.bind(PROVIDER, "role:d", PROFILE, "tester", position=1)
    assert _roles(store) == [("role:d", 1), ("role:a", 2), ("role:b", 3), ("role:c", 4)]

    store.unbind(PROVIDER, "role:a")
    assert _roles(store) == [("role:d", 1), ("role:b", 2), ("role:c", 3)]

    moved = store.bind(PROVIDER, "role:c", PROFILE, "tester", replace=True, position=1)
    assert moved.replaced is True
    assert moved.position == 1
    assert _roles(store) == [("role:c", 1), ("role:d", 2), ("role:b", 3)]

    match = store.lookup(PROVIDER, "person@example.com", ["b", "c"])
    assert match is not None
    assert match.tier == "role"
    assert match.binding.subject == "role:c"
    assert [item.subject for item in match.not_used] == ["role:b"]

    store.bind(PROVIDER, "role:b", PROFILE, "tester", replace=True, position=1)
    match = store.lookup(PROVIDER, "person@example.com", ["b", "c"])
    assert match is not None
    assert match.binding.subject == "role:b"
    store.close()


def test_user_beats_roles_and_roles_beat_default(tmp_path: Path, store_key: str) -> None:
    store = _store(tmp_path, store_key)
    store.bind(PROVIDER, "role:b", PROFILE, "tester")
    store.bind(PROVIDER, "default", PROFILE, "tester")
    store.bind(PROVIDER, "user:Alice@Example.com", PROFILE, "tester")

    user = store.lookup(PROVIDER, "Alice@Example.com", ["b"])
    assert user is not None and user.tier == "user"
    assert user.binding.subject == "user:alice@example.com"

    role = store.lookup(PROVIDER, "other@example.com", ["b"])
    assert role is not None and role.tier == "role" and role.binding.subject == "role:b"

    # An empty role list is what the caller passes when rbac is off.
    default = store.lookup(PROVIDER, "other@example.com", [])
    assert default is not None and default.tier == "default"
    store.close()


def test_lookup_returns_needs_reentry_profile(tmp_path: Path, store_key: str) -> None:
    data = tmp_path / "data"
    store = _store(tmp_path, store_key)
    store.bind(PROVIDER, "role:c", PROFILE, "tester", position=1)
    store.bind(PROVIDER, "role:b", PROFILE, "tester", position=2)
    store.close()

    other_key = secrets.token_urlsafe(32)
    assert other_key != store_key
    reopened = Store.open(data, other_key)
    assert reopened.key_state == "key_mismatch"
    match = reopened.lookup(PROVIDER, "person@example.com", ["b", "c"])
    assert match is not None
    assert match.binding.subject == "role:c"
    assert match.profile.state == "needs_reentry"
    raw = sqlite3.connect(reopened.path)
    state = raw.execute("SELECT state FROM profiles WHERE id = ?", (PROFILE,)).fetchone()
    raw.close()
    assert state is not None and state[0] == "ready"
    reopened.close()


def test_position_is_rejected_for_user_and_default(tmp_path: Path, store_key: str) -> None:
    store = _store(tmp_path, store_key)
    with pytest.raises(ValueError):
        store.bind(PROVIDER, "user:person@example.com", PROFILE, "tester", position=1)
    with pytest.raises(ValueError):
        store.bind(PROVIDER, "default", PROFILE, "tester", position=1)
    assert store.bindings(PROVIDER).user == []
    assert store.bindings(PROVIDER).default is None
    store.close()
