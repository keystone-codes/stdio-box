"""creds_* tools, tool checks, and the sample policy."""

import json
import logging
import re
import socket
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from jsonschema import Draft202012Validator
from starlette.testclient import TestClient
from tests.support.facade import TEST_STORE_KEY, make_config, make_token
from tests.support.routing import ECHO_DIR, ECHO_SERVER, load_echo

from stdio_box.catalog import build_catalog
from stdio_box.cli import main
from stdio_box.creds_tools import TOOLS, CredsDeps
from stdio_box.forward import StoreForwarder, render_whoami
from stdio_box.http_app import build_app
from stdio_box.instances import InstanceManager
from stdio_box.provider import Check, Field, HttpAuth, HttpCheck, Variant, load_provider
from stdio_box.resolver import resolve
from stdio_box.spawner import InlineSpawner, allowlist_for
from stdio_box.store.repo import Store

ECHO_HTTP = Path(__file__).resolve().parent / "data" / "packages" / "echo-http"
ORIGIN = "https://echo.example"
SENTINEL = "SENTINEL-SECRET-9f3a"
_CHANGING = [
    "echo_creds_profile_create",
    "echo_creds_profile_rotate",
    "echo_creds_profile_delete",
    "echo_creds_bind",
    "echo_creds_unbind",
]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_host, _port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_input_schemas() -> None:
    assert len(TOOLS) == 11
    for tool in TOOLS:
        Draft202012Validator.check_schema(tool.inputSchema)


def test_policy_example(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "policy-example",
                "--namespace",
                "echo",
                "--provider-dir",
                str(ECHO_DIR),
                "--critical",
                "who",
            ]
        )
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert text.splitlines()[:4] == [
        "# Example only. keystonectl prints this file and never merges it into your policy.",
        "# The box does not check roles: whoever the policy lets call echo_creds_* manages",
        "# everyone's accounts in this box. Do not give a non-administrator role echo_* :",
        "# it also matches echo_creds_*.",
    ]
    assert (
        "# Only a signed-in person may change accounts; API keys and service accounts may not."
        in text
    )
    loaded = yaml.safe_load(text)
    reader = loaded["roles"]["echo_reader"]["allow"]
    writer = loaded["roles"]["echo_writer"]["allow"]
    for name in reader + writer:
        assert "echo_*" not in name
        assert "*" not in name
    assert reader == ["echo_creds_whoami", "echo_creds_providers"]
    assert writer[-2:] == ["echo_creds_whoami", "echo_creds_providers"]
    assert "echo_who" in writer
    assert "echo_extra_tool" in writer
    assert loaded["roles"]["echo_box_admin"]["allow"] == ["echo_creds_*"]
    assert loaded["requires_human_identity"] == _CHANGING
    assert loaded["critical_tools"] == [*_CHANGING, "echo_who"]
    assert loaded["version"] == "2.0.0"
    assert loaded["default_deny"] is True


def test_create_form_bind_and_delete(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, store, _manager, _provider):
        created = _ok(
            _call(
                client,
                "creds_profile_create",
                {
                    "name": "main",
                    "variant": "token",
                    "fields": {"ECHO_URL": "https://echo.invalid"},
                },
            )
        )
        assert created["state"] == "awaiting_secrets"
        assert created["secret_fields"] == ["ECHO_TOKEN"]
        assert created["next"].startswith("Send this link")
        token = created["form_url"].rstrip("/").split("/")[-1]
        assert token not in json.dumps(store.list_audit())
        _token, response = _submit_form(
            client, created["form_url"], {"ECHO_TOKEN": "entered-token-value"}
        )
        assert response.status_code == 200
        assert "Saved" in response.text
        listed = _ok(_call(client, "creds_profiles", {}))
        profile = listed["profiles"][0]
        assert profile["id"] == "echo/main"
        assert profile["state"] == "ready"
        assert profile["last_check"]["ok"] is True
        assert "identity" in profile["last_check"]
        bound = _ok(_call(client, "creds_bind", {"subject": "role:dev", "profile": "echo/main"}))
        assert bound["position"] == 1
        assert bound["order"][0]["subject"] == "role:dev"
        who = _ok(_call(client, "creds_whoami", {}, roles=["dev"]))
        assert who["profile"] == "echo/main"
        assert who["tier"] == "role"
        refused = _error(_call(client, "creds_profile_delete", {"profile": "echo/main"}))
        assert refused["code"] == "profile_in_use"
        assert "role:dev" in refused["message"]
        _ok(_call(client, "creds_unbind", {"subject": "role:dev"}))
        deleted = _ok(_call(client, "creds_profile_delete", {"profile": "echo/main"}))
        assert deleted == {"profile": "echo/main", "deleted": True}
        assert _ok(_call(client, "creds_profiles", {}))["profiles"] == []


