"""Process environment for one box.

HTTP settings (bearer, identity, store key) stay required for ``stdio-box serve``.
Install settings are loaded separately: ``snapshot-tools`` and ``stdio-box install``
do not need the door's secrets. ``BOX_COMMAND`` may be empty for those commands;
``launch`` requires it unless the debug child override is set.
"""

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_MIN_SECRET_BYTES = 32
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_TIMEOUT_DEFAULT = 240
_TIMEOUT_MIN = 30
_TIMEOUT_MAX = 1800
_CACHE_DEFAULT = Path("/cache")


class ConfigError(Exception):
    """One or more install ``BOX_*`` values are missing or not valid."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


@dataclass(frozen=True)
class Config:
    bearer_token: str
    identity_secret: str
    identity_audience: str
    public_base_url: str
    store_key: str
    provider: str
    provider_dir: str
    command: str
    port: int
    data_dir: str
    log_level: str
    allowed_hosts: tuple[str, ...]
    debug: bool = False
    child_argv_override: str | None = None


@dataclass(frozen=True)
class InstallSettings:
    command: str
    provider: str
    provider_dir: Path | None
    cache_dir: Path
    install_timeout_seconds: int
    pip_index_url: str
    npm_registry: str
    debug: bool
    child_argv_override: str


def load_config(environ: Mapping[str, str]) -> Config:
    """Read ``BOX_*`` settings. On any problem, print one line per name and exit 2."""
    config, errors = _parse(environ)
    if errors:
        for name in errors:
            print(f"config error: {name} is required", file=sys.stderr)
        raise SystemExit(2)
    assert config is not None
    return config


def load_settings(environ: Mapping[str, str] | None = None) -> InstallSettings:
    """Read install settings. ``BOX_COMMAND`` may be empty; ``launch`` requires it."""
    env = os.environ if environ is None else environ
    errors: list[str] = []
    timeout = _timeout(env.get("BOX_INSTALL_TIMEOUT_SECONDS", ""), errors)
    pip_index = _https_setting("BOX_PIP_INDEX_URL", env.get("BOX_PIP_INDEX_URL", ""), errors)
    npm_registry = _https_setting("BOX_NPM_REGISTRY", env.get("BOX_NPM_REGISTRY", ""), errors)
    provider = env.get("BOX_PROVIDER", "").strip()
    if provider and _PROVIDER_NAME.fullmatch(provider) is None:
        errors.append("BOX_PROVIDER must match ^[a-z][a-z0-9_-]{0,30}$")
    provider_dir = _install_provider_dir(env, provider)
    if errors:
        raise ConfigError(errors)
    return InstallSettings(
        command=env.get("BOX_COMMAND", "").strip(),
        provider=provider,
        provider_dir=provider_dir,
        cache_dir=_CACHE_DEFAULT,
        install_timeout_seconds=timeout,
        pip_index_url=pip_index,
        npm_registry=npm_registry,
        debug=env.get("BOX_DEBUG", "").strip() == "1",
        child_argv_override=env.get("BOX_CHILD_ARGV_OVERRIDE", "").strip(),
    )


def _parse(environ: Mapping[str, str]) -> tuple[Config | None, list[str]]:
    errors: list[str] = []

    bearer = environ.get("BOX_BEARER_TOKEN", "")
    if not _secret_ok(bearer):
        errors.append("BOX_BEARER_TOKEN")

    identity_secret = environ.get("BOX_IDENTITY_SECRET", "")
    if not _secret_ok(identity_secret):
        errors.append("BOX_IDENTITY_SECRET")

    audience = environ.get("BOX_IDENTITY_AUDIENCE", "").strip()
    if not audience:
        errors.append("BOX_IDENTITY_AUDIENCE")

    public_base_url = environ.get("BOX_PUBLIC_BASE_URL", "").strip()
    if not _https_base(public_base_url):
        errors.append("BOX_PUBLIC_BASE_URL")

    store_key = environ.get("BOX_STORE_KEY", "")
    if not _secret_ok(store_key):
        errors.append("BOX_STORE_KEY")

    provider = environ.get("BOX_PROVIDER", "").strip()
    if _PROVIDER_NAME.fullmatch(provider) is None:
        errors.append("BOX_PROVIDER")

    provider_dir = _provider_dir(environ, provider, errors)

    command = environ.get("BOX_COMMAND", "").strip()
    if not command:
        errors.append("BOX_COMMAND")

    port = _port(environ, errors)
    data_dir = environ.get("BOX_DATA_DIR", "").strip() or "/data"
    log_level = _log_level(environ, errors)
    allowed_hosts = _allowed_hosts(environ.get("BOX_ALLOWED_HOSTS", ""))
    debug, child_argv_override = read_spawner_settings(environ)

    if errors:
        return None, errors
    return (
        Config(
            bearer_token=bearer,
            identity_secret=identity_secret,
            identity_audience=audience,
            public_base_url=public_base_url,
            store_key=store_key,
            provider=provider,
            provider_dir=provider_dir,
            command=command,
            port=port,
            data_dir=data_dir,
            log_level=log_level,
            allowed_hosts=allowed_hosts,
            debug=debug,
            child_argv_override=child_argv_override,
        ),
        [],
    )


def read_spawner_settings(environ: Mapping[str, str]) -> tuple[bool, str | None]:
    """``BOX_DEBUG`` and ``BOX_CHILD_ARGV_OVERRIDE``. Neither is required."""
    override = environ.get("BOX_CHILD_ARGV_OVERRIDE")
    return environ.get("BOX_DEBUG") == "1", override or None


def _secret_ok(value: str) -> bool:
    return len(value.encode("utf-8")) >= _MIN_SECRET_BYTES


def _https_base(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def _provider_dir(environ: Mapping[str, str], provider: str, errors: list[str]) -> str:
    configured = environ.get("BOX_PROVIDER_DIR", "").strip()
    if configured:
        directory = configured
    elif "BOX_PROVIDER" in errors:
        return ""
    else:
        directory = f"/opt/box/packages/{provider}"
    root_has_descriptor = _is_file(directory, "provider.yaml")
    root_has_snapshot = _is_file(directory, "tools.snapshot.json")
    if not root_has_descriptor or not root_has_snapshot:
        errors.append("BOX_PROVIDER_DIR")
    return directory


def _is_file(directory: str, name: str) -> bool:
    return (Path(directory) / name).is_file()


def _port(environ: Mapping[str, str], errors: list[str]) -> int:
    raw = environ.get("BOX_PORT", "").strip()
    if raw == "":
        return 8081
    try:
        port = int(raw)
    except ValueError:
        errors.append("BOX_PORT")
        return 8081
    if port < 1 or port > 65535:
        errors.append("BOX_PORT")
        return 8081
    return port


def _log_level(environ: Mapping[str, str], errors: list[str]) -> str:
    raw = environ.get("BOX_LOG_LEVEL", "").strip()
    if raw == "":
        return "INFO"
    level = raw.upper()
    if level not in _LOG_LEVELS:
        errors.append("BOX_LOG_LEVEL")
        return "INFO"
    return level


def _allowed_hosts(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _timeout(raw: str, errors: list[str]) -> int:
    text = raw.strip()
    if not text:
        return _TIMEOUT_DEFAULT
    try:
        value = int(text)
    except ValueError:
        errors.append(
            f"BOX_INSTALL_TIMEOUT_SECONDS must be an integer from {_TIMEOUT_MIN} to {_TIMEOUT_MAX}"
        )
        return _TIMEOUT_DEFAULT
    if value < _TIMEOUT_MIN or value > _TIMEOUT_MAX:
        errors.append(
            f"BOX_INSTALL_TIMEOUT_SECONDS must be an integer from {_TIMEOUT_MIN} to {_TIMEOUT_MAX}"
        )
    return value


def _https_setting(name: str, raw: str, errors: list[str]) -> str:
    text = raw.strip()
    if not text:
        return ""
    if not text.lower().startswith("https://") or any(char.isspace() for char in text):
        errors.append(f"{name} must start with https://")
        return ""
    return text


def _install_provider_dir(env: Mapping[str, str], provider: str) -> Path | None:
    raw = env.get("BOX_PROVIDER_DIR", "").strip()
    if raw:
        return Path(raw)
    if provider and _PROVIDER_NAME.fullmatch(provider):
        return Path("/opt/box/packages") / provider
    return None
