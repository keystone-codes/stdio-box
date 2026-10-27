"""creds_desired_get, creds_desired_set, and creds_sync: plan, confirm, forms, prune."""

import fnmatch
import json
import socket
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from tests.support.routing import ECHO_DIR
from tests.test_creds_tools import (
    ECHO_HTTP,
    _account_provider,
    _call,
    _error,
    _http_client,
    _ok,
    _open_client,
    _seed_account,
    _submit_form,
)

from stdio_box.catalog import build_catalog
from stdio_box.cli import main
from stdio_box.desired import SYNC_MARK

LABEL_SET = """
version: 1
profiles:
  echo-http/labels:
    variant: label
    description: Labels
    fields:
      ECHO_LABEL: team
bindings:
  users:
    ann@example.com: echo-http/labels
"""

ACCOUNT_SET = """
version: 1
profiles:
  echo-http/ada:
    variant: account
    fields:
      ECHO_URL: https://ada.example
      ECHO_ORG: acme
      ECHO_LABEL: ada
bindings:
  users:
    ada@example.com: echo-http/ada
"""

SHARED = """
version: 1
profiles:
  echo-http/shared:
    variant: account
    fields:
      ECHO_URL: {url}
      ECHO_ORG: acme
      ECHO_LABEL: {label}
"""


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_host, _port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def upstream():
    """Every check answers 200 unless ``mode["status"]`` says otherwise."""
    mode = {"status": 200, "seen": []}

    def respond(request: httpx.Request) -> httpx.Response:
        mode["seen"].append(request)
        if mode["status"] != 200:
            return httpx.Response(mode["status"])
        return httpx.Response(200, json={"name": "Ada"})

    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=respond)
        yield mode


def test_set_get_and_restore(tmp_path: Path) -> None:
    with _http_client(tmp_path) as (client, store, _manager, _provider):
        empty = _ok(_call(client, "creds_desired_get", {}))
        assert empty["version"] is None and empty["versions"] == []
        before = store.revision()

        first = _ok(_call(client, "creds_desired_set", {"text": LABEL_SET}))
        assert first["version"] == 1
        assert first["unchanged"] is False
        assert first["created_by"] == "ann@example.com"
        same = _ok(_call(client, "creds_desired_set", {"text": LABEL_SET}))
        assert same == {**same, "version": 1, "unchanged": True}
        second = _ok(_call(client, "creds_desired_set", {"text": ACCOUNT_SET}))
        assert second["version"] == 2
        restored = _ok(_call(client, "creds_desired_set", {"from_version": 1}))
        assert restored["version"] == 3
        assert restored["restored_from"] == 1

        current = _ok(_call(client, "creds_desired_get", {}))
        assert current["version"] == 3
        assert current["text"] == LABEL_SET
        assert [item["version"] for item in current["versions"]] == [3, 2, 1]
        old = _ok(_call(client, "creds_desired_get", {"version": 2}))
        assert old["text"] == ACCOUNT_SET
        assert old["latest"] == 3
        missing = _error(_call(client, "creds_desired_get", {"version": 9}))
        assert missing["code"] == "desired_not_found"
        missing = _error(_call(client, "creds_desired_set", {"from_version": 9}))
        assert missing["code"] == "desired_not_found"

        # Storing a set changes no credentials, so the instance watcher sees nothing.
        assert store.revision() == before
        assert store.list_profiles() == []
        tools = [row["tool"] for row in store.list_audit()]
        assert tools.count("creds_desired_get") == 4
        assert tools.count("creds_desired_set") == 5


