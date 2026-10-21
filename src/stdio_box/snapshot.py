"""Capture ``tools/list`` by installing the locked server and starting it once."""

import hashlib
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from stdio_box.boxcommand import BoxCommand, BoxCommandError, parse_box_command
from stdio_box.config import ConfigError, InstallSettings, load_settings
from stdio_box.install import (
    INSTALLER_UID,
    InstallFailed,
    configure_install_logging,
    ensure_installed,
    installer_environment,
    log_event,
    mask_install_text,
    setuid_available,
)
from stdio_box.provider import Provider, ProviderError, load_provider

_USAGE = (
    "usage: stdio-box snapshot-tools [--provider-dir DIR] [--out PATH|-] "
    "[--check] [--env-file PATH]"
)
_INHERITED = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")
_CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"


def snapshot_main(argv: list[str], environ: Mapping[str, str] | None = None) -> int:
    """Install into a temporary cache, call ``tools/list``, and write or check the snapshot."""
    configure_install_logging()
    try:
        flags = _flags(argv)
    except ValueError:
        print(_USAGE, file=sys.stderr)
        return 2
    source = os.environ if environ is None else environ
    try:
        settings = load_settings(source)
    except ConfigError as exc:
        for message in exc.messages:
            print(f"config error: {message}", file=sys.stderr)
        return 2
    provider_dir = flags.provider_dir or settings.provider_dir
    if provider_dir is None:
        print("config error: BOX_PROVIDER is required", file=sys.stderr)
        return 2
    try:
        provider = load_provider(provider_dir)
        command = parse_box_command(settings.command or provider.server.command)
        discovery = _discovery_env(provider, flags.env_file)
        tools = _capture(provider, provider_dir, settings, command, discovery)
    except (BoxCommandError, ProviderError, InstallFailed, ConfigError) as exc:
        return _failure(exc)
    snapshot = build_snapshot(provider, tools)
    if flags.out is not None:
        _write_snapshot(snapshot, flags.out)
    if flags.check:
        return _check(provider_dir / "tools.snapshot.json", snapshot)
    if flags.out is None:
        _write_snapshot(snapshot, "-")
    return 0


def build_snapshot(provider: Provider, tools: list[dict]) -> dict:
    """The snapshot document: ``tools`` as the server listed them, no rules applied."""
    return {
        "schema_version": 1,
        "server": {"command": provider.server.command},
        "taken_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "tools": tools,
    }


