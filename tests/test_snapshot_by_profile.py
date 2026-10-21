"""creds_snapshot_tools, credsctl snapshot, and stdio-box box-tools."""

import json
import logging
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
import yaml
from starlette.testclient import TestClient
from tests.support.facade import TEST_STORE_KEY, make_config, make_token
from tests.support.routing import ECHO_DIR, ECHO_SERVER

from stdio_box.catalog import build_catalog
from stdio_box.cli import credsctl_main, main
from stdio_box.creds_tools import TOOLS, CredsDeps
from stdio_box.forward import StoreForwarder, render_whoami
from stdio_box.http_app import build_app
from stdio_box.instances import InstanceManager
from stdio_box.provider import ToolRules, load_provider
from stdio_box.resolver import resolve
from stdio_box.snapshot import _differences, _without_taken_at, snapshot_main, snapshot_sha256
from stdio_box.spawner import InlineSpawner, allowlist_for
from stdio_box.store.repo import Store

# Obviously fake values. None of them may appear in a result, a log line, or a table.
USER_TOKEN = "FAKE-USER-TOKEN-7c1e-not-a-secret"
ROLE_TOKEN = "FAKE-ROLE-TOKEN-4b9d-not-a-secret"
EXTRA_SECRET = "FAKE-EXTRA-VALUE-22aa-not-a-secret"
_KEYS = {"id", "sha256", "tools", "profile", "variant", "variant_filters_tools"}
_ECHO_CHILD_TOOLS = [
    "who",
    "slow",
    "block",
    "crash",
    "spam",
    "meta",
    "escape",
    "forkbomb",
    "extra_tool",
]