@pytest.mark.parametrize(
    "entry",
    [
        "    secrets:\n      ECHO_TOKEN: {file: /run/box-sync/ada.token}\n",
        "    secrets:\n      ECHO_TOKEN: {env: ADA_TOKEN}\n",
        "    env_file: /run/box-sync/ada.env\n",
        "    fields:\n      ECHO_TOKEN: inline-value\n",
    ],
)
def test_set_refuses_secrets(tmp_path: Path, entry: str) -> None:
    text = "version: 1\nprofiles:\n  echo-http/ada:\n    variant: account\n" + entry
    with _http_client(tmp_path) as (client, store, _manager, _provider):
        refused = _error(_call(client, "creds_desired_set", {"text": text}))
        assert refused["code"] == "secret_field_in_chat"
        assert refused["message"].startswith("profiles.echo-http/ada: ")
        assert "one-time form" in refused["hint"]
        # The message speaks to a chat: no file or env reference is offered.
        assert "one-time form link" in refused["message"]
        assert "file or env" not in refused["message"]
        assert "inline-value" not in json.dumps(refused)
        assert store.get_desired("echo-http") is None
        rows = store.list_audit()
        assert rows[-1]["tool"] == "creds_desired_set"
        assert rows[-1]["code"] == "secret_field_in_chat"


def test_set_checks_the_whole_set(tmp_path: Path) -> None:
    with _http_client(tmp_path) as (client, store, _manager, _provider):
        broken = _error(_call(client, "creds_desired_set", {"text": "version: [1"}))
        assert broken["code"] == "desired_invalid"
        assert broken["message"] == "the set is not valid YAML"
        unknown = LABEL_SET.replace("ECHO_LABEL: team", "ECHO_COLOUR: red")
        refused = _error(_call(client, "creds_desired_set", {"text": unknown}))
        assert refused["code"] == "desired_invalid"
        assert refused["at"] == "profiles.echo-http/labels"
        private = SHARED.format(url="http://10.0.0.1", label="x")
        refused = _error(_call(client, "creds_desired_set", {"text": private}))
        assert refused["code"] == "desired_invalid"
        dangling = LABEL_SET.replace(
            "ann@example.com: echo-http/labels", "ann@example.com: echo-http/nobody"
        )
        refused = _error(_call(client, "creds_desired_set", {"text": dangling}))
        assert refused["code"] == "profile_not_found"
        assert "neither in the set nor in the box" in refused["message"]
        both = _error(_call(client, "creds_desired_set", {"text": LABEL_SET, "from_version": 1}))
        assert both["code"] == "invalid_argument"
        assert store.get_desired("echo-http") is None


def test_plan_then_confirm(tmp_path: Path, upstream) -> None:
    with _http_client(tmp_path) as (client, store, _manager, _provider):
        nothing = _error(_call(client, "creds_sync", {}))
        assert nothing["code"] == "desired_not_found"
        _ok(_call(client, "creds_desired_set", {"text": LABEL_SET}))
        before = store.revision()

        plan = _ok(_call(client, "creds_sync", {}))
        assert plan["applied"] is False
        assert plan["changes"] == 2
        assert plan["profiles"] == [
            {"profile": "echo-http/labels", "variant": "label", "action": "created"}
        ]
        assert plan["bindings"] == [
            {"subject": "user:ann@example.com", "profile": "echo-http/labels", "action": "bound"}
        ]
        code = plan["confirm"]
        assert isinstance(code, str) and code in plan["next"]
        # A plan writes nothing and runs no checks.
        assert store.list_profiles() == []
        assert store.revision() == before
        assert upstream["seen"] == []

        done = _ok(_call(client, "creds_sync", {"confirm": code}))
        assert done["applied"] is True
        assert done["partial"] is False
        assert done["confirm"] is None
        assert done["profiles"][0]["action"] == "created"
        assert done["profiles"][0]["check"] == "ok"
        profile = store.get_profile("echo-http/labels")
        assert profile.state == "ready"
        assert profile.managed_by == SYNC_MARK
        listed = _ok(_call(client, "creds_profiles", {}))["profiles"]
        assert [item["managed_by"] for item in listed] == [SYNC_MARK]
        assert profile.description == "Labels"
        assert store.bindings("echo-http").user[0].subject == "user:ann@example.com"

        # Nothing left to do: no code, no writes, no restarts.
        after = store.revision()
        again = _ok(_call(client, "creds_sync", {}))
        assert again["changes"] == 0
        assert again["confirm"] is None
        assert again["next"] == "Nothing to change."
        assert again["profiles"][0]["action"] == "unchanged"
        assert store.revision() == after
        assert store.get_profile("echo-http/labels").version == profile.version

        rows = [row for row in store.list_audit() if row["tool"] == "creds_sync"]
        assert [row["detail"].get("command") for row in rows].count("sync") == 2
        summary = [row for row in rows if "confirmed" in (row["detail"] or {})]
        assert [row["detail"]["confirmed"] for row in summary] == [False, True, False]
        assert all(code not in json.dumps(row) for row in store.list_audit())