def test_role_order(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, store, _manager, _provider):
        first = _seed(store, "a")
        second = _seed(store, "b")
        _ok(_call(client, "creds_bind", {"subject": "role:dev", "profile": first}))
        _ok(_call(client, "creds_bind", {"subject": "role:ops", "profile": second}))
        who = _ok(_call(client, "creds_whoami", {}, sub="dev@example.com", roles=["dev", "ops"]))
        assert who["profile"] == first
        assert who["tier"] == "role"
        assert any(item["subject"] == "role:ops" for item in who["not_used"])
        moved = _ok(
            _call(
                client,
                "creds_bind",
                {"subject": "role:ops", "profile": second, "replace": True, "position": 1},
            )
        )
        assert [item["subject"] for item in moved["order"]] == ["role:ops", "role:dev"]
        again = _ok(_call(client, "creds_whoami", {}, sub="dev@example.com", roles=["dev", "ops"]))
        assert again["profile"] == second
        listed = _ok(_call(client, "creds_bindings", {}))
        assert [item["subject"] for item in listed["role"]] == ["role:ops", "role:dev"]
        assert listed["role"][0]["state"] == "ready"
        refused = _error(
            _call(client, "creds_bind", {"subject": "user:x", "profile": first, "position": 1})
        )
        assert refused["code"] == "invalid_argument"
        after = _ok(_call(client, "creds_bindings", {}))
        assert all(item["subject"] != "user:x" for item in after["user"])


def test_service_account_binding(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, store, _manager, _provider):
        profile_id = _seed(store, "a")
        _ok(_call(client, "creds_bind", {"subject": "user:svc:backend", "profile": profile_id}))
        who = _ok(
            _call(
                client,
                "creds_whoami",
                {},
                sub="svc:backend",
                sub_type="service",
                roles=["member"],
            )
        )
        assert who["profile"] == profile_id
        assert who["tier"] == "user"
        assert who["sub_type"] == "service"


def test_invalid_argument(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, store, _manager, _provider):
        refused = _error(
            _call(
                client,
                "creds_profile_create",
                {"name": "main", "fields": {"ECHO_URL": "https://echo.invalid"}},
            )
        )
        assert refused["code"] == "invalid_argument"
        assert "token" in refused["message"]
        assert store.list_profiles("echo") == []


def test_profile_exists(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, _store, _manager, _provider):
        fields = {
            "name": "main",
            "variant": "token",
            "fields": {"ECHO_URL": "https://echo.invalid"},
        }
        _ok(_call(client, "creds_profile_create", fields))
        refused = _error(_call(client, "creds_profile_create", fields))
        assert refused["code"] == "profile_exists"


