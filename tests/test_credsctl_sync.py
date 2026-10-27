"""credsctl sync: create, rotate, leave unchanged, prune, partial apply, and redaction."""

import json
import os
import secrets
import socket
from pathlib import Path

import httpx
import pytest
import respx

from stdio_box.cli import credsctl_main
from stdio_box.store.repo import Store

PROVIDER_DIR = Path(__file__).resolve().parent / "data" / "packages" / "atlassian"
ECHO_HTTP = Path(__file__).resolve().parent / "data" / "packages" / "echo-http"
TOKEN = "alexey-token-value"
BACKEND = "backend-token-value"
NEW_TOKEN = "alexey-new-token-value"
REJECTED = "rejected-token-value"
GOOD = "good-token-value"


@pytest.fixture
def store_key() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_key: str) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("BOX_DATA_DIR", str(data))
    monkeypatch.setenv("BOX_STORE_KEY", store_key)
    monkeypatch.setenv("BOX_PROVIDER", "atlassian")
    monkeypatch.setenv("BOX_PROVIDER_DIR", str(PROVIDER_DIR))
    return data


def _invoke(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as raised:
        credsctl_main(args)
    captured = capsys.readouterr()
    code = raised.value.code
    assert isinstance(code, int)
    return code, captured.out, captured.err


def _private(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _store(box: Path, key: str) -> Store:
    return Store.open(box, key)


def _three_profiles(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The stage example: a person, a service account, and a role account."""
    _private(directory / "alexey.token", TOKEN + "\n")
    _private(
        directory / "backend.env",
        "JIRA_URL=https://b.example.com\n"
        "JIRA_USERNAME=backend@example.com\n"
        f"JIRA_API_TOKEN={BACKEND}\n",
    )
    monkeypatch.setenv("SKYNET_JIRA_TOKEN", "skynet-token-value")
    path = directory / "profiles.yaml"
    path.write_text(
        """
version: 1
profiles:
  atlassian/alexey:
    description: Alexey
    fields:
      JIRA_URL: https://a.example.com
      JIRA_USERNAME: alexey@example.com
    secrets:
      JIRA_API_TOKEN: {file: alexey.token}
  atlassian/backend:
    env_file: backend.env
  atlassian/skynet:
    variant: jira_cloud
    fields:
      JIRA_URL: https://b.example.com
      JIRA_USERNAME: skynet@example.com
    secrets:
      JIRA_API_TOKEN: {env: SKYNET_JIRA_TOKEN}
bindings:
  users:
    Alexey@Example.com: atlassian/alexey
    svc:backend: atlassian/backend
    skynet@example.com: atlassian/skynet
  roles:
    - atlassian_writer: atlassian/skynet
""",
        encoding="utf-8",
    )
    return path


def _sync(path: Path, *extra: str) -> list[str]:
    return ["sync", "--file", str(path), "--no-check", *extra, "--json"]


def test_sync_creates_then_leaves_an_unchanged_box_alone(
    box: Path,
    store_key: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _three_profiles(box.parent, monkeypatch)
    code, out, err = _invoke(_sync(path), capsys)
    assert code == 0, out
    assert err == ""
    result = json.loads(out)["result"]
    assert [(row["profile"], row["action"]) for row in result["profiles"]] == [
        ("atlassian/alexey", "created"),
        ("atlassian/backend", "created"),
        ("atlassian/skynet", "created"),
    ]
    assert {row["subject"]: row["action"] for row in result["bindings"]} == {
        "user:alexey@example.com": "bound",
        "user:svc:backend": "bound",
        "user:skynet@example.com": "bound",
        "role:atlassian_writer": "bound",
    }
    for secret in (TOKEN, BACKEND, "skynet-token-value"):
        assert secret not in out

    store = _store(box, store_key)
    assert store.read_secrets("atlassian/alexey") == {"JIRA_API_TOKEN": TOKEN}
    assert store.get_profile("atlassian/alexey").managed_by == "credsctl sync"
    assert store.get_profile("atlassian/alexey").description == "Alexey"
    assert store.get_profile("atlassian/skynet").variant == "jira_cloud"
    found = store.bindings("atlassian")
    assert [(item.subject, item.position) for item in found.role] == [("role:atlassian_writer", 1)]
    before = store.revision()
    store.close()

    code, out, _err = _invoke(_sync(path), capsys)
    assert code == 0
    result = json.loads(out)["result"]
    assert {row["action"] for row in result["profiles"]} == {"unchanged"}
    assert result["bindings"] == []
    store = _store(box, store_key)
    assert store.revision() == before
    assert store.get_profile("atlassian/alexey").version == 1
    store.close()


def test_sync_rotates_a_changed_secret_and_keeps_its_bindings(
    box: Path,
    store_key: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _three_profiles(box.parent, monkeypatch)
    assert _invoke(_sync(path), capsys)[0] == 0
    _private(box.parent / "alexey.token", NEW_TOKEN + "\n")
    code, out, err = _invoke(_sync(path), capsys)
    assert code == 0
    rows = {row["profile"]: row["action"] for row in json.loads(out)["result"]["profiles"]}
    assert rows == {
        "atlassian/alexey": "rotated",
        "atlassian/backend": "unchanged",
        "atlassian/skynet": "unchanged",
    }
    assert NEW_TOKEN not in out and NEW_TOKEN not in err
    store = _store(box, store_key)
    assert store.read_secrets("atlassian/alexey") == {"JIRA_API_TOKEN": NEW_TOKEN}
    assert store.get_profile("atlassian/alexey").version == 2
    users = {item.subject: item.profile_id for item in store.bindings("atlassian").user}
    assert users["user:alexey@example.com"] == "atlassian/alexey"
    store.close()


def test_sync_takes_over_an_imported_profile_by_rotating_it(
    box: Path,
    store_key: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = _private(
        box.parent / "other.env",
        "JIRA_URL=https://a.example.com\nJIRA_USERNAME=alexey@example.com\n"
        "JIRA_API_TOKEN=old-token-value\n",
    )
    code, _out, _err = _invoke(
        ["profile", "import", "atlassian/alexey", "--from-file", str(other), "--no-check"], capsys
    )
    assert code == 0
    store = _store(box, store_key)
    assert store.get_profile("atlassian/alexey").managed_by is None
    store.close()
    path = _three_profiles(box.parent, monkeypatch)
    code, out, _err = _invoke(_sync(path), capsys)
    assert code == 0
    rows = {row["profile"]: row["action"] for row in json.loads(out)["result"]["profiles"]}
    assert rows["atlassian/alexey"] == "rotated"
    store = _store(box, store_key)
    assert store.get_profile("atlassian/alexey").managed_by == "credsctl sync"
    store.close()


def test_sync_without_prune_keeps_and_reports_what_is_not_in_the_file(
    box: Path,
    store_key: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _three_profiles(box.parent, monkeypatch)
    assert _invoke(_sync(path), capsys)[0] == 0
    manual = _private(
        box.parent / "manual.env",
        f"JIRA_URL=https://a.example.com\nJIRA_USERNAME=m@example.com\nJIRA_API_TOKEN={TOKEN}\n",
    )
    assert (
        _invoke(
            ["profile", "import", "atlassian/manual", "--from-file", str(manual), "--no-check"],
            capsys,
        )[0]
        == 0
    )
    assert _invoke(["bind", "role:reader", "atlassian/manual"], capsys)[0] == 0
    assert _invoke(["bind", "role:first", "atlassian/manual", "--position", "1"], capsys)[0] == 0
    assert _invoke(["bind", "default", "atlassian/manual"], capsys)[0] == 0

    code, out, _err = _invoke(_sync(path), capsys)
    assert code == 0
    result = json.loads(out)["result"]
    assert result["not_in_file"] == [
        {"profile": "atlassian/manual", "managed": False, "action": "kept"}
    ]
    assert result["bindings"] == [
        {
            "subject": "role:atlassian_writer",
            "profile": "atlassian/skynet",
            "action": "moved",
            "position": 1,
        },
        {"subject": "role:first", "profile": "atlassian/manual", "action": "moved", "position": 2},
        {"subject": "role:reader", "profile": "atlassian/manual", "action": "kept", "position": 3},
        {"subject": "default", "profile": "atlassian/manual", "action": "kept"},
    ]
    store = _store(box, store_key)
    found = store.bindings("atlassian")
    assert [item.subject for item in found.role] == [
        "role:atlassian_writer",
        "role:first",
        "role:reader",
    ]
    assert found.default is not None
    store.close()


def test_sync_prune_deletes_only_profiles_sync_wrote_and_every_unlisted_binding(
    box: Path,
    store_key: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _three_profiles(box.parent, monkeypatch)
    assert _invoke(_sync(path), capsys)[0] == 0
    manual = _private(
        box.parent / "manual.env",
        f"JIRA_URL=https://a.example.com\nJIRA_USERNAME=m@example.com\nJIRA_API_TOKEN={TOKEN}\n",
    )
    assert (
        _invoke(
            ["profile", "import", "atlassian/manual", "--from-file", str(manual), "--no-check"],
            capsys,
        )[0]
        == 0
    )
    assert _invoke(["bind", "role:reader", "atlassian/manual"], capsys)[0] == 0
    assert _invoke(["bind", "user:ann@example.com", "atlassian/manual"], capsys)[0] == 0
    assert _invoke(["bind", "default", "atlassian/manual"], capsys)[0] == 0

    smaller = box.parent / "smaller.yaml"
    text = path.read_text(encoding="utf-8")
    start = text.index("  atlassian/backend:")
    end = text.index("  atlassian/skynet:")
    text = text[:start] + text[end:]
    text = text.replace("    svc:backend: atlassian/backend\n", "")
    smaller.write_text(text, encoding="utf-8")

    store = _store(box, store_key)
    before = store.revision()
    store.close()
    code, out, _err = _invoke(_sync(smaller, "--prune", "--dry-run"), capsys)
    assert code == 0
    planned = json.loads(out)["result"]
    assert planned["dry_run"] is True
    store = _store(box, store_key)
    assert store.revision() == before
    store.close()

    code, out, _err = _invoke(_sync(smaller, "--prune"), capsys)
    assert code == 0, out
    result = json.loads(out)["result"]
    assert result["not_in_file"] == planned["not_in_file"]
    assert result["bindings"] == planned["bindings"]
    assert sorted(result["not_in_file"], key=lambda row: row["profile"]) == [
        {"profile": "atlassian/backend", "managed": True, "action": "deleted"},
        {"profile": "atlassian/manual", "managed": False, "action": "kept"},
    ]
    assert {row["subject"] for row in result["bindings"] if row["action"] == "unbound"} == {
        "user:svc:backend",
        "user:ann@example.com",
        "role:reader",
        "default",
    }
    store = _store(box, store_key)
    assert store.get_profile("atlassian/backend") is None
    assert store.get_profile("atlassian/manual") is not None
    found = store.bindings("atlassian")
    assert [item.subject for item in found.user] == [
        "user:alexey@example.com",
        "user:skynet@example.com",
    ]
    assert [item.subject for item in found.role] == ["role:atlassian_writer"]
    assert found.default is None
    store.close()


def test_sync_can_bind_a_profile_that_is_only_in_the_box(
    box: Path,
    store_key: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manual = _private(
        box.parent / "manual.env",
        f"JIRA_URL=https://a.example.com\nJIRA_USERNAME=m@example.com\nJIRA_API_TOKEN={TOKEN}\n",
    )
    assert (
        _invoke(
            ["profile", "import", "atlassian/manual", "--from-file", str(manual), "--no-check"],
            capsys,
        )[0]
        == 0
    )
    path = box.parent / "bind-only.yaml"
    path.write_text("version: 1\nbindings:\n  default: atlassian/manual\n", encoding="utf-8")
    code, out, _err = _invoke(_sync(path, "--prune"), capsys)
    assert code == 0
    result = json.loads(out)["result"]
    assert result["bindings"] == [
        {"subject": "default", "profile": "atlassian/manual", "action": "bound"}
    ]
    assert result["not_in_file"] == [
        {"profile": "atlassian/manual", "managed": False, "action": "kept"}
    ]
    store = _store(box, store_key)
    assert store.bindings("atlassian").default.profile_id == "atlassian/manual"
    store.close()


@pytest.mark.parametrize(
    ("body", "error_id", "message"),
    [
        (
            "    fields:\n      JIRA_URL: https://a.example.com\n"
            "      JIRA_USERNAME: a@example.com\n      JIRA_API_TOKEN: inline-secret-value\n",
            "secret_in_file",
            "JIRA_API_TOKEN is a secret field",
        ),
        (
            "    fields:\n      JIRA_URL: https://a.example.com\n"
            "      JIRA_USERNAME: a@example.com\n"
            "    secrets:\n      JIRA_API_TOKEN: inline-secret-value\n",
            "invalid_argument",
            "must be {file: <path>} or {env: <NAME>}",
        ),
        (
            "    fields:\n      JIRA_URL: https://a.example.com\n"
            "      JIRA_USERNAME: a@example.com\n"
            "    secrets:\n      JIRA_API_TOKEN: {env: BOX_STORE_KEY}\n",
            "invalid_argument",
            "BOX_ variables cannot be used",
        ),
        (
            "    fields:\n      JIRA_URL: https://a.example.com\n"
            "      JIRA_USERNAME: a@example.com\n"
            "    secrets:\n      JIRA_API_TOKEN: {file: loose.token}\n",
            "invalid_argument",
            "file must be mode 600",
        ),
        (
            "    fields:\n      JIRA_URL: https://a.example.com\n"
            "    secrets:\n      JIRA_API_TOKEN: {env: SOME_TOKEN}\n",
            "variant_ambiguous",
            "no variant has exactly these fields",
        ),
    ],
)
def test_sync_rejects_a_bad_file_and_writes_nothing(
    box: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
    error_id: str,
    message: str,
) -> None:
    monkeypatch.setenv("SOME_TOKEN", "env-secret-value")
    _private(box.parent / "loose.token", "loose-secret-value\n", mode=0o644)
    path = box.parent / "bad.yaml"
    path.write_text("version: 1\nprofiles:\n  atlassian/a:\n" + body, encoding="utf-8")
    code, out, err = _invoke(_sync(path), capsys)
    assert code == 3
    payload = json.loads(out)
    assert payload["error"]["id"] == error_id
    assert message in payload["error"]["message"]
    assert payload["error"]["message"].startswith("profiles.atlassian/a: ")
    for secret in ("inline-secret-value", "loose-secret-value", "env-secret-value"):
        assert secret not in out and secret not in err
    assert not (box / "box.sqlite3").exists()


def test_sync_binding_to_an_unknown_profile_exits_3(
    box: Path, store_key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = box.parent / "unknown.yaml"
    path.write_text(
        "version: 1\nbindings:\n  roles:\n    - writer: atlassian/nobody\n", encoding="utf-8"
    )
    code, out, _err = _invoke(_sync(path), capsys)
    assert code == 3
    assert json.loads(out)["error"]["id"] == "profile_not_found"
    store = _store(box, store_key)
    assert store.bindings("atlassian").role == []
    store.close()


def _public_dns(_host, _port, *_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]


def _echo_check(request: httpx.Request) -> httpx.Response:
    if request.headers.get("Authorization") == f"Bearer {GOOD}":
        return httpx.Response(200, json={"name": "good"})
    return httpx.Response(401)


def test_sync_skips_a_rejected_profile_applies_the_rest_and_exits_6(
    box: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOX_PROVIDER", "echo-http")
    monkeypatch.setenv("BOX_PROVIDER_DIR", str(ECHO_HTTP))
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    monkeypatch.setenv("GOOD_TOKEN", GOOD)
    monkeypatch.setenv("BAD_TOKEN", REJECTED)
    path = box.parent / "echo.yaml"
    path.write_text(
        """
version: 1
profiles:
  echo-http/good:
    fields: {ECHO_URL: "https://echo.example", ECHO_ORG: acme}
    secrets: {ECHO_TOKEN: {env: GOOD_TOKEN}}
  echo-http/bad:
    fields: {ECHO_URL: "https://echo.example", ECHO_ORG: acme}
    secrets: {ECHO_TOKEN: {env: BAD_TOKEN}}
bindings:
  users:
    svc:good: echo-http/good
    svc:bad: echo-http/bad
""",
        encoding="utf-8",
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=_echo_check)
        code, out, err = _invoke(["sync", "--file", str(path), "--json"], capsys)
    assert code == 6
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["id"] == "partially_applied"
    result = payload["result"]
    rows = {row["profile"]: row for row in result["profiles"]}
    assert rows["echo-http/good"]["action"] == "created"
    assert rows["echo-http/good"]["check"] == "ok"
    assert rows["echo-http/bad"]["action"] == "skipped"
    assert rows["echo-http/bad"]["check"] == "rejected"
    assert [(row["id"], row.get("subject")) for row in result["errors"]] == [
        ("check_failed", None),
        ("profile_skipped", "user:svc:bad"),
    ]
    assert "did not accept these values (HTTP 401)" in result["errors"][0]["message"]
    assert GOOD not in out and REJECTED not in out and REJECTED not in err

    store = Store.open(box, os.environ["BOX_STORE_KEY"])
    assert store.get_profile("echo-http/bad") is None
    assert store.get_profile("echo-http/good").last_check_ok is True
    users = {item.subject: item.profile_id for item in store.bindings("echo-http").user}
    assert users == {"user:svc:good": "echo-http/good"}
    store.close()


def test_sync_human_output_lists_changes_without_values(
    box: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _three_profiles(box.parent, monkeypatch)
    code, out, err = _invoke(["sync", "--file", str(path), "--no-check", "--dry-run"], capsys)
    assert code == 0
    assert err == ""
    assert out.splitlines()[0] == "dry run: nothing was written"
    assert "profile  atlassian/alexey  created" in out
    assert "binding  role:atlassian_writer  bound  atlassian/skynet  position 1" in out
    for secret in (TOKEN, BACKEND, "skynet-token-value"):
        assert secret not in out


def test_profile_list_shows_managed_by(
    box: Path,
    store_key: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _three_profiles(box.parent, monkeypatch)
    assert _invoke(_sync(path), capsys)[0] == 0
    store = _store(box, store_key)
    store.create_profile("atlassian/manual", "atlassian", "jira_cloud", {}, None, "tester")
    store.close()
    code, out, _err = _invoke(["profile", "list"], capsys)
    assert code == 0
    lines = out.splitlines()
    assert "atlassian/alexey  jira_cloud  ready  managed_by: credsctl sync" in lines
    assert "atlassian/manual  jira_cloud  awaiting_secrets  managed_by: -" in lines


@pytest.mark.parametrize(
    ("args", "first"),
    [
        (["sync", "--help"], "usage: credsctl sync --file <path|-> [--dry-run]"),
        (["profile", "import", "-h"], "usage: credsctl profile import <id>"),
        (["profile", "--help"], "usage: credsctl profile list"),
        (["--help"], "usage: credsctl [--json] <command> [<args>]"),
    ],
)
def test_help_exits_zero(args: list[str], first: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _invoke(args, capsys)
    assert code == 0
    assert err == ""
    assert out.startswith(first)


def test_help_in_json_and_for_an_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = _invoke(["--json", "sync", "--help"], capsys)
    assert code == 0
    assert "--prune" in json.loads(out)["result"]["usage"]
    code, _out, err = _invoke(["nope", "--help"], capsys)
    assert code == 2
    assert err.startswith("usage: credsctl [--json]")