def test_stale_or_wrong_code_applies_nothing(tmp_path: Path, upstream) -> None:
    with _http_client(tmp_path) as (client, store, _manager, provider):
        _ok(_call(client, "creds_desired_set", {"text": LABEL_SET}))
        code = _ok(_call(client, "creds_sync", {}))["confirm"]

        wrong = _error(_call(client, "creds_sync", {"confirm": "0" * len(code)}))
        assert wrong["code"] == "sync_plan_stale"
        pruned = _error(_call(client, "creds_sync", {"confirm": code, "prune": True}))
        assert pruned["code"] == "sync_plan_stale"

        # The box changed between the plan and the confirmation.
        _seed_account(store, provider.name)
        stale = _error(_call(client, "creds_sync", {"confirm": code}))
        assert stale["code"] == "sync_plan_stale"
        assert "Nothing was applied" in stale["message"]

        # So did the stored set.
        code = _ok(_call(client, "creds_sync", {}))["confirm"]
        _ok(_call(client, "creds_desired_set", {"text": LABEL_SET.replace("team", "crew")}))
        stale = _error(_call(client, "creds_sync", {"confirm": code}))
        assert stale["code"] == "sync_plan_stale"

        assert store.get_profile("echo-http/labels") is None
        assert store.bindings("echo-http").user == []
        assert upstream["seen"] == []


def test_new_secret_gets_a_form(tmp_path: Path, upstream) -> None:
    secret = "SENTINEL-SECRET-desired-1"
    with _http_client(tmp_path) as (client, store, _manager, _provider):
        _ok(_call(client, "creds_desired_set", {"text": ACCOUNT_SET}))
        plan = _ok(_call(client, "creds_sync", {}))
        assert plan["profiles"] == [
            {
                "profile": "echo-http/ada",
                "variant": "account",
                "action": "created",
                "secret_fields": ["ECHO_TOKEN"],
            }
        ]
        done = _ok(_call(client, "creds_sync", {"confirm": plan["confirm"]}))
        row = done["profiles"][0]
        assert row["form_url"].startswith("https://echo.example/creds/")
        assert row["expires_at"]
        assert "Send each form_url" in done["next"]
        profile = store.get_profile("echo-http/ada")
        assert profile.state == "awaiting_secrets"
        assert profile.managed_by == SYNC_MARK
        # The binding is made now; the profile is used once the form is sent.
        assert store.bindings("echo-http").user[0].profile_id == "echo-http/ada"
        assert upstream["seen"] == []

        # While the link is open, a new plan leaves it alone.
        waiting = _ok(_call(client, "creds_sync", {}))
        assert waiting["changes"] == 0
        assert waiting["profiles"][0]["action"] == "awaiting_form"

        _token, response = _submit_form(client, row["form_url"], {"ECHO_TOKEN": secret})
        assert response.status_code == 200, response.text
        assert upstream["seen"][0].headers["host"] == "ada.example"
        assert store.get_profile("echo-http/ada").state == "ready"
        assert _ok(_call(client, "creds_sync", {}))["profiles"][0]["action"] == "unchanged"

        for item in store.list_audit():
            assert secret not in json.dumps(item)
            assert row["form_url"].rsplit("/", 1)[1] not in json.dumps(item)


