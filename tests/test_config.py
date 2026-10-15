"""BOX_* settings: one stderr line and exit 2 for each problem."""

import os
from pathlib import Path

import pytest

from stdio_box.cli import main
from stdio_box.config import load_config

_REQUIRED = (
    "BOX_BEARER_TOKEN",
    "BOX_IDENTITY_SECRET",
    "BOX_IDENTITY_AUDIENCE",
    "BOX_PUBLIC_BASE_URL",
    "BOX_STORE_KEY",
    "BOX_PROVIDER",
    "BOX_COMMAND",
)


def _touch(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "provider.yaml").write_text("name: echo\n", encoding="utf-8")
    (directory / "tools.snapshot.json").write_text("{}\n", encoding="utf-8")


def _environ(directory: Path, **overrides: str) -> dict[str, str]:
    values = {
        "BOX_BEARER_TOKEN": "b" * 32,
        "BOX_IDENTITY_SECRET": "s" * 32,
        "BOX_IDENTITY_AUDIENCE": "echo",
        "BOX_PUBLIC_BASE_URL": "https://echo.example",
        "BOX_STORE_KEY": "k" * 32,
        "BOX_PROVIDER": "echo",
        "BOX_PROVIDER_DIR": str(directory),
        "BOX_COMMAND": "uvx echo-server==0.0.0",
    }
    values.update(overrides)
    return values


def test_each_missing_setting_is_its_own_line(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        load_config({})
    assert exc.value.code == 2
    lines = capsys.readouterr().err.splitlines()
    assert lines == [f"config error: {name} is required" for name in _REQUIRED]


def test_invalid_values_use_the_same_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _touch(tmp_path)
    environ = _environ(
        tmp_path,
        BOX_BEARER_TOKEN="b" * 31,
        BOX_PUBLIC_BASE_URL="http://echo.example",
        BOX_PROVIDER="Echo",
        BOX_PORT="no",
        BOX_LOG_LEVEL="loud",
    )
    with pytest.raises(SystemExit) as exc:
        load_config(environ)
    assert exc.value.code == 2
    text = capsys.readouterr().err
    for name in (
        "BOX_BEARER_TOKEN",
        "BOX_PUBLIC_BASE_URL",
        "BOX_PROVIDER",
        "BOX_PORT",
        "BOX_LOG_LEVEL",
    ):
        assert f"config error: {name} is required" in text
    assert "b" * 31 not in text


def test_pending_public_domain_and_defaults(tmp_path: Path) -> None:
    _touch(tmp_path)
    config = load_config(
        _environ(tmp_path, BOX_PUBLIC_BASE_URL="https://public-domain-pending.invalid")
    )
    assert config.public_base_url == "https://public-domain-pending.invalid"
    assert config.port == 8081
    assert config.data_dir == "/data"
    assert config.log_level == "INFO"
    assert config.allowed_hosts == ()
    assert config.allow_http_urls is False
    assert config.allow_private_urls is False
    assert config.allowed_url_hosts == ()
    assert config.form_ttl_seconds == 900
    assert config.provider_dir == str(tmp_path)

    hosts = load_config(_environ(tmp_path, BOX_ALLOWED_HOSTS=" a.example, ,b.example "))
    assert hosts.allowed_hosts == ("a.example", "b.example")
    assert hosts.log_level == "INFO"


def test_url_and_form_settings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _touch(tmp_path)
    config = load_config(
        _environ(
            tmp_path,
            BOX_ALLOW_HTTP_URLS="true",
            BOX_ALLOW_PRIVATE_URLS="TRUE",
            BOX_ALLOWED_URL_HOSTS=" *.atlassian.net, ,example.com ",
            BOX_FORM_TTL_SECONDS="60",
        )
    )
    assert config.allow_http_urls is True
    assert config.allow_private_urls is True
    assert config.allowed_url_hosts == ("*.atlassian.net", "example.com")
    assert config.form_ttl_seconds == 60

    with pytest.raises(SystemExit) as exc:
        load_config(_environ(tmp_path, BOX_FORM_TTL_SECONDS="59"))
    assert exc.value.code == 2
    assert "config error: BOX_FORM_TTL_SECONDS is required" in capsys.readouterr().err


def test_missing_provider_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    environ = _environ(tmp_path)
    del environ["BOX_PROVIDER_DIR"]
    with pytest.raises(SystemExit) as exc:
        load_config(environ)
    assert exc.value.code == 2
    assert "config error: BOX_PROVIDER_DIR is required" in capsys.readouterr().err


def test_serve_exits_on_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in list(os.environ):
        if key.startswith("BOX_"):
            monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["serve"])
    assert exc.value.code == 2
    assert "config error: BOX_BEARER_TOKEN is required" in capsys.readouterr().err
