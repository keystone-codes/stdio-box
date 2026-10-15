"""Install-time ``BOX_*`` settings.

Later cards add the remaining settings to this module. This revision loads
only what installation and ``snapshot-tools`` need.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_TIMEOUT_DEFAULT = 240
_TIMEOUT_MIN = 30
_TIMEOUT_MAX = 1800
_CACHE_DEFAULT = Path("/cache")


class ConfigError(Exception):
    """One or more ``BOX_*`` values are missing or not valid."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


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
    provider_dir = _provider_dir(env, provider)
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


def _provider_dir(env: Mapping[str, str], provider: str) -> Path | None:
    raw = env.get("BOX_PROVIDER_DIR", "").strip()
    if raw:
        return Path(raw)
    if provider and _PROVIDER_NAME.fullmatch(provider):
        return Path("/opt/box/packages") / provider
    return None