def render_snapshot(snapshot: dict) -> str:
    """The exact text ``snapshot-tools`` writes."""
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def snapshot_sha256(snapshot: object) -> str:
    """SHA-256 of the canonical JSON without ``taken_at``.

    Canonical is ``json.dumps(..., sort_keys=True, separators=(",", ":"),
    ensure_ascii=False)`` encoded as UTF-8. Two snapshots of the same server
    taken at different times have the same hash.
    """
    canonical = json.dumps(
        _without_taken_at(snapshot), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def listed_tools(listed: object) -> list[dict]:
    """``tools/list`` result as plain JSON objects, the way the snapshot stores them."""
    return [tool.model_dump(mode="json", exclude_none=True) for tool in listed.tools]


class _Flags:
    def __init__(self) -> None:
        self.provider_dir: Path | None = None
        self.out: str | None = None
        self.check = False
        self.env_file: Path | None = None


def _flags(argv: list[str]) -> _Flags:
    flags = _Flags()
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag == "--check":
            flags.check = True
            index += 1
            continue
        if flag in {"--provider-dir", "--out", "--env-file"}:
            if index + 1 >= len(argv):
                raise ValueError(flag)
            value = argv[index + 1]
            if flag == "--provider-dir":
                flags.provider_dir = Path(value)
            elif flag == "--out":
                flags.out = value
            else:
                flags.env_file = Path(value)
            index += 2
            continue
        raise ValueError(flag)
    return flags


def _capture(
    provider: Provider,
    provider_dir: Path,
    settings: InstallSettings,
    command: BoxCommand,
    discovery: dict[str, str],
) -> list[dict]:
    secrets = tuple(value for value in discovery.values() if len(value) >= 6)
    secrets += tuple(value for _, value in provider.server.static_env if len(value) >= 6)
    with tempfile.TemporaryDirectory(prefix="stdio-box-home-") as home:
        env = _child_environment(provider, discovery, Path(home))
        override = settings.child_argv_override if settings.debug else ""
        if override:
            argv0, args = _override_argv(override)
            return _list_tools(argv0, args, env, secrets)
        run_as = INSTALLER_UID if setuid_available() else None
        if run_as is None:
            log_event("install_isolation_unavailable")
        with tempfile.TemporaryDirectory(prefix="stdio-box-snapshot-") as cache:
            cache_root = Path(cache)
            installed = ensure_installed(
                command,
                provider,
                provider_dir,
                cache_root,
                run_as=run_as,
                timeout=settings.install_timeout_seconds,
                env=installer_environment(
                    cache_root,
                    pip_index_url=settings.pip_index_url,
                    npm_registry=settings.npm_registry,
                ),
            )
            return _list_tools(installed.argv0, installed.args, env, secrets)


def _override_argv(text: str) -> tuple[str, list[str]]:
    if any(mark in text for mark in (";", "$(")):
        raise BoxCommandError("box_command_invalid", "child argv override is not a direct command")
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise BoxCommandError(
            "box_command_invalid", "child argv override could not be parsed"
        ) from exc
    if not tokens:
        raise BoxCommandError("box_command_invalid", "child argv override is empty")
    return tokens[0], tokens[1:]


def _child_environment(provider: Provider, discovery: dict[str, str], home: Path) -> dict[str, str]:
    env = {"PATH": _CHILD_PATH, "HOME": str(home), "LANG": "C.UTF-8"}
    env.update(discovery)
    for key, value in provider.server.static_env:
        env[key] = value
    return env


def _discovery_env(provider: Provider, env_file: Path | None) -> dict[str, str]:
    if provider.discovery.mode == "manual":
        if env_file is None:
            raise ConfigError(["discovery.mode is manual; pass --env-file"])
        return _read_env_file(env_file)
    if env_file is not None:
        raise ConfigError(["--env-file is only used when discovery.mode is manual"])
    return dict(provider.discovery.env)


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ConfigError([f"{path} does not exist"])
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        name, separator, value = line.partition("=")
        if not separator or not name:
            raise ConfigError(["env file contains a line that is not NAME=VALUE"])
        values[name] = value
    return values


def _list_tools(
    argv0: str, args: list[str], env: dict[str, str], secrets: tuple[str, ...]
) -> list[dict]:
    try:
        return anyio.run(_collect_tools, argv0, args, env, secrets)
    except InstallFailed:
        raise
    except Exception as exc:
        detail = mask_install_text(str(exc), secrets)
        raise InstallFailed(f"snapshot failed: {detail}") from None


async def _collect_tools(
    argv0: str, args: list[str], env: dict[str, str], secrets: tuple[str, ...]
) -> list[dict]:
    # anyio accepts stderr only when the stream has a real file descriptor.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        params = StdioServerParameters(command=argv0, args=args, env=env)
        try:
            with _hidden_inherited_env():
                with anyio.fail_after(60):
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            listed = await session.list_tools()
        except TimeoutError as exc:
            raise InstallFailed("timeout") from exc
        except Exception as exc:
            errlog.seek(0)
            first = ""
            for line in errlog.read().splitlines():
                if line.strip():
                    first = mask_install_text(line.strip(), secrets)
                    break
            detail = first or mask_install_text(str(exc), secrets)
            raise InstallFailed(f"snapshot failed: {detail}") from None
    return listed_tools(listed)


@contextmanager
def _hidden_inherited_env() -> Iterator[None]:
    """``stdio_client`` merges a few variables from the parent. Hide them for the spawn."""
    saved = {key: os.environ.get(key) for key in _INHERITED}
    try:
        for key in _INHERITED:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_snapshot(snapshot: dict, out: str) -> None:
    text = render_snapshot(snapshot)
    if out == "-":
        print(text, end="")
        return
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _check(path: Path, snapshot: dict) -> int:
    if not path.is_file():
        print(f"{path.name}: file not found")
        return 1
    try:
        expected = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"{path.name}: not json")
        return 1
    differences = _differences(_without_taken_at(expected), _without_taken_at(snapshot))
    if not differences:
        return 0
    for line in differences:
        print(line)
    return 1


def _without_taken_at(document: object) -> object:
    if not isinstance(document, dict):
        return document
    copy = dict(document)
    copy.pop("taken_at", None)
    return copy


def _differences(expected: object, actual: object, path: str = "") -> list[str]:
    prefix = path or "<root>"
    if isinstance(expected, dict) and isinstance(actual, dict):
        messages: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}" if path else str(key)
            if key not in expected:
                messages.append(f"{child}: unexpected")
            elif key not in actual:
                messages.append(f"{child}: missing")
            else:
                messages.extend(_differences(expected[key], actual[key], child))
        return messages
    if isinstance(expected, list) and isinstance(actual, list):
        messages: list[str] = []
        if len(expected) != len(actual):
            messages.append(f"{prefix}: length {len(expected)} != {len(actual)}")
        for index, (left, right) in enumerate(zip(expected, actual, strict=False)):
            child = f"{path}[{index}]" if path else f"[{index}]"
            messages.extend(_differences(left, right, child))
        return messages
    if expected != actual:
        return [f"{prefix}: expected {_short(expected)}, got {_short(actual)}"]
    return []


def _short(value: object) -> str:
    text = repr(value)
    if len(text) > 120:
        return text[:117] + "..."
    return text


def _failure(exc: Exception) -> int:
    if isinstance(exc, ConfigError):
        for message in exc.messages:
            print(f"config error: {message}", file=sys.stderr)
        return 2
    if isinstance(exc, BoxCommandError):
        log_event("provider_install_failed", code=exc.code, reason=exc.message)
        return 3
    if isinstance(exc, ProviderError):
        reason = exc.messages[0] if exc.messages else "provider is not valid"
        log_event("provider_install_failed", code="provider_invalid", reason=reason)
        return 3
    reason = exc.reason if isinstance(exc, InstallFailed) else str(exc)
    log_event("provider_install_failed", code="install_failed", reason=reason)
    return 3