def test_secret_field_in_chat(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with _echo_client(tmp_path) as (client, store, _manager, _provider):
        refused = _error(
            _call(
                client,
                "creds_profile_create",
                {
                    "name": "main",
                    "variant": "token",
                    "fields": {"echo_token": SENTINEL, "ECHO_URL": "https://echo.invalid"},
                },
            )
        )
        assert refused["code"] == "secret_field_in_chat"
        assert "ECHO_TOKEN" in refused["message"]
        blob = json.dumps(refused) + json.dumps(store.list_audit()) + caplog.text
        assert SENTINEL not in blob
        assert store.list_profiles("echo") == []


def test_missing_fields(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, store, _manager, _provider):
        refused = _error(
            _call(client, "creds_profile_create", {"name": "main", "variant": "token"})
        )
        assert refused["code"] == "missing_fields"
        assert "ECHO_URL" in refused["message"]
        assert store.list_profiles("echo") == []


def test_form_unavailable(tmp_path: Path) -> None:
    with _echo_client(tmp_path, public_base_url="https://public-domain-pending.invalid") as (
        client,
        store,
        _manager,
        _provider,
    ):
        refused = _error(
            _call(
                client,
                "creds_profile_create",
                {
                    "name": "main",
                    "variant": "token",
                    "fields": {"ECHO_URL": "https://echo.invalid"},
                },
            )
        )
        assert refused["code"] == "form_unavailable"
        assert "credsctl profile import echo/main" in refused["hint"]
        profile = store.get_profile("echo/main")
        assert profile is not None
        assert profile.state == "awaiting_secrets"


def test_check_failed(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(500))
        with _http_client(tmp_path) as (client, store, _manager, provider):
            refused = _error(
                _call(
                    client,
                    "creds_profile_create",
                    {"name": "main", "variant": "label", "fields": {"ECHO_LABEL": "kept"}},
                )
            )
            assert refused["code"] == "check_failed"
            profile = store.get_profile(f"{provider.name}/main")
            assert profile is not None
            assert profile.state == "check_failed"


def test_profile_not_found(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, _store, _manager, _provider):
        for name, arguments in (
            ("creds_profile_delete", {"profile": "echo/missing"}),
            (
                "creds_profile_rotate",
                {"profile": "echo/missing", "replace_secrets": False, "fields": {}},
            ),
            ("creds_bind", {"subject": "role:dev", "profile": "echo/missing"}),
            ("creds_check", {"profile": "echo/missing"}),
        ):
            assert _error(_call(client, name, arguments))["code"] == "profile_not_found"


def test_binding_exists(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, store, _manager, _provider):
        profile_id = _seed(store, "a")
        _ok(_call(client, "creds_bind", {"subject": "user:ann@example.com", "profile": profile_id}))
        refused = _error(
            _call(client, "creds_bind", {"subject": "user:ann@example.com", "profile": profile_id})
        )
        assert refused["code"] == "binding_exists"
        assert "already uses" in refused["message"]
        assert "replace: true" in refused["message"]


def test_binding_not_found(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, _store, _manager, _provider):
        refused = _error(_call(client, "creds_unbind", {"subject": "user:nobody@example.com"}))
        assert refused["code"] == "binding_not_found"


def test_unknown_provider(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, _store, _manager, _provider):
        refused = _error(_call(client, "creds_profiles", {"provider": "other"}))
        assert refused["code"] == "unknown_provider"


def test_create_rejects_private_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(host, _port, *_args, **_kwargs):
        address = "10.0.0.5" if host == "internal.invalid" else "1.1.1.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with _echo_client(tmp_path) as (client, store, _manager, _provider):
        refused = _error(
            _call(
                client,
                "creds_profile_create",
                {
                    "name": "main",
                    "variant": "token",
                    "fields": {"ECHO_URL": "https://internal.invalid"},
                },
            )
        )
        assert refused["code"] == "invalid_argument"
        assert store.get_profile("echo/main") is None


def test_rotate_nonsecret_only(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    mode = {"status": 200}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if mode["status"] != 200:
            return httpx.Response(mode["status"])
        return httpx.Response(200, json={"name": "Ada"})

    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=respond)
        with _http_client(tmp_path) as (client, store, manager, provider):
            profile_id = _seed_account(store, provider.name)
            checked = _ok(_call(client, "creds_check", {"profile": profile_id}))
            assert checked["ok"] is True
            running = manager.current(profile_id)
            assert running is not None
            pid = running.pid
            mode["status"] = 401
            refused = _error(
                _call(
                    client,
                    "creds_profile_rotate",
                    {
                        "profile": profile_id,
                        "replace_secrets": False,
                        "fields": {"ECHO_LABEL": "rejected-label"},
                    },
                )
            )
            assert refused["code"] == "check_failed"
            assert store.get_profile(profile_id).public_fields["ECHO_LABEL"] == "old-label"
            assert manager.current(profile_id) is not None
            assert manager.current(profile_id).pid == pid
            mode["status"] = 200
            saved = _ok(
                _call(
                    client,
                    "creds_profile_rotate",
                    {
                        "profile": profile_id,
                        "replace_secrets": False,
                        "fields": {"ECHO_LABEL": "new-label"},
                    },
                )
            )
            assert saved["state"] == "ready"
            assert store.get_profile(profile_id).public_fields["ECHO_LABEL"] == "new-label"
            assert manager.current(profile_id) is None


def test_rotate_new_url_requires_secrets(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"name": "Ada"})

    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=respond)
        with _http_client(tmp_path) as (client, store, _manager, provider):
            profile_id = _seed_account(store, provider.name)
            refused = _error(
                _call(
                    client,
                    "creds_profile_rotate",
                    {
                        "profile": profile_id,
                        "replace_secrets": False,
                        "fields": {"ECHO_URL": "https://new.example"},
                    },
                )
            )
            assert refused["code"] == "secret_reentry_required"
            assert seen == []
            header = _error(
                _call(
                    client,
                    "creds_profile_rotate",
                    {
                        "profile": profile_id,
                        "replace_secrets": False,
                        "fields": {"ECHO_ORG": "other"},
                    },
                )
            )
            assert header["code"] == "secret_reentry_required"
            assert seen == []
            assert store.get_profile(profile_id).public_fields["ECHO_URL"] == "https://old.example"
            linked = _ok(
                _call(
                    client,
                    "creds_profile_rotate",
                    {
                        "profile": profile_id,
                        "replace_secrets": True,
                        "fields": {"ECHO_URL": "https://new.example"},
                    },
                )
            )
            page = client.get("/creds/" + linked["form_url"].rstrip("/").split("/")[-1])
            assert page.status_code == 200
            assert "https://new.example" in page.text
            assert seen == []
            assert store.get_profile(profile_id).public_fields["ECHO_URL"] == "https://old.example"
            _token, response = _submit_form(
                client, linked["form_url"], {"ECHO_TOKEN": "rotated-token-value"}
            )
            assert response.status_code == 200, response.text
            assert len(seen) == 1
            assert seen[0].headers["host"] == "new.example"
            assert seen[0].headers["authorization"] == "Bearer rotated-token-value"
            assert "previous-token-value" not in seen[0].headers.get("authorization", "")
            assert store.get_profile(profile_id).public_fields["ECHO_URL"] == "https://new.example"
            assert store.read_secrets(profile_id)["ECHO_TOKEN"] == "rotated-token-value"