def test_changed_address_sends_a_rotate_form(tmp_path: Path, upstream) -> None:
    with _http_client(tmp_path) as (client, store, _manager, provider):
        profile_id = _seed_account(store, provider.name)
        text = SHARED.format(url="https://new.example", label="old-label")
        _ok(_call(client, "creds_desired_set", {"text": text}))
        plan = _ok(_call(client, "creds_sync", {}))
        assert plan["profiles"][0]["action"] == "secrets_requested"
        assert plan["profiles"][0]["changed"] == ["ECHO_URL"]
        done = _ok(_call(client, "creds_sync", {"confirm": plan["confirm"]}))
        form_url = done["profiles"][0]["form_url"]
        # The old address and token stay in use until the form is sent.
        current = store.get_profile(profile_id)
        assert current.public_fields["ECHO_URL"] == "https://old.example"
        assert current.state == "ready"
        assert current.managed_by == SYNC_MARK
        assert upstream["seen"] == []
        _token, response = _submit_form(client, form_url, {"ECHO_TOKEN": "rotated-token-value"})
        assert response.status_code == 200, response.text
        assert store.get_profile(profile_id).public_fields["ECHO_URL"] == "https://new.example"
        assert store.read_secrets(profile_id)["ECHO_TOKEN"] == "rotated-token-value"


def test_changed_label_keeps_the_secret(tmp_path: Path, upstream) -> None:
    with _http_client(tmp_path) as (client, store, _manager, provider):
        profile_id = _seed_account(store, provider.name)
        version = store.get_profile(profile_id).version
        text = SHARED.format(url="https://old.example", label="new-label")
        _ok(_call(client, "creds_desired_set", {"text": text}))
        plan = _ok(_call(client, "creds_sync", {}))
        assert plan["profiles"][0]["action"] == "updated"
        done = _ok(_call(client, "creds_sync", {"confirm": plan["confirm"]}))
        assert done["profiles"][0]["check"] == "ok"
        assert "form_url" not in done["profiles"][0]
        profile = store.get_profile(profile_id)
        assert profile.public_fields["ECHO_LABEL"] == "new-label"
        assert profile.version == version + 1
        assert store.read_secrets(profile_id)["ECHO_TOKEN"] == "previous-token-value"
        assert upstream["seen"][0].headers["authorization"] == "Bearer previous-token-value"


def test_prune_only_with_the_argument(tmp_path: Path, upstream) -> None:
    with _http_client(tmp_path) as (client, store, _manager, provider):
        manual = _seed_account(store, provider.name)
        store.create_profile(
            "echo-http/old",
            provider.name,
            "label",
            {"ECHO_LABEL": "old"},
            None,
            "x",
            managed_by=SYNC_MARK,
        )
        store.put_secrets("echo-http/old", {}, "x", version=1)
        store.bind(provider.name, "user:bob@example.com", manual, "x")
        store.bind(provider.name, "role:ops", "echo-http/old", "x")
        _ok(_call(client, "creds_desired_set", {"text": LABEL_SET}))

        kept = _ok(_call(client, "creds_sync", {}))
        assert kept["not_in_set"] == [
            {"profile": "echo-http/old", "managed": True, "action": "kept"},
            {"profile": manual, "managed": False, "action": "kept"},
        ]
        # Bindings the set does not name are shown, and are not changes.
        assert kept["bindings"] == [
            {"subject": "user:ann@example.com", "profile": "echo-http/labels", "action": "bound"},
            {"subject": "user:bob@example.com", "profile": manual, "action": "kept"},
            {"subject": "role:ops", "profile": "echo-http/old", "action": "kept", "position": 1},
        ]
        assert kept["changes"] == 2
        assert "1 profile written by sync" in kept["next"]
        assert "2 bindings" in kept["next"]
        _ok(_call(client, "creds_sync", {"confirm": kept["confirm"]}))
        assert store.get_profile("echo-http/old") is not None

        plan = _ok(_call(client, "creds_sync", {"prune": True}))
        assert '"prune": true' in plan["next"]
        assert plan["not_in_set"] == [
            {"profile": "echo-http/old", "managed": True, "action": "deleted"},
            {"profile": manual, "managed": False, "action": "kept"},
        ]
        unbound = sorted(row["subject"] for row in plan["bindings"])
        assert unbound == ["role:ops", "user:bob@example.com"]
        done = _ok(_call(client, "creds_sync", {"prune": True, "confirm": plan["confirm"]}))
        assert done["partial"] is False
        assert store.get_profile("echo-http/old") is None
        assert store.get_profile(manual) is not None
        found = store.bindings(provider.name)
        assert [item.subject for item in found.user] == ["user:ann@example.com"]
        assert found.role == []


