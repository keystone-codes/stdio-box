"""Process entry: prepare directories, install, then split wrapper from spawner.

``install_step`` parses ``BOX_COMMAND``, checks the lock, and installs under
uid 10002 when setuid is available. This module does not catch ``SystemExit``.
On success the spawner allow-list is ``argv0`` and its realpath, and the
wrapper receives ``installed_payload`` as ``--installed``. The child argv tail
is ``InstalledServer.args``. A debug override does not install and does not fork
from this module's install helper: the split below is the only fork.
"""

import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from stdio_box.boxcommand import BoxCommandError, parse_box_command
from stdio_box.config import ConfigError, InstallSettings, load_settings
from stdio_box.ids import (
    INSTALLER_GID,
    INSTALLER_UID,
    WRAPPER_GID,
    WRAPPER_UID,
    can_setuid,
)
from stdio_box.install import (
    InstalledServer,
    InstallFailed,
    configure_install_logging,
    ensure_installed,
    installed_payload,
    installer_environment,
    log_event,
    prepare_cache_root,
    setuid_available,
    verify_against_lock,
)
from stdio_box.log import configure_logging
from stdio_box.protocol import control_socketpair
from stdio_box.provider import ProviderError, load_provider
from stdio_box.spawner import SpawnerServer, allowlist_for


def install_step(settings: InstallSettings) -> InstalledServer:
    """Parse ``BOX_COMMAND``, check the lock, and install as uid 10002 when possible.

    ``BOX_DEBUG=1`` with ``BOX_CHILD_ARGV_OVERRIDE`` returns that program and
    does not call ``ensure_installed``. The override is the debug-spawn child.
    """
    if settings.debug and settings.child_argv_override:
        return InstalledServer(
            argv0=settings.child_argv_override,
            key="debug",
            from_cache=False,
            duration_ms=0,
            at="",
            args=(),
        )
    if not settings.command:
        raise ConfigError(["BOX_COMMAND is required"])
    if settings.provider_dir is None:
        raise ConfigError(["BOX_PROVIDER is required"])
    command = parse_box_command(settings.command)
    provider = load_provider(settings.provider_dir)
    verify_against_lock(command, settings.provider_dir, provider)
    run_as = INSTALLER_UID if setuid_available() else None
    if run_as is None:
        log_event("install_isolation_unavailable")
    prepare_cache_root(settings.cache_dir, run_as)
    return ensure_installed(
        command,
        provider,
        settings.provider_dir,
        settings.cache_dir,
        run_as=run_as,
        timeout=settings.install_timeout_seconds,
        env=installer_environment(
            settings.cache_dir,
            pip_index_url=settings.pip_index_url,
            npm_registry=settings.npm_registry,
        ),
    )


def main() -> int:
    """Prepare directories, install, then split. Do not catch ``SystemExit``."""
    configure_logging()
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        _print_config(exc)
        return 2
    # cap_drop of SETUID leaves euid 0 but setuid fails. Stay in one process.
    # The unprivileged path still runs install_step, so a missing command exits 2.
    if os.geteuid() == 0 and can_setuid(WRAPPER_UID):
        prepare_directories()
        installed = _finish_install(settings)
        return _split(installed)
    installed = _finish_install(settings)
    _exec_serve(None, installed)
    return 1


def _finish_install(settings: InstallSettings) -> InstalledServer:
    try:
        installed = install_step(settings)
    except ConfigError as exc:
        _print_config(exc)
        raise SystemExit(2) from exc
    except BoxCommandError as exc:
        log_event("provider_install_failed", code=exc.code, reason=exc.message)
        raise SystemExit(3) from exc
    except ProviderError as exc:
        reason = exc.messages[0] if exc.messages else "provider is not valid"
        log_event("provider_install_failed", code="provider_invalid", reason=reason)
        raise SystemExit(3) from exc
    except InstallFailed as exc:
        log_event("provider_install_failed", code="install_failed", reason=exc.reason)
        raise SystemExit(3) from exc
    if settings.debug and settings.child_argv_override:
        return installed
    log_event(
        "install",
        key=installed.key,
        at=installed.at,
        duration_ms=installed.duration_ms,
        from_cache=installed.from_cache,
        outcome="ok",
    )
    return installed


def prepare_directories() -> None:
    """Enforce owners on the data and cache volumes and recreate ``/tmp/box``.

    ``/cache`` belongs to uid 10002. Children and slot reap never run as that uid.
    """
    _ensure_dir("/data", WRAPPER_UID, WRAPPER_GID, 0o700)
    _ensure_dir("/cache", INSTALLER_UID, INSTALLER_GID, 0o755)
    _recreate_tmp_box()