def test_creds_check_starts_instance(tmp_path: Path) -> None:
    with _echo_client(tmp_path) as (client, store, manager, _provider):
        profile_id = _seed(store, "main")
        checked = _ok(_call(client, "creds_check", {"profile": profile_id}))
        assert checked["ok"] is True
        assert checked["checks"][0]["ok"] is True
        assert checked["instance"]["state"] == "running"
        assert checked["instance"]["tools"] == 8
        assert manager.current(profile_id) is not None
        assert manager.current(profile_id).state == "running"
        assert "ephemeral-check" not in manager._slots.values()
        assert list(manager._slots.values()) == [profile_id]


@contextmanager
def _echo_client(tmp_path: Path, **overrides):
    provider, catalog, names = load_echo()
    yield from _open_client(tmp_path, provider, catalog, names, **overrides)


@contextmanager
def _http_client(tmp_path: Path):
    provider = _account_provider()
    snapshot = json.loads((ECHO_HTTP / "tools.snapshot.json").read_text(encoding="utf-8"))
    catalog = build_catalog(snapshot, provider)
    names = frozenset(tool.name for tool in catalog.published_tools())
    yield from _open_client(tmp_path, provider, catalog, names)


def _open_client(tmp_path: Path, provider, catalog, names, **overrides):
    import anyio

    store = Store.open(
        tmp_path / "data",
        TEST_STORE_KEY,
        field_names=[field.name for field in provider.fields],
    )
    settings = {
        "data_dir": str(tmp_path / "data"),
        "allowed_hosts": ("echo.example",),
        "public_base_url": ORIGIN,
    }
    settings.update(overrides)
    config = make_config(**settings)
    argv = [sys.executable, str(ECHO_SERVER)]
    slot_root = str(tmp_path / "slots")
    manager = InstanceManager(
        InlineSpawner(allowlist_for(argv[0]), slot_root=slot_root),
        store,
        provider,
        catalog,
        argv,
        snapshot_names=names,
        max_instances=4,
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
            yield client, store, manager, provider
    finally:
        store.close()


def _account_provider():
    base = load_provider(ECHO_HTTP)
    fields = (
        Field("ECHO_URL", "url", False, "Address", None, 2048),
        Field("ECHO_TOKEN", "string", True, "Token", None, 4096),
        Field("ECHO_ORG", "string", False, "Org", None, 200),
        Field("ECHO_LABEL", "string", False, "Label", None, 200),
    )
    account_http = HttpCheck(
        method="GET",
        url="{ECHO_URL}/who",
        expect_status=(200,),
        auth=HttpAuth(kind="bearer", bearer="ECHO_TOKEN"),
        headers=(("X-Org-ID", "{ECHO_ORG}"),),
        identity="name",
        timeout_seconds=5,
    )
    label_http = HttpCheck(
        method="GET",
        url="https://echo.example/label",
        expect_status=(200,),
        auth=None,
        headers=(),
        identity="name",
        timeout_seconds=5,
    )
    account = Variant(
        "account",
        "Account",
        ("ECHO_URL", "ECHO_TOKEN", "ECHO_ORG", "ECHO_LABEL"),
        None,
        ("*.example",),
        (Check(kind="http", http=account_http),),
    )
    label = Variant(
        "label",
        "Label",
        ("ECHO_LABEL",),
        None,
        ("*.example",),
        (Check(kind="http", http=label_http),),
    )
    return replace(base, fields=fields, variants=(account, label))


def _seed(store: Store, name: str) -> str:
    profile_id = f"echo/{name}"
    store.create_profile(
        profile_id,
        "echo",
        "token",
        {"ECHO_URL": "https://echo.invalid"},
        None,
        "tester",
    )
    store.put_secrets(profile_id, {"ECHO_TOKEN": "token-value-aaaa"}, "tester", version=1)
    store.set_check(profile_id, True, None, datetime.now(timezone.utc))
    return profile_id


def _seed_account(store: Store, provider_name: str) -> str:
    profile_id = f"{provider_name}/shared"
    store.create_profile(
        profile_id,
        provider_name,
        "account",
        {
            "ECHO_URL": "https://old.example",
            "ECHO_ORG": "acme",
            "ECHO_LABEL": "old-label",
        },
        None,
        "tester",
    )
    store.put_secrets(profile_id, {"ECHO_TOKEN": "previous-token-value"}, "tester", version=1)
    store.set_check(profile_id, True, "Ada", datetime.now(timezone.utc))
    return profile_id


def _call(client: TestClient, name: str, arguments: dict, **token) -> dict:
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
            "params": {"name": name, "arguments": arguments},
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


def _submit_form(client: TestClient, form_url: str, fields: dict[str, str]) -> tuple[str, object]:
    token = form_url.rstrip("/").split("/")[-1]
    page = client.get(f"/creds/{token}")
    assert page.status_code == 200, page.text
    match = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text)
    assert match is not None
    cookie = page.headers["set-cookie"].split(";", 1)[0]
    nonce = cookie.split("=", 1)[1]
    response = client.post(
        f"/creds/{token}",
        data={"csrf": match.group(1), **fields},
        headers={"Origin": ORIGIN, "Cookie": f"__Host-box_form={nonce}"},
    )
    return token, response
