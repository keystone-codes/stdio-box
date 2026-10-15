"""Install a provider server from its hash-pinned lock into the cache volume.

The installer version is part of the cache key. Bump it when the install
flags change so an older tree is not reused.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from stdio_box.boxcommand import BoxCommand, BoxCommandError
from stdio_box.provider import Provider

logger = logging.getLogger("stdio_box.install")

INSTALLER_VERSION = "1"
INSTALLER_UID = 10002
PYTHON_BIN = "/usr/local/bin/python3.12"
_INSTALLER_PATH = "/usr/local/bin:/usr/bin:/bin"
_HASH = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})\b")
_TOKEN_PATTERNS = (
    re.compile(
        r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|xox[abprs]-|sk-(?:proj-|ant-)?)"
        r"[A-Za-z0-9_\-]{20,}"
    ),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ATATT[A-Za-z0-9_\-=]{20,}"),
    re.compile(r"ntn_[A-Za-z0-9]{20,}"),
    re.compile(r"secret_[A-Za-z0-9]{20,}"),
    re.compile(r"y0_[A-Za-z0-9_\-]{20,}"),
    re.compile(r"tvly-[A-Za-z0-9]{20,}"),
)


class InstallFailed(Exception):
    """The installer did not produce a complete tree."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class LockInfo:
    runner: str
    package: str
    version: str
    lock_bytes: bytes
    package_json_bytes: bytes


@dataclass(frozen=True)
class InstalledServer:
    argv0: str
    key: str
    from_cache: bool
    duration_ms: int
    at: str
    args: tuple[str, ...]


def configure_install_logging() -> None:
    """Send install records to stderr as one JSON object per line."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def log_event(event: str, **fields: object) -> None:
    logger.info("%s", json.dumps({"event": event, **fields}, sort_keys=True))


def installer_environment(
    cache_root: Path, *, pip_index_url: str = "", npm_registry: str = ""
) -> dict[str, str]:
    """Environment of the installer process. Nothing else is passed through."""
    env = {
        "PATH": _INSTALLER_PATH,
        "HOME": str(cache_root / ".home"),
        "LANG": "C.UTF-8",
    }
    if pip_index_url:
        env["UV_INDEX_URL"] = pip_index_url
    if npm_registry:
        env["npm_config_registry"] = npm_registry
    return env


def mask_install_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Mask secret values and the same token shapes as child stderr."""
    masked = text
    for secret in sorted((item for item in secrets if len(item) >= 6), key=len, reverse=True):
        masked = masked.replace(secret, "***")
    for pattern in _TOKEN_PATTERNS:
        masked = pattern.sub("***", masked)
    return masked


def installed_payload(server: InstalledServer) -> str:
    """JSON passed to the wrapper as ``--installed``."""
    return json.dumps(
        {
            "path": server.argv0,
            "key": server.key,
            "from_cache": server.from_cache,
            "duration_ms": server.duration_ms,
            "at": server.at,
        },
        sort_keys=True,
    )


def install_status(server: InstalledServer) -> dict[str, object]:
    """``install`` object for ``/healthz`` and ``credsctl status``."""
    return {
        "key": server.key,
        "at": server.at,
        "duration_ms": server.duration_ms,
        "from_cache": server.from_cache,
    }


def cache_key(
    lock_bytes: bytes, package_json_bytes: bytes, runner: str, installer_version: str
) -> str:
    """First 16 hex characters of the install identity."""
    hasher = hashlib.sha256()
    for part in (
        lock_bytes,
        package_json_bytes,
        runner.encode("utf-8"),
        installer_version.encode("utf-8"),
    ):
        hasher.update(len(part).to_bytes(8, "big"))
        hasher.update(part)
    return hasher.hexdigest()[:16]


def verify_against_lock(cmd: BoxCommand, provider_dir: Path, provider: Provider) -> LockInfo:
    """Check ``cmd`` against the provider lock. Raise ``box_command_not_locked`` on mismatch."""
    root = Path(provider_dir)
    lock_name = provider.server.lock
    if cmd.runner == "uvx" and lock_name != "requirements.txt":
        raise BoxCommandError(
            "box_command_not_locked",
            f"server.lock is {lock_name}, command uses uvx",
        )
    if cmd.runner == "npx" and lock_name != "package-lock.json":
        raise BoxCommandError(
            "box_command_not_locked",
            f"server.lock is {lock_name}, command uses npx",
        )
    if cmd.runner == "uvx":
        lock_bytes = _read_bytes(root / "requirements.txt")
        _verify_requirements(cmd, lock_bytes)
        return LockInfo(cmd.runner, cmd.package, cmd.version, lock_bytes, b"")
    lock_bytes = _read_bytes(root / "package-lock.json")
    package_json = _read_bytes(root / "package.json")
    _verify_node(cmd, package_json, lock_bytes)
    return LockInfo(cmd.runner, cmd.package, cmd.version, lock_bytes, package_json)


