"""credsctl exit codes, variant selection, and redaction of field values."""

import io
import json
import os
import secrets
import socket
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
import respx

from stdio_box.cli import credsctl_main
from stdio_box.store.repo import Store

PROVIDER_DIR = Path(__file__).resolve().parent / "data" / "packages" / "atlassian"
ECHO_HTTP = Path(__file__).resolve().parent / "data" / "packages" / "echo-http"
REJECTED = "rejected-token-value"
TOKEN = "test-token-value"
QUOTED = '"quoted-token"'


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


def _write_env(path: Path, text: str, mode: int = 0o600) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _jira_cloud() -> str:
    return (
        "# imported account\n"
        "\n"
        "JIRA_URL=https://example.atlassian.net\n"
        "JIRA_USERNAME=person@example.com\n"
        f"JIRA_API_TOKEN={TOKEN}\n"
    )


def _cloud() -> str:
    return (
        _jira_cloud()
        + "CONFLUENCE_URL=https://example.atlassian.net/wiki\n"
        + "CONFLUENCE_USERNAME=person@example.com\n"
        + f"CONFLUENCE_API_TOKEN={TOKEN}\n"
    )


def _json(out: str) -> dict:
    return json.loads(out)


def test_import_from_stdin_and_from_a_mode_600_file(
    box: Path, store_key: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(_jira_cloud()))
    code, out, err = _invoke(
        ["profile", "import", "atlassian/stdin", "--from-file", "-", "--no-check", "--json"],
        capsys,
    )
    assert code == 0
    assert err == ""
    payload = _json(out)
    assert payload["ok"] is True
    assert payload["code"] == 0
    assert payload["result"]["variant"] == "jira_cloud"
    assert payload["result"]["check"] == "skipped"
    assert TOKEN not in out and TOKEN not in err

    path = box.parent / "jira.env"
    _write_env(path, _cloud())
    code, out, err = _invoke(
        ["profile", "import", "atlassian/both", "--from-file", str(path), "--no-check", "--json"],
        capsys,
    )
    assert code == 0
    assert _json(out)["result"]["variant"] == "cloud"
    assert TOKEN not in out

    server = "JIRA_URL=https://jira.example.com\nJIRA_PERSONAL_TOKEN=server-token-value\n"
    server_path = box.parent / "server.env"
    _write_env(server_path, server)
    code, out, _err = _invoke(
        [
            "profile",
            "import",
            "atlassian/server",
            "--from-file",
            str(server_path),
            "--no-check",
            "--json",
        ],
        capsys,
    )
    assert _json(out)["result"]["variant"] == "jira_server"
    assert "server-token-value" not in out

    quoted = box.parent / "quoted.env"
    _write_env(quoted, _jira_cloud().replace(TOKEN, QUOTED))
    code, out, _err = _invoke(
        [
            "profile",
            "import",
            "atlassian/quoted",
            "--from-file",
            str(quoted),
            "--no-check",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert _json(out)["result"]["check"] == "skipped"
    assert "quoted-token" not in out

    store = Store.open(box, store_key, field_names=["JIRA_API_TOKEN"])
    assert store.read_secrets("atlassian/stdin") == {"JIRA_API_TOKEN": TOKEN}
    assert store.read_secrets("atlassian/quoted") == {"JIRA_API_TOKEN": QUOTED}
    listed_code, listed, listed_err = _invoke(["profile", "list", "--json"], capsys)
    assert listed_code == 0
    assert TOKEN not in listed and QUOTED not in listed and TOKEN not in listed_err
    audit_code, audit_out, audit_err = _invoke(["audit", "--json"], capsys)
    assert audit_code == 0
    assert TOKEN not in audit_out and "quoted-token" not in audit_out
    assert TOKEN not in audit_err
    store.close()


def test_variant_ambiguous_unknown_fields_and_if_absent(
    box: Path, store_key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    ambiguous = box.parent / "ambiguous.env"
    _write_env(
        ambiguous,
        "JIRA_URL=https://example.atlassian.net\nCONFLUENCE_PERSONAL_TOKEN=confluence-token-value\n",
    )
    code, out, err = _invoke(
        ["profile", "import", "atlassian/odd", "--from-file", str(ambiguous), "--json"], capsys
    )
    assert code == 3
    payload = _json(out)
    assert payload["error"]["id"] == "variant_ambiguous"
    names = [item["name"] for item in payload["result"]["variants"]]
    assert "jira_cloud" in names and "cloud" in names and "jira_server" in names
    assert "confluence-token-value" not in out and "confluence-token-value" not in err

    unknown = box.parent / "unknown.env"
    _write_env(unknown, _jira_cloud() + "JIRA_SSL_VERIFY=true\n")
    code, out, err = _invoke(
        ["profile", "import", "atlassian/shared", "--from-file", str(unknown), "--json"], capsys
    )
    assert code == 3
    assert _json(out)["error"]["id"] == "unknown_fields"
    assert _json(out)["result"]["unknown_fields"] == ["JIRA_SSL_VERIFY"]
    assert TOKEN not in out and TOKEN not in err
    assert not (box / "box.sqlite3").exists()

    code, out, _err = _invoke(
        [
            "profile",
            "import",
            "atlassian/shared",
            "--from-file",
            str(unknown),
            "--ignore-unknown",
            "--no-check",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert _json(out)["result"]["ignored"] == ["JIRA_SSL_VERIFY"]
    assert _json(out)["result"]["variant"] == "jira_cloud"
    assert TOKEN not in out

    store = Store.open(box, store_key)
    before = store.revision()
    assert store.read_secrets("atlassian/shared") == {"JIRA_API_TOKEN": TOKEN}
    store.close()
    code, out, _err = _invoke(
        [
            "profile",
            "import",
            "atlassian/shared",
            "--from-file",
            str(unknown),
            "--if-absent",
            "--ignore-unknown",
            "--json",
        ],
        capsys,
    )
    assert code == 4
    assert _json(out)["error"]["id"] == "profile_exists"
    store = Store.open(box, store_key)
    assert store.revision() == before
    assert store.read_secrets("atlassian/shared") == {"JIRA_API_TOKEN": TOKEN}
    store.close()


def test_loose_file_is_rejected(box: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = box.parent / "loose.env"
    _write_env(path, _jira_cloud(), mode=0o644)
    code, out, err = _invoke(
        ["profile", "import", "atlassian/shared", "--from-file", str(path), "--json"], capsys
    )
    assert code == 3
    assert "file must be mode 600" in out
    assert TOKEN not in out and TOKEN not in err
    assert not (box / "box.sqlite3").exists()


def test_exit_codes_usage_and_bad_input(box: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = _invoke(["no-such-command"], capsys)
    assert code == 2
    assert "usage: credsctl" in err

    code, out, _err = _invoke(["bind", "not a subject", "atlassian/shared", "--json"], capsys)
    assert code == 3
    assert _json(out)["error"]["id"] == "invalid_argument"


def test_newer_store_exits_5(box: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conn = sqlite3.connect(box / "box.sqlite3")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '99')")
    conn.commit()
    conn.close()
    code, out, _err = _invoke(["store", "status", "--json"], capsys)
    assert code == 5
    assert _json(out)["error"]["id"] == "store_schema_newer"


def test_bind_position_and_bindings_json_order(
    box: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = box.parent / "jira.env"
    _write_env(path, _jira_cloud())
    code, _out, _err = _invoke(
        ["profile", "import", "atlassian/shared", "--from-file", str(path), "--no-check", "--json"],
        capsys,
    )
    assert code == 0
    for subject in ("role:a", "role:b", "role:c"):
        code, _out, _err = _invoke(["bind", subject, "atlassian/shared", "--json"], capsys)
        assert code == 0
    code, out, _err = _invoke(
        ["bind", "role:d", "atlassian/shared", "--position", "1", "--json"], capsys
    )
    assert code == 0
    assert _json(out)["result"]["position"] == 1
    code, _out, _err = _invoke(["bind", "user:svc:backend", "atlassian/shared", "--json"], capsys)
    assert code == 0
    code, _out, _err = _invoke(["bind", "default", "atlassian/shared", "--json"], capsys)
    assert code == 0

    code, out, err = _invoke(["bindings", "--json"], capsys)
    assert code == 0
    assert err == ""
    assert out.index('"user"') < out.index('"role"') < out.index('"default"')
    result = _json(out)["result"]
    assert result["user"] == [{"subject": "user:svc:backend", "profile": "atlassian/shared"}]
    assert [item["subject"] for item in result["role"]] == [
        "role:d",
        "role:a",
        "role:b",
        "role:c",
    ]
    assert [item["position"] for item in result["role"]] == [1, 2, 3, 4]
    assert result["default"] == {"profile": "atlassian/shared"}
    assert TOKEN not in out


def test_status_before_the_wrapper_writes_a_row(
    box: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = _invoke(["status", "--json"], capsys)
    assert code == 0
    assert _json(out)["result"] == {"status": "not running"}


def test_root_drops_privileges_before_opening_the_store(
    box: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("setgroups", tuple(groups))))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("setgid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("setuid", uid)))
    code, _out, _err = _invoke(["store", "status", "--json"], capsys)
    assert code == 0
    assert calls == [("setgroups", ()), ("setgid", 10001), ("setuid", 10001)]


def _echo_env(path: Path) -> None:
    _write_env(
        path,
        f"ECHO_URL=https://echo.example\nECHO_TOKEN={REJECTED}\nECHO_ORG=acme\n",
    )


def _public_dns(_host, _port, *_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]


def test_import_rejected_token_writes_nothing(
    box: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOX_PROVIDER", "echo-http")
    monkeypatch.setenv("BOX_PROVIDER_DIR", str(ECHO_HTTP))
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    path = box.parent / "echo.env"
    _echo_env(path)
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(401))
        code, out, err = _invoke(
            ["profile", "import", "echo-http/main", "--from-file", str(path), "--json"],
            capsys,
        )
    assert code == 1
    payload = _json(out)
    assert payload["error"]["id"] == "check_failed"
    assert "did not accept these values (HTTP 401)" in payload["error"]["message"]
    assert REJECTED not in out and REJECTED not in err
    store = Store.open(box, os.environ["BOX_STORE_KEY"], field_names=["ECHO_TOKEN"])
    assert store.get_profile("echo-http/main") is None
    store.close()


def test_import_no_check_writes_without_a_check(
    box: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOX_PROVIDER", "echo-http")
    monkeypatch.setenv("BOX_PROVIDER_DIR", str(ECHO_HTTP))
    path = box.parent / "echo.env"
    _echo_env(path)
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(401))
        code, out, err = _invoke(
            [
                "profile",
                "import",
                "echo-http/main",
                "--from-file",
                str(path),
                "--no-check",
                "--json",
            ],
            capsys,
        )
        assert mock.calls.call_count == 0
    assert code == 0
    assert err == ""
    assert _json(out)["result"]["check"] == "skipped"
    assert REJECTED not in out
    store = Store.open(box, os.environ["BOX_STORE_KEY"])
    profile = store.get_profile("echo-http/main")
    assert profile is not None
    assert profile.last_check_ok is None
    assert store.read_secrets("echo-http/main") == {"ECHO_TOKEN": REJECTED}
    store.close()