def _ensure_dir(path: str, uid: int, gid: int, mode: int) -> None:
    os.makedirs(path, mode=mode, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def _recreate_tmp_box() -> None:
    path = "/tmp/box"
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        _remove_children(path)
        os.rmdir(path)
    elif os.path.lexists(path):
        os.unlink(path)
    os.mkdir(path, 0o711)
    os.chown(path, 0, 0)
    os.chmod(path, 0o711)


def _remove_children(path: str) -> None:
    for name in os.listdir(path):
        child = os.path.join(path, name)
        info = os.lstat(child)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            os.unlink(child)
            continue
        _remove_children(child)
        os.rmdir(child)


def _split(installed: InstalledServer) -> int:
    wrapper, spawner = control_socketpair()
    pid = os.fork()
    if pid == 0:
        spawner.close()
        try:
            os.setgroups([])
            os.setgid(WRAPPER_GID)
            os.setuid(WRAPPER_UID)
        except OSError as exc:
            print(f"cannot drop privileges: {exc}", file=sys.stderr)
            os._exit(1)
        fd = wrapper.detach()
        os.set_inheritable(fd, True)
        try:
            _exec_serve(fd, installed)
        except OSError as exc:
            print(f"cannot exec wrapper: {exc}", file=sys.stderr)
            os._exit(127)
    wrapper.close()
    server = SpawnerServer(spawner, allowlist_for(installed.argv0), wrapper_pid=pid)
    try:
        return server.serve_forever()
    finally:
        spawner.close()


def _exec_serve(spawner_fd: int | None, installed: InstalledServer) -> None:
    payload = installed_payload(installed)
    argv = ["stdio-box", "serve", "--installed", payload]
    if spawner_fd is None:
        argv.extend(["--spawner", "inline"])
    else:
        argv.extend(["--spawner-fd", str(spawner_fd)])
    os.execvp(argv[0], argv)


def run_launch(environ: Mapping[str, str] | None = None) -> int:
    """Install and print the payload. Callers that own the split use ``main`` instead.

    This helper does not fork. ``stdio-box launch`` does not call it.
    """
    configure_install_logging()
    source = os.environ if environ is None else environ
    try:
        settings = load_settings(source)
        if not settings.command:
            raise ConfigError(["BOX_COMMAND is required"])
        if settings.provider_dir is None:
            raise ConfigError(["BOX_PROVIDER is required"])
    except ConfigError as exc:
        for message in exc.messages:
            print(f"config error: {message}", file=sys.stderr)
        return 2
    try:
        installed = install_step(settings)
    except BoxCommandError as exc:
        log_event("provider_install_failed", code=exc.code, reason=exc.message)
        return 3
    except ProviderError as exc:
        reason = exc.messages[0] if exc.messages else "provider is not valid"
        log_event("provider_install_failed", code="provider_invalid", reason=reason)
        return 3
    except InstallFailed as exc:
        log_event("provider_install_failed", code="install_failed", reason=exc.reason)
        return 3
    log_event(
        "install",
        key=installed.key,
        at=installed.at,
        duration_ms=installed.duration_ms,
        from_cache=installed.from_cache,
        outcome="ok",
    )
    print(installed_payload(installed), flush=True)
    return 0


def run_install(argv: list[str], environ: Mapping[str, str] | None = None) -> int:
    """Install without the process split. A non-root caller does not change uid."""
    configure_install_logging()
    try:
        provider_dir, cache_dir = _install_flags(argv)
    except ValueError:
        print(_INSTALL_USAGE, file=sys.stderr)
        return 2
    source = os.environ if environ is None else environ
    try:
        settings = load_settings(source)
    except ConfigError as exc:
        for message in exc.messages:
            print(f"config error: {message}", file=sys.stderr)
        return 2
    if provider_dir is None:
        provider_dir = settings.provider_dir
    if provider_dir is None:
        print("config error: BOX_PROVIDER is required", file=sys.stderr)
        return 2
    if cache_dir is not None:
        settings = replace(settings, cache_dir=cache_dir)
    try:
        provider = load_provider(provider_dir)
        command_text = settings.command or provider.server.command
        command = parse_box_command(command_text)
        run_as = INSTALLER_UID if setuid_available() else None
        prepare_cache_root(settings.cache_dir, run_as)
        installed = ensure_installed(
            command,
            provider,
            provider_dir,
            settings.cache_dir,
            run_as=run_as,
            timeout=settings.install_timeout_seconds,
            env=installer_environment(
                settings.cache_dir,
                pip_index_url=settings.pip_index_url,
                npm_registry=settings.npm_registry,
            ),
        )
    except BoxCommandError as exc:
        log_event("provider_install_failed", code=exc.code, reason=exc.message)
        return 3
    except ProviderError as exc:
        reason = exc.messages[0] if exc.messages else "provider is not valid"
        log_event("provider_install_failed", code="provider_invalid", reason=reason)
        return 3
    except InstallFailed as exc:
        log_event("provider_install_failed", code="install_failed", reason=exc.reason)
        return 3
    log_event(
        "install",
        key=installed.key,
        at=installed.at,
        duration_ms=installed.duration_ms,
        from_cache=installed.from_cache,
        outcome="ok",
    )
    print(installed_payload(installed), flush=True)
    return 0


_INSTALL_USAGE = "usage: stdio-box install [--provider-dir DIR] [--cache DIR]"


def _install_flags(argv: list[str]) -> tuple[Path | None, Path | None]:
    provider_dir: Path | None = None
    cache_dir: Path | None = None
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in {"--provider-dir", "--cache"}:
            if index + 1 >= len(argv):
                raise ValueError(flag)
            value = Path(argv[index + 1])
            if flag == "--provider-dir":
                provider_dir = value
            else:
                cache_dir = value
            index += 2
            continue
        raise ValueError(flag)
    return provider_dir, cache_dir


def _print_config(exc: ConfigError) -> None:
    for message in exc.messages:
        print(f"config error: {message}", file=sys.stderr)