def ensure_installed(
    cmd: BoxCommand,
    provider: Provider,
    provider_dir: Path,
    cache_root: Path,
    *,
    run_as: int | None,
    timeout: float,
    env: dict[str, str],
    find_links: Path | None = None,
    python_executable: str = PYTHON_BIN,
) -> InstalledServer:
    """Return the installed executable, installing into ``cache_root`` when needed.

    ``find_links`` adds ``--find-links`` for a local wheel directory. Launch
    leaves it unset. Hashes are still required.
    """
    started = time.monotonic()
    info = verify_against_lock(cmd, provider_dir, provider)
    key = cache_key(info.lock_bytes, info.package_json_bytes, cmd.runner, INSTALLER_VERSION)
    root = _provider_cache(cache_root, provider.name)
    _prepare_home(cache_root, run_as)
    dest = root / key
    if _cache_hit(dest, key):
        return _server(dest, provider, cmd, key, from_cache=True, started=started)
    if dest.exists():
        shutil.rmtree(dest)
    tmp = root / f".tmp-{secrets.token_hex(16)}"
    tmp.mkdir()
    if run_as is not None:
        os.chown(tmp, run_as, run_as)
    try:
        _install_tree(
            cmd,
            provider,
            Path(provider_dir),
            tmp,
            env=env,
            run_as=run_as,
            timeout=timeout,
            find_links=find_links,
            python_executable=python_executable,
        )
        _lock_down(tmp)
        binary = _executable(tmp, provider)
        if not binary.is_file():
            raise InstallFailed(f"executable {provider.server.bin} was not installed")
        _retarget_install_paths(tmp, dest)
        try:
            os.rename(tmp, dest)
        except OSError:
            if _cache_hit(dest, key):
                shutil.rmtree(tmp, ignore_errors=True)
                return _server(dest, provider, cmd, key, from_cache=True, started=started)
            shutil.rmtree(tmp, ignore_errors=True)
            raise InstallFailed("could not publish the installed server") from None
    except InstallFailed:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    marker = dest / ".complete"
    marker.write_text(key + "\n", encoding="utf-8")
    os.chmod(marker, 0o644)
    if run_as is not None:
        os.chown(marker, run_as, run_as)
    _prune_old_keys(root, key)
    return _server(dest, provider, cmd, key, from_cache=False, started=started)


def prepare_cache_root(cache_root: Path, run_as: int | None) -> None:
    """Make ``cache_root`` writable by the installer uid when we can change owners."""
    cache_root.mkdir(parents=True, exist_ok=True)
    if run_as is None:
        return
    os.chown(cache_root, run_as, run_as)
    os.chmod(cache_root, 0o755)


def setuid_available() -> bool:
    """True when this process may start the installer as uid 10002."""
    if os.geteuid() != 0:
        return False
    status = Path("/proc/self/status")
    if not status.is_file():
        return True
    for line in status.read_text(encoding="utf-8").splitlines():
        if line.startswith("CapEff:"):
            return bool(int(line.split()[1], 16) & (1 << 7))
    return False


def _server(
    dest: Path,
    provider: Provider,
    cmd: BoxCommand,
    key: str,
    *,
    from_cache: bool,
    started: float,
) -> InstalledServer:
    binary = _executable(dest, provider)
    return InstalledServer(
        argv0=str(binary),
        key=key,
        from_cache=from_cache,
        duration_ms=int((time.monotonic() - started) * 1000),
        at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        args=cmd.args,
    )


def _executable(dest: Path, provider: Provider) -> Path:
    if provider.server.lock == "package-lock.json":
        return dest / "node_modules" / ".bin" / provider.server.bin
    return dest / "venv" / "bin" / provider.server.bin


def _provider_cache(cache_root: Path, name: str) -> Path:
    root = cache_root / name
    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    resolved_cache = cache_root.resolve()
    if resolved_cache != resolved_root and resolved_cache not in resolved_root.parents:
        raise InstallFailed("provider cache path escapes the cache root")
    return root


def _prepare_home(cache_root: Path, run_as: int | None) -> None:
    home = cache_root / ".home"
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o755)
    if run_as is not None:
        os.chown(home, run_as, run_as)
        provider_parent = cache_root
        if provider_parent.exists():
            os.chown(provider_parent, run_as, run_as)


def _cache_hit(dest: Path, key: str) -> bool:
    marker = dest / ".complete"
    if not marker.is_file():
        return False
    return marker.read_text(encoding="utf-8").strip() == key


