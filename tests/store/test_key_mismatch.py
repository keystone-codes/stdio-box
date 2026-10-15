import secrets
import sqlite3
from pathlib import Path

import pytest

from stdio_box.store.errors import SecretUnreadable
from stdio_box.store.repo import Store

ONE = "atlassian/one"
TWO = "atlassian/two"
PUBLIC = {"JIRA_URL": "https://example.atlassian.net"}


def test_mismatch_then_rewrite_then_rekey(tmp_path: Path) -> None:
    key_a = secrets.token_urlsafe(32)
    key_b = secrets.token_urlsafe(32)
    data = tmp_path / "data"
    store = Store.open(data, key_a)
    for profile_id, token in ((ONE, "secret-one"), (TWO, "secret-two")):
        store.create_profile(profile_id, "atlassian", "jira_cloud", dict(PUBLIC), None, "tester")
        store.put_secrets(profile_id, {"JIRA_API_TOKEN": token}, "tester", version=1)
    store.bind("atlassian", "user:svc:backend", ONE, "tester")
    store.bind("atlassian", "default", TWO, "tester")
    store.close()

    opened = Store.open(data, key_b)
    assert opened.key_state == "key_mismatch"
    bindings = opened.bindings("atlassian")
    assert [item.subject for item in bindings.user] == ["user:svc:backend"]
    assert bindings.default is not None and bindings.default.profile_id == TWO
    with pytest.raises(SecretUnreadable):
        opened.read_secrets(ONE)
    assert opened.get_profile(ONE) is not None
    assert opened.get_profile(ONE).state == "needs_reentry"
    assert opened.get_profile(TWO).state == "needs_reentry"
    raw = sqlite3.connect(opened.path)
    stored = dict(raw.execute("SELECT id, state FROM profiles").fetchall())
    raw.close()
    assert stored == {ONE: "ready", TWO: "ready"}

    opened.put_secrets(ONE, {"JIRA_API_TOKEN": "secret-rewritten"}, "tester", version=2)
    assert opened.key_state == "ok"
    assert opened.read_secrets(ONE) == {"JIRA_API_TOKEN": "secret-rewritten"}
    assert opened.get_profile(TWO).state == "needs_reentry"
    with pytest.raises(SecretUnreadable):
        opened.read_secrets(TWO)

    opened.rekey(key_a)
    assert opened.key_state == "ok"
    assert opened.read_secrets(ONE) == {"JIRA_API_TOKEN": "secret-rewritten"}
    assert opened.read_secrets(TWO) == {"JIRA_API_TOKEN": "secret-two"}
    assert opened.get_profile(ONE).state == "ready"
    assert opened.get_profile(TWO).state == "ready"
    opened.close()
