"""Wrapper entry: non-dumpable, then a spawner client or the inline spawner.

``BOX_DEBUG=1`` also opens an abstract-namespace socket so ``debug-spawn`` and
``credsctl status`` can drive children from another process in the container.
That socket is not the control channel; the spawner still uses the inherited
socketpair.
"""

import ctypes
import json
import logging
import os
import signal
import socket
import sys
import threading
from dataclasses import dataclass

import anyio

from stdio_box.child_io import open_child_session
from stdio_box.install import InstalledServer as InstallRecord
from stdio_box.install import install_status
from stdio_box.installed import InstalledServer
from stdio_box.log import configure_logging
from stdio_box.runtime import get_install, get_isolation, note_child_isolation, note_install
from stdio_box.spawner import (
    ChildHandle,
    InlineSpawner,
    SpawnerClient,
    SpawnerError,
    allowlist_for,
)
from stdio_box.stderr import StderrPump

_LOG = logging.getLogger("stdio_box.wrapper")
DEBUG_ADDRESS = b"\0stdio-box-debug"
_CHILD_PATH = "/opt/stdio-box/.venv/bin:/usr/local/bin:/usr/bin:/bin"


@dataclass
class _ServeArgs:
    inline: bool
    spawner_fd: int | None
    installed: InstalledServer


@dataclass
class _Running:
    handle: ChildHandle
    session: object
    exit_stack: object
    pump: StderrPump


def main(argv: list[str]) -> int:
    configure_logging()
    args = _parse_args(argv)
    _disable_dumpable()
    note_install(
        install_status(
            InstallRecord(
                argv0=args.installed.argv0,
                key=args.installed.key,
                from_cache=args.installed.from_cache,
                duration_ms=args.installed.duration_ms,
                at=args.installed.at,
                args=args.installed.args,
            )
        )
    )
    allowed = allowlist_for(args.installed.argv0)
    if args.inline:
        spawner: InlineSpawner | SpawnerClient = InlineSpawner(allowed)
        note_child_isolation("shared_uid")
    else:
        assert args.spawner_fd is not None
        spawner = SpawnerClient(socket.socket(fileno=args.spawner_fd))
    hub = _DebugHub(spawner, [args.installed.argv0, *args.installed.args])
    if os.environ.get("BOX_DEBUG") == "1":
        hub.start()
    # The isolation image has no facade settings. A configured box opens the
    # store and serves HTTP in this same process.
    if os.environ.get("BOX_BEARER_TOKEN"):
        from stdio_box.cli import serve_http

        serve_http(spawner=spawner, installed=args.installed)
        return 0
    _wait_until_stopped(spawner)
    return 0