def test_snapshot_under_user_profile(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with _client(tmp_path) as (client, store):
        profile_id = _seed(store, "mine", USER_TOKEN)
        store.bind("echo", "user:ann@example.com", profile_id, "tester")
        result = _call(client, sub="ann@example.com")
        payload = _ok(result)
        assert set(payload) == _KEYS
        assert isinstance(payload["id"], int)
        assert payload["profile"] == profile_id
        assert payload["variant"] == "token"
        assert payload["variant_filters_tools"] is True
        assert payload["tools"] == _ECHO_CHILD_TOOLS
        stored = store.get_tool_snapshot(payload["id"])
        assert stored is not None
        assert stored.profile == profile_id
        assert stored.variant == "token"
        assert stored.subject == "user:ann@example.com"
        assert stored.sha256 == payload["sha256"]
        assert snapshot_sha256(json.loads(stored.body)) == payload["sha256"]
        events = store.list_audit(event="call")
        assert [item["tool"] for item in events] == ["creds_snapshot_tools"]
        assert events[0]["profile_id"] == profile_id
        _assert_no_secret(tmp_path, json.dumps(result) + caplog.text, USER_TOKEN)


def test_snapshot_under_role_profile(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with _client(tmp_path) as (client, store):
        role_profile = _seed(store, "team", ROLE_TOKEN, variant="full")
        other = _seed(store, "other", USER_TOKEN)
        store.bind("echo", "role:dev", role_profile, "tester")
        store.bind("echo", "user:someone-else@example.com", other, "tester")
        result = _call(client, sub="bob@example.com", roles=["dev"])
        payload = _ok(result)
        assert payload["profile"] == role_profile
        assert payload["variant"] == "full"
        stored = store.get_tool_snapshot(payload["id"])
        assert stored is not None
        assert stored.subject == "role:dev"
        assert stored.profile == role_profile
        blob = json.dumps(result) + caplog.text
        _assert_no_secret(tmp_path, blob, ROLE_TOKEN)
        _assert_no_secret(tmp_path, blob, EXTRA_SECRET)


def test_snapshot_without_profile_is_refused(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, store):
        other = _seed(store, "other", USER_TOKEN)
        store.bind("echo", "user:someone-else@example.com", other, "tester")
        refused = _error(_call(client, sub="nobody@example.com", roles=["member"]))
        assert refused["code"] == "no_credentials_profile"
        assert store.list_tool_snapshots() == []
        assert [item["code"] for item in store.list_audit(event="refusal")] == [
            "no_credentials_profile"
        ]


def test_snapshot_refuses_arguments(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, store):
        profile_id = _seed(store, "mine", USER_TOKEN)
        store.bind("echo", "user:ann@example.com", profile_id, "tester")
        refused = _error(_call(client, {"profile": "echo/other"}, sub="ann@example.com"))
        assert refused["code"] == "invalid_argument"
        assert store.list_tool_snapshots() == []


def test_profile_without_secrets_is_refused(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, store):
        store.create_profile(
            "echo/empty", "echo", "token", {"ECHO_URL": "https://echo.invalid"}, None, "t"
        )
        store.bind("echo", "user:ann@example.com", "echo/empty", "tester")
        refused = _error(_call(client, sub="ann@example.com"))
        assert refused["code"] == "profile_needs_reentry"
        assert store.list_tool_snapshots() == []


def test_raw_json_matches_snapshot_tools_and_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _client(tmp_path) as (client, store):
        profile_id = _seed(store, "mine", USER_TOKEN)
        store.bind("echo", "user:ann@example.com", profile_id, "tester")
        payload = _ok(_call(client, sub="ann@example.com"))
        stored = store.get_tool_snapshot(payload["id"])
        assert stored is not None

    _credsctl_env(tmp_path, monkeypatch)
    code, exported, _err = _credsctl(["snapshot", "export", str(payload["id"])], capsys)
    assert code == 0
    assert exported == stored.body
    assert snapshot_sha256(json.loads(exported)) == payload["sha256"]

    provider_dir = tmp_path / "echo"
    provider_dir.mkdir()
    for name in ("provider.yaml", "requirements.txt"):
        (provider_dir / name).write_bytes((ECHO_DIR / name).read_bytes())
    environ = {
        "BOX_DEBUG": "1",
        "BOX_CHILD_ARGV_OVERRIDE": f"{sys.executable} {ECHO_SERVER}",
        "BOX_PROVIDER_DIR": str(provider_dir),
    }
    out = tmp_path / "taken.json"
    assert snapshot_main(["--provider-dir", str(provider_dir), "--out", str(out)], environ) == 0
    taken = json.loads(out.read_text(encoding="utf-8"))
    assert _differences(_without_taken_at(taken), _without_taken_at(json.loads(exported))) == []
    assert snapshot_sha256(taken) == payload["sha256"]
    # The stored text has the same layout snapshot-tools writes.
    assert exported.startswith("{\n")
    assert exported.endswith("}\n")


def test_tools_are_published_names_and_raw_json_is_not(tmp_path: Path) -> None:
    base = load_provider(ECHO_DIR)
    provider = replace(
        base,
        tools=ToolRules(
            strip_prefix=None,
            normalize=None,
            rename=(("who", "who_am_i"),),
            hide=("crash",),
        ),
    )
    with _client(tmp_path, provider=provider) as (client, store):
        profile_id = _seed(store, "mine", USER_TOKEN)
        store.bind("echo", "user:ann@example.com", profile_id, "tester")
        payload = _ok(_call(client, sub="ann@example.com"))
        assert "who_am_i" in payload["tools"]
        assert "who" not in payload["tools"]
        assert "crash" not in payload["tools"]
        stored = store.get_tool_snapshot(payload["id"])
        assert stored is not None
        raw_names = [tool["name"] for tool in json.loads(stored.body)["tools"]]
        assert raw_names == _ECHO_CHILD_TOOLS


def test_variant_without_tool_patterns(tmp_path: Path) -> None:
    base = load_provider(ECHO_DIR)
    open_variant = replace(base.variants[0], tools=None)
    provider = replace(base, variants=(open_variant, *base.variants[1:]))
    with _client(tmp_path, provider=provider) as (client, store):
        profile_id = _seed(store, "mine", USER_TOKEN)
        store.bind("echo", "default", profile_id, "tester")
        payload = _ok(_call(client, sub="anyone@example.com"))
        assert payload["variant_filters_tools"] is False
        stored = store.get_tool_snapshot(payload["id"])
        assert stored is not None
        assert stored.subject == "default"


def test_only_the_newest_five_are_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _client(tmp_path) as (client, store):
        profile_id = _seed(store, "mine", USER_TOKEN)
        store.bind("echo", "user:ann@example.com", profile_id, "tester")
        ids = [_ok(_call(client, sub="ann@example.com", jti=f"j-{n}"))["id"] for n in range(7)]
        listed = store.list_tool_snapshots()
        assert [item.id for item in listed] == list(reversed(ids[-5:]))
        assert len({item.sha256 for item in listed}) == 1

    _credsctl_env(tmp_path, monkeypatch)
    code, out, _err = _credsctl(["--json", "snapshot", "list"], capsys)
    assert code == 0
    rows = json.loads(out)["result"]["snapshots"]
    assert [row["id"] for row in rows] == list(reversed(ids[-5:]))
    assert set(rows[0]) == {"id", "sha256", "taken_at", "profile"}
    assert rows[0]["profile"] == profile_id

    code, out, err = _credsctl(["snapshot", "export", str(ids[0])], capsys)
    assert code == 3
    assert out == ""
    assert "snapshot not found" in err
    code, out, _err = _credsctl(["--json", "snapshot", "export", "not-an-id"], capsys)
    assert code == 3
    assert json.loads(out)["error"]["id"] == "snapshot_not_found"
    code, _out, _err = _credsctl(["snapshot", "export"], capsys)
    assert code == 2


def test_box_tools_prints_eleven_names(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["box-tools"])
    assert exc.value.code == 0
    names = json.loads(capsys.readouterr().out)
    assert len(names) == 11
    assert names == [tool.name for tool in TOOLS]
    assert names[-1] == "creds_snapshot_tools"
    assert all(name.startswith("creds_") for name in names)


def test_snapshot_tool_annotations_and_policy(capsys: pytest.CaptureFixture[str]) -> None:
    tool = next(item for item in TOOLS if item.name == "creds_snapshot_tools")
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.openWorldHint is True
    assert tool.inputSchema["properties"] == {}
    with pytest.raises(SystemExit) as exc:
        main(["policy-example", "--namespace", "echo", "--provider-dir", str(ECHO_DIR)])
    assert exc.value.code == 0
    loaded = yaml.safe_load(capsys.readouterr().out)
    assert loaded["roles"]["echo_box_admin"]["allow"] == ["echo_creds_*"]
    assert "echo_creds_snapshot_tools" not in loaded["requires_human_identity"]
    assert "echo_creds_snapshot_tools" not in loaded["roles"]["echo_reader"]["allow"]
    assert "echo_creds_snapshot_tools" not in loaded["roles"]["echo_writer"]["allow"]


def _assert_no_secret(tmp_path: Path, blob: str, secret: str) -> None:
    assert secret not in blob
    conn = sqlite3.connect(tmp_path / "data" / "box.sqlite3")
    try:
        for table in ("tool_snapshots", "audit", "profiles", "meta", "status"):
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                assert secret not in json.dumps(row, default=str)
    finally:
        conn.close()


def _seed(store: Store, name: str, token: str, *, variant: str = "token") -> str:
    profile_id = f"echo/{name}"
    store.create_profile(
        profile_id, "echo", variant, {"ECHO_URL": "https://echo.invalid"}, None, "tester"
    )
    secrets = {"ECHO_TOKEN": token}
    if variant == "full":
        secrets["ECHO_EXTRA"] = EXTRA_SECRET
    store.put_secrets(profile_id, secrets, "tester", version=1)
    return profile_id


@contextmanager
def _client(tmp_path: Path, *, provider=None):
    if provider is None:
        provider = load_provider(ECHO_DIR)
    snapshot = json.loads((ECHO_DIR / "tools.snapshot.json").read_text(encoding="utf-8"))
    catalog = build_catalog(snapshot, provider)
    names = frozenset(tool["name"] for tool in snapshot["tools"])
    store = Store.open(
        tmp_path / "data",
        TEST_STORE_KEY,
        field_names=[field.name for field in provider.fields],
    )
    config = make_config(data_dir=str(tmp_path / "data"), allowed_hosts=("echo.example",))
    argv = [sys.executable, str(ECHO_SERVER)]
    slot_root = str(tmp_path / "slots")
    manager = InstanceManager(
        InlineSpawner(allowlist_for(argv[0]), slot_root=slot_root),
        store,
        provider,
        catalog,
        argv,
        snapshot_names=names,
        max_instances=2,
        idle_seconds=600,
        start_timeout_seconds=15,
        slot_root=slot_root,
    )
    forwarder = StoreForwarder(store, provider, catalog, manager, call_timeout_seconds=30)

    async def whoami(identity):
        def _resolve():
            return render_whoami(provider, identity, resolve(store, provider, identity))

        return await anyio.to_thread.run_sync(_resolve)

    app = build_app(
        config,
        provider,
        catalog,
        forwarder,
        store,
        whoami=whoami,
        background=manager.run,
        form_stats=manager,
        creds=CredsDeps(config, store, manager, catalog),
    )
    try:
        with TestClient(app, base_url="http://echo.example") as client:
            yield client, store
            assert "ephemeral-check" not in manager._slots.values()
    finally:
        store.close()


def _call(client: TestClient, arguments: dict | None = None, **token) -> dict:
    name = "creds_snapshot_tools"
    response = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": "Bearer box-test-bearer-token-aaaaaaaaaa",
            "X-Keystone-Identity": make_token(tool=name, **token),
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert response.status_code == 200, response.text
    return json.loads(response.text)["result"]


def _ok(result: dict) -> dict:
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _error(result: dict) -> dict:
    assert result.get("isError") is True, result
    return json.loads(result["content"][0]["text"])


def _credsctl_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BOX_STORE_KEY", TEST_STORE_KEY)
    monkeypatch.setenv("BOX_PROVIDER", "echo")
    monkeypatch.setenv("BOX_PROVIDER_DIR", str(ECHO_DIR))


def _credsctl(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    capsys.readouterr()
    with pytest.raises(SystemExit) as raised:
        credsctl_main(args)
    captured = capsys.readouterr()
    code = raised.value.code
    assert isinstance(code, int)
    return code, captured.out, captured.err