def test_rejected_profile_is_skipped_and_the_rest_applied(tmp_path: Path, upstream) -> None:
    text = LABEL_SET + "  default: echo-http/labels\n"
    text = text.replace(
        "profiles:\n",
        "profiles:\n  echo-http/ada:\n    variant: account\n    fields:\n"
        "      ECHO_URL: https://ada.example\n      ECHO_ORG: acme\n      ECHO_LABEL: ada\n",
    )
    with _http_client(tmp_path) as (client, store, _manager, _provider):
        _ok(_call(client, "creds_desired_set", {"text": text}))
        plan = _ok(_call(client, "creds_sync", {}))
        upstream["status"] = 401
        done = _ok(_call(client, "creds_sync", {"confirm": plan["confirm"]}))
        assert done["partial"] is True
        actions = {row["profile"]: row["action"] for row in done["profiles"]}
        assert actions == {"echo-http/ada": "created", "echo-http/labels": "skipped"}
        assert [error["id"] for error in done["errors"]][0] == "check_failed"
        assert {row["action"] for row in done["bindings"]} == {"skipped"}
        assert store.get_profile("echo-http/labels") is None
        assert store.get_profile("echo-http/ada").state == "awaiting_secrets"


def test_form_unavailable_without_public_address(tmp_path: Path, upstream) -> None:
    with _pending_client(tmp_path) as (client, store, _manager, _provider):
        _ok(_call(client, "creds_desired_set", {"text": ACCOUNT_SET}))
        plan = _ok(_call(client, "creds_sync", {}))
        done = _ok(_call(client, "creds_sync", {"confirm": plan["confirm"]}))
        assert done["partial"] is True
        assert done["errors"][0]["id"] == "form_unavailable"
        assert "credsctl profile import echo-http/ada" in done["errors"][0]["message"]
        assert store.get_profile("echo-http/ada") is None


@contextmanager
def _pending_client(tmp_path: Path):
    provider = _account_provider()
    snapshot = json.loads((ECHO_HTTP / "tools.snapshot.json").read_text(encoding="utf-8"))
    catalog = build_catalog(snapshot, provider)
    names = frozenset(tool.name for tool in catalog.published_tools())
    yield from _open_client(
        tmp_path, provider, catalog, names, public_base_url="https://public-domain-pending.invalid"
    )


def test_only_box_admin_reaches_the_new_tools(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["policy-example", "--namespace", "echo", "--provider-dir", str(ECHO_DIR)])
    policy = yaml.safe_load(capsys.readouterr().out)
    new = ["echo_creds_desired_get", "echo_creds_desired_set", "echo_creds_sync"]
    for role, body in policy["roles"].items():
        reached = [
            name for name in new if any(fnmatch.fnmatchcase(name, item) for item in body["allow"])
        ]
        assert reached == (new if role == "echo_box_admin" else [])
    assert {"echo_creds_desired_set", "echo_creds_sync"} <= set(policy["requires_human_identity"])
    assert {"echo_creds_desired_set", "echo_creds_sync"} <= set(policy["critical_tools"])
