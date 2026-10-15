"""A call, a refusal, and a child error each leave an audit row. Secrets do not."""

import json
import sqlite3
from pathlib import Path

import pytest
from tests.support.routing import make_runtime, open_store, person, seed

from stdio_box.store.audit import list_events

TOKEN = "echo-secret-token-do-not-log"


def _events(path: Path) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return list_events(conn)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_calls_are_audited_without_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    store = open_store(tmp_path)
    profile_id = seed(store, "ann", TOKEN, "user:ann@example.com")
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store)
    async with manager.run():
        ok = await forwarder.call(person("ann@example.com", tool="who", jti="ok"), "who", "who", {})
        refused = await forwarder.call(
            person("stranger@example.com", tool="who", jti="no"), "who", "who", {}
        )
        failed = await forwarder.call(
            person("ann@example.com", tool="slow", jti="bad"),
            "slow",
            "slow",
            {"seconds": "no"},
        )
        await forwarder.call(
            person("ann@example.com", tool="spam", jti="spam"), "spam", "spam", {"lines": 1}
        )
    assert not ok.isError
    assert refused.isError
    assert failed.isError
    rows = _events(store.path)
    by_jti = {row["jti"]: row for row in rows}
    assert by_jti["ok"]["event"] == "call"
    assert by_jti["ok"]["outcome"] == "ok"
    assert by_jti["ok"]["profile_id"] == profile_id
    assert by_jti["ok"]["tier"] == "user"
    assert by_jti["ok"]["duration_ms"] >= 0
    assert by_jti["no"]["event"] == "refusal"
    assert by_jti["no"]["outcome"] == "refused"
    assert by_jti["no"]["code"] == "no_credentials_profile"
    assert by_jti["no"]["duration_ms"] >= 0
    assert by_jti["bad"]["event"] == "call"
    assert by_jti["bad"]["outcome"] == "error"
    assert by_jti["bad"]["profile_id"] == profile_id
    assert by_jti["bad"]["tier"] == "user"
    assert by_jti["bad"]["duration_ms"] >= 0
    blob = json.dumps(rows) + caplog.text
    assert TOKEN not in blob
    store.close()