def _install_tree(
    cmd: BoxCommand,
    provider: Provider,
    provider_dir: Path,
    tmp: Path,
    *,
    env: dict[str, str],
    run_as: int | None,
    timeout: float,
    find_links: Path | None,
    python_executable: str,
) -> None:
    deadline = time.monotonic() + timeout
    secrets = tuple(env.values())
    if cmd.runner == "uvx":
        venv = tmp / "venv"
        commands = [
            ["uv", "venv", "--python", python_executable, str(venv)],
            _pip_command(venv, provider_dir, find_links),
        ]
        for command in commands:
            _run_command(
                command, cwd=tmp, env=env, run_as=run_as, deadline=deadline, secrets=secrets
            )
        return
    for name in ("package.json", "package-lock.json"):
        target = tmp / name
        shutil.copyfile(provider_dir / name, target)
        os.chmod(target, 0o644)
        if run_as is not None:
            os.chown(target, run_as, run_as)
    npm_cache = tmp / ".npm"
    _run_command(
        [
            "npm",
            "ci",
            "--ignore-scripts",
            "--omit=dev",
            "--no-audit",
            "--no-fund",
            "--cache",
            str(npm_cache),
        ],
        cwd=tmp,
        env=env,
        run_as=run_as,
        deadline=deadline,
        secrets=secrets,
    )
    shutil.rmtree(npm_cache, ignore_errors=True)


def _pip_command(venv: Path, provider_dir: Path, find_links: Path | None) -> list[str]:
    command = [
        "uv",
        "pip",
        "install",
        "--python",
        str(venv / "bin" / "python"),
        "--require-hashes",
        "--no-deps",
        "--only-binary",
        ":all:",
        "--no-cache",
    ]
    if find_links is not None:
        command.extend(["--find-links", str(find_links)])
    command.extend(["-r", str(provider_dir / "requirements.txt")])
    return command


