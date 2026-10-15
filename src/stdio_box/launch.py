"""Process entry: prepare directories, install, then split wrapper from spawner.

``install_step`` is the hook the provider install fills in. On success this
module gives the result to the spawner (allow-list of ``argv0`` and its
resolved target) and to the wrapper as ``--installed`` JSON with ``argv0``,
``key``, ``from_cache``, and ``duration_ms``.
"""

import os
import stat
import sys
from dataclasses import dataclass

from stdio_box.ids import (
    INSTALLER_GID,
    INSTALLER_UID,
    WRAPPER_GID,
    WRAPPER_UID,
    can_setuid,
)
from stdio_box.installed import InstalledServer
from stdio_box.log import configure_logging
from stdio_box.protocol import control_socketpair
from stdio_box.spawner import SpawnerServer, allowlist_for

_NOT_IMPLEMENTED = "provider install is not implemented yet"


@dataclass(frozen=True)
class LaunchConfig:
    debug: bool
    argv_override: str | None

    @classmethod
    def from_environ(cls) -> "LaunchConfig":
        override = os.environ.get("BOX_CHILD_ARGV_OVERRIDE")
        return cls(debug=os.environ.get("BOX_DEBUG") == "1", argv_override=override or None)


def install_step(config: LaunchConfig) -> InstalledServer:
    """Return the installed server, or exit before the process split.

    The real install parses ``BOX_COMMAND``, checks the lock file, and runs
    under uid 10002 when ``can_setuid(10002)`` is true. When setuid is not
    available it installs without changing uid and logs
    ``install_isolation_unavailable``. A failed install logs
    ``provider_install_failed`` and raises ``SystemExit(3)``.

    This revision only accepts ``BOX_CHILD_ARGV_OVERRIDE`` while
    ``BOX_DEBUG=1``. Anything else raises ``SystemExit(2)``.
    """
    if config.debug and config.argv_override:
        return InstalledServer(argv0=config.argv_override, key="debug")
    print(_NOT_IMPLEMENTED, file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    configure_logging()
    config = LaunchConfig.from_environ()
    # cap_drop of SETUID leaves euid 0 but setuid fails. Stay in one process.
    privileged = os.geteuid() == 0 and can_setuid(WRAPPER_UID)
    if privileged:
        prepare_directories()
    installed = install_step(config)
    if not privileged:
        _exec_serve(None, installed)
    return _split(installed)


def prepare_directories() -> None:
    """Enforce owners on the data and cache volumes and recreate ``/tmp/box``."""
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
    payload = installed.to_json()
    argv = ["stdio-box", "serve", "--installed", payload]
    if spawner_fd is None:
        argv.extend(["--spawner", "inline"])
    else:
        argv.extend(["--spawner-fd", str(spawner_fd)])
    os.execvp(argv[0], argv)