def _disable_dumpable() -> None:
    if sys.platform != "linux":
        _LOG.debug("prctl PR_SET_DUMPABLE skipped: not Linux")
        return
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(4, 0, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        _LOG.warning("prctl PR_SET_DUMPABLE failed: %s", os.strerror(err))


def _parse_args(argv: list[str]) -> _ServeArgs:
    inline = False
    spawner_fd: int | None = None
    installed_text: str | None = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--spawner" and index + 1 < len(argv) and argv[index + 1] == "inline":
            inline = True
            index += 2
            continue
        if item == "--spawner-fd" and index + 1 < len(argv):
            spawner_fd = int(argv[index + 1])
            index += 2
            continue
        if item == "--installed" and index + 1 < len(argv):
            installed_text = argv[index + 1]
            index += 2
            continue
        print(
            "usage: stdio-box serve (--spawner inline | --spawner-fd N) --installed JSON",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if installed_text is None or inline == (spawner_fd is not None):
        print(
            "usage: stdio-box serve (--spawner inline | --spawner-fd N) --installed JSON",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        installed = InstalledServer.from_json(installed_text)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"installed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return _ServeArgs(inline=inline, spawner_fd=spawner_fd, installed=installed)


def _wait_until_stopped(spawner: InlineSpawner | SpawnerClient) -> None:
    stop = threading.Event()

    def _on_signal(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    while not stop.is_set():
        if isinstance(spawner, SpawnerClient) and spawner.closed:
            return
        stop.wait(0.5)


class _DebugHub:
    def __init__(self, spawner: InlineSpawner | SpawnerClient, argv: list[str]) -> None:
        self._spawner = spawner
        self._argv = argv
        self._slots: dict[int, _Running] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform != "linux":
            _LOG.debug("debug channel skipped: not Linux")
            return
        self._thread = threading.Thread(
            target=self._thread_main, name="stdio-box-debug", daemon=True
        )
        self._thread.start()

    def _thread_main(self) -> None:
        anyio.run(self._serve)

    async def _serve(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(DEBUG_ADDRESS)
        sock.listen(8)
        try:
            while True:
                conn, _addr = await anyio.to_thread.run_sync(sock.accept)
                try:
                    await self._handle(conn)
                finally:
                    conn.close()
        finally:
            sock.close()

    async def _handle(self, conn: socket.socket) -> None:
        raw = await anyio.to_thread.run_sync(_readline, conn)
        if not raw:
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            _write(conn, {"ok": False, "error": "bad_request"})
            return
        if not isinstance(msg, dict):
            _write(conn, {"ok": False, "error": "bad_request"})
            return
        try:
            result = await self._dispatch(msg)
        except SpawnerError as exc:
            result = {"ok": False, "error": exc.code}
        except Exception as exc:
            _LOG.warning("debug request failed: %s", type(exc).__name__)
            result = {"ok": False, "error": "failed"}
        _write(conn, result)

    async def _dispatch(self, msg: dict[str, object]) -> dict[str, object]:
        op = msg.get("op")
        if op == "status":
            return {
                "ok": True,
                "status": "ok",
                "isolation": get_isolation(),
                "install": get_install(),
            }
        slot = msg.get("slot")
        if type(slot) is not int:
            return {"ok": False, "error": "bad_request"}
        if op == "spawn":
            env = msg.get("env")
            if not isinstance(env, dict):
                env = {}
            return await self._spawn(slot, {str(key): str(value) for key, value in env.items()})
        if op == "call":
            name = msg.get("name")
            arguments = msg.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return {"ok": False, "error": "bad_request"}
            return await self._call(slot, name, arguments)
        if op == "stop":
            return await self._stop(slot)
        return {"ok": False, "error": "bad_request"}

    async def _spawn(self, slot: int, env: dict[str, str]) -> dict[str, object]:
        if slot in self._slots:
            return {"ok": False, "error": "slot_busy"}
        child_env = {
            "PATH": _CHILD_PATH,
            "LANG": "C.UTF-8",
            "HOME": f"/tmp/box/slot-{slot}",
        }
        child_env.update(env)
        handle = await anyio.to_thread.run_sync(
            lambda: self._spawner.spawn(slot, self._argv, child_env, f"/tmp/box/slot-{slot}")
        )
        note_child_isolation(handle.isolation)
        pump = StderrPump(
            handle.take_stderr(),
            "debug",
            1,
            child_env.values(),
            slot,
        )
        pump.start()
        stack = open_child_session(handle)
        try:
            session = await stack.__aenter__()
            await session.initialize()
            who = await session.call_tool("who", {})
        except Exception:
            await stack.__aexit__(*sys.exc_info())
            pump.close()
            await anyio.to_thread.run_sync(self._spawner.stop, handle.pid)
            try:
                await anyio.to_thread.run_sync(self._spawner.reap_slot, slot)
            except SpawnerError:
                pass
            raise
        self._slots[slot] = _Running(handle=handle, session=session, exit_stack=stack, pump=pump)
        payload = who.structuredContent or {}
        return {"ok": True, "pid": handle.pid, "uid": payload.get("uid"), "who": payload}

    async def _call(self, slot: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
        running = self._slots.get(slot)
        if running is None:
            return {"ok": False, "error": "not_running"}
        result = await running.session.call_tool(name, arguments)  # type: ignore[attr-defined]
        return {
            "ok": True,
            "isError": bool(result.isError),
            "structured": result.structuredContent,
        }

    async def _stop(self, slot: int) -> dict[str, object]:
        running = self._slots.pop(slot, None)
        if running is None:
            return {"ok": False, "error": "not_running"}
        # A setsid descendant inherits the child's stdout pipe. The reader
        # blocks in that read while holding the file lock, so closing the
        # session first deadlocks. Stop the group, reap the uid (which drops
        # the last write end), then close the session. Do not put another
        # cancel scope around that close: the session's task group already
        # cancels itself, and an outer deadline re-cancels this server task.
        await anyio.to_thread.run_sync(self._spawner.stop, running.handle.pid)
        reap = await anyio.to_thread.run_sync(self._spawner.reap_slot, slot)
        try:
            await running.exit_stack.__aexit__(None, None, None)  # type: ignore[attr-defined]
        except Exception:
            _LOG.warning("session close failed", exc_info=True)
        running.pump.close()
        return {"ok": True, "reap": reap}


def debug_spawn_main(argv: list[str]) -> int:
    if os.environ.get("BOX_DEBUG") != "1":
        print("debug-spawn is disabled", file=sys.stderr)
        return 2
    try:
        request = _parse_debug(argv)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        response = _debug_request(request)
    except OSError:
        print("wrapper is not running", file=sys.stderr)
        return 1
    print(response)
    payload = json.loads(response)
    return 0 if payload.get("ok") else 1


def credsctl_status() -> int:
    """Report liveness by asking the wrapper. The store-backed status replaces this."""
    try:
        response = _debug_request({"op": "status"})
    except OSError:
        print("wrapper is not running", file=sys.stderr)
        return 1
    print(response)
    return 0


def _parse_debug(argv: list[str]) -> dict[str, object]:
    if argv == ["--status"]:
        return {"op": "status"}
    slot: int | None = None
    tool: str | None = None
    arguments: dict[str, object] = {}
    env: dict[str, str] = {}
    stop = False
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--slot" and index + 1 < len(argv):
            slot = int(argv[index + 1])
            index += 2
            continue
        if item == "--tool" and index + 1 < len(argv):
            tool = argv[index + 1]
            index += 2
            continue
        if item == "--arguments" and index + 1 < len(argv):
            parsed = json.loads(argv[index + 1])
            if not isinstance(parsed, dict):
                raise ValueError("arguments must be a JSON object")
            arguments = parsed
            index += 2
            continue
        if item == "--env" and index + 1 < len(argv):
            key, sep, value = argv[index + 1].partition("=")
            if not sep or not key:
                raise ValueError("env must be KEY=VALUE")
            env[key] = value
            index += 2
            continue
        if item == "--stop":
            stop = True
            index += 1
            continue
        raise ValueError("usage: stdio-box debug-spawn --slot N [--tool NAME] [--stop]")
    if slot is None:
        raise ValueError("debug-spawn requires --slot")
    if stop:
        return {"op": "stop", "slot": slot}
    if tool is not None:
        return {"op": "call", "slot": slot, "name": tool, "arguments": arguments, "env": env}
    return {"op": "spawn", "slot": slot, "env": env}


def _debug_request(msg: dict[str, object]) -> str:
    # A tool call may need the slot to be running. Start it when env is present
    # on a call and the wrapper says not_running.
    if msg.get("op") == "call":
        started = _exchange({"op": "spawn", "slot": msg["slot"], "env": msg.get("env") or {}})
        parsed = json.loads(started)
        if not parsed.get("ok") and parsed.get("error") != "slot_busy":
            return started
    return _exchange(msg)


def _exchange(msg: dict[str, object]) -> str:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(DEBUG_ADDRESS)
        sock.sendall(json.dumps(msg).encode() + b"\n")
        return _readline(sock)
    finally:
        sock.close()


def _readline(conn: socket.socket) -> str:
    buf = bytearray()
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    line, _sep, _rest = bytes(buf).partition(b"\n")
    return line.decode()


def _write(conn: socket.socket, msg: dict[str, object]) -> None:
    conn.sendall(json.dumps(msg).encode() + b"\n")