def _run_command(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    run_as: int | None,
    deadline: float,
    secrets: tuple[str, ...],
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InstallFailed("timeout")
    popen_kwargs: dict[str, object] = {
        "args": args,
        "cwd": cwd,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "start_new_session": True,
    }
    if run_as is not None:
        popen_kwargs["user"] = run_as
        popen_kwargs["group"] = run_as
        popen_kwargs["extra_groups"] = []
    try:
        proc = subprocess.Popen(**popen_kwargs)
    except PermissionError as exc:
        raise InstallFailed("could not change installer uid") from exc
    try:
        stdout, stderr = proc.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise InstallFailed("timeout") from None
    _log_output(stdout, stderr, secrets)
    if proc.returncode != 0:
        raise InstallFailed(_first_error_line(stdout, stderr, proc.returncode, secrets))


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _log_output(stdout: str, stderr: str, secrets: tuple[str, ...]) -> None:
    for stream in (stdout, stderr):
        for line in stream.splitlines():
            if line.strip():
                logger.info("%s", mask_install_text(line, secrets))


def _first_error_line(stdout: str, stderr: str, returncode: int, secrets: tuple[str, ...]) -> str:
    text = stderr if stderr.strip() else stdout
    for line in text.splitlines():
        if line.strip():
            return mask_install_text(line.strip(), secrets)[:500]
    return f"installer exited with status {returncode}"


def _retarget_install_paths(tmp: Path, dest: Path) -> None:
    """Point venv scripts at the published directory before the temp path disappears."""
    venv = tmp / "venv"
    if not venv.is_dir():
        return
    old = str(tmp).encode()
    new = str(dest).encode()
    candidates = [venv / "pyvenv.cfg"]
    bindir = venv / "bin"
    if bindir.is_dir():
        candidates.extend(
            path for path in bindir.iterdir() if path.is_file() and not path.is_symlink()
        )
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if old not in data:
            continue
        path.write_bytes(data.replace(old, new))


def _lock_down(root: Path) -> None:
    """Directories are 0755. Files lose group and other write. Symlinks are left alone."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        os.chmod(current, 0o755)
        kept: list[str] = []
        for name in dirnames:
            if (current / name).is_symlink():
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = current / name
            if path.is_symlink():
                continue
            mode = path.stat().st_mode & 0o777
            os.chmod(path, mode & ~0o022)


def _prune_old_keys(provider_root: Path, current: str) -> None:
    completed: list[tuple[int, str, Path]] = []
    for child in provider_root.iterdir():
        if not child.is_dir() or child.name == current or child.name.startswith("."):
            continue
        marker = child / ".complete"
        if not marker.is_file():
            shutil.rmtree(child, ignore_errors=True)
            continue
        if marker.read_text(encoding="utf-8").strip() != child.name:
            shutil.rmtree(child, ignore_errors=True)
            continue
        completed.append((marker.stat().st_mtime_ns, child.name, child))
    completed.sort()
    for _, _, path in completed[:-1]:
        shutil.rmtree(path, ignore_errors=True)


def _read_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise BoxCommandError("box_command_not_locked", f"{path.name} does not exist")
    return path.read_bytes()


def _verify_requirements(cmd: BoxCommand, lock_bytes: bytes) -> None:
    wanted = canonicalize_name(cmd.package)
    wanted_version = Version(cmd.version)
    matched = False
    mismatched: str | None = None
    for line in _requirement_lines(lock_bytes.decode("utf-8")):
        if line.startswith("-"):
            raise BoxCommandError(
                "box_command_not_locked",
                "requirements.txt contains an installer option",
            )
        requirement, options = _split_requirement(line)
        hashes = [token for token in options if _HASH.fullmatch(token)]
        try:
            parsed = Requirement(requirement)
        except InvalidRequirement as exc:
            raise BoxCommandError(
                "box_command_not_locked",
                "requirements.txt has a requirement that could not be parsed",
            ) from exc
        if not hashes:
            raise BoxCommandError(
                "box_command_not_locked",
                f"requirements.txt requirement {parsed.name}=={_pinned_version(parsed)} "
                "has no sha256 hash",
            )
        if canonicalize_name(parsed.name) != wanted:
            continue
        specs = list(parsed.specifier)
        if len(specs) != 1 or specs[0].operator != "==":
            mismatched = f"requirements.txt does not pin {parsed.name} to an exact version"
            continue
        found = Version(specs[0].version)
        if found != wanted_version:
            mismatched = (
                f"requirements.txt pins {parsed.name}=={specs[0].version}, "
                f"command asks for {cmd.version}"
            )
            continue
        matched = True
    if not matched:
        raise BoxCommandError(
            "box_command_not_locked",
            mismatched or f"requirements.txt has no hash-pinned {cmd.package}=={cmd.version}",
        )


def _pinned_version(requirement: Requirement) -> str:
    specs = list(requirement.specifier)
    if len(specs) == 1:
        return specs[0].version
    return "?"


def _requirement_lines(text: str) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        piece = stripped[:-1].rstrip() if stripped.endswith("\\") else stripped
        current = f"{current} {piece}".strip() if current else piece
        if stripped.endswith("\\"):
            continue
        if current:
            logical.append(current)
        current = ""
    if current:
        logical.append(current)
    return logical


def _split_requirement(line: str) -> tuple[str, list[str]]:
    requirement: list[str] = []
    options: list[str] = []
    seen_option = False
    for token in line.split():
        if token.startswith("-"):
            seen_option = True
        if seen_option:
            options.append(token)
        else:
            requirement.append(token)
    return " ".join(requirement), options


def _verify_node(cmd: BoxCommand, package_json: bytes, lock_bytes: bytes) -> None:
    try:
        document = json.loads(package_json)
        lock = json.loads(lock_bytes)
    except json.JSONDecodeError as exc:
        raise BoxCommandError(
            "box_command_not_locked",
            "package.json or package-lock.json is not json",
        ) from exc
    dependencies = document.get("dependencies")
    if dependencies != {cmd.package: cmd.version}:
        found = dependencies.get(cmd.package) if isinstance(dependencies, dict) else None
        if isinstance(found, str) and found != cmd.version:
            detail = f"package.json pins {cmd.package} to {found}, command asks for {cmd.version}"
        else:
            detail = f"package.json dependencies must be exactly {{{cmd.package}: {cmd.version}}}"
        raise BoxCommandError("box_command_not_locked", detail)
    if lock.get("lockfileVersion") != 3:
        raise BoxCommandError(
            "box_command_not_locked",
            f"package-lock.json lockfileVersion is {lock.get('lockfileVersion')}, expected 3",
        )
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise BoxCommandError("box_command_not_locked", "package-lock.json has no packages")
    entry_name = f"node_modules/{cmd.package}"
    entry = packages.get(entry_name)
    found_version = entry.get("version") if isinstance(entry, dict) else None
    if found_version != cmd.version:
        raise BoxCommandError(
            "box_command_not_locked",
            f"package-lock.json {entry_name} version is {found_version}, "
            f"command asks for {cmd.version}",
        )
    for name, item in packages.items():
        # The root project is not a downloaded package and has no integrity.
        if name == "":
            continue
        if not isinstance(item, dict) or not item.get("integrity"):
            raise BoxCommandError(
                "box_command_not_locked",
                f"package-lock.json entry {name} has no integrity",
            )
