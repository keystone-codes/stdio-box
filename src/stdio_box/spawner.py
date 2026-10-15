"""Start, signal, and reap per-slot children.

The privileged server runs as root and speaks the socketpair protocol. It does
not open a network socket, read ``/data``, or import the wrapper. The inline
implementation is the same interface inside the wrapper process, for macOS and
for a container started with ``user:``.
"""

import errno
import fcntl
import json
import logging
import os
import queue
import resource
import select
import signal
import socket
import stat
import subprocess
import threading
from dataclasses import dataclass, field

from stdio_box.ids import MAX_SLOTS, NPROC_LIMIT, is_eperm, slot_cwd, slot_uid
from stdio_box.protocol import recv_message, send_message

_LOG = logging.getLogger("stdio_box.spawner")
_REAP_DEADLINE_SECONDS = 5.0
_OWNED_ROOTS = ("/tmp", "/dev/shm")


class SpawnerError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class ChildHandle:
    """Descriptors for one child. stdin write, stdout read, stderr read."""

    pid: int
    slot: int
    stdin: int | None
    stdout: int | None
    stderr: int | None
    isolation: str
    _stdin_file: object | None = field(default=None, repr=False)

    def bind_stdin(self, file: object) -> None:
        self._stdin_file = file
        self.stdin = None

    def take_stdout(self) -> int:
        fd = self.stdout
        if fd is None:
            raise RuntimeError("stdout already taken")
        self.stdout = None
        return fd

    def take_stderr(self) -> int:
        fd = self.stderr
        if fd is None:
            raise RuntimeError("stderr already taken")
        self.stderr = None
        return fd

    def close_stdin(self) -> None:
        file = self._stdin_file
        self._stdin_file = None
        if file is not None:
            close = getattr(file, "close", None)
            if close is not None:
                try:
                    close()
                except OSError:
                    pass
            self.stdin = None
            return
        self._close_fd("stdin")

    def close(self) -> None:
        self.close_stdin()
        self._close_fd("stdout")
        self._close_fd("stderr")

    def _close_fd(self, name: str) -> None:
        fd = getattr(self, name)
        if fd is None:
            return
        setattr(self, name, None)
        try:
            os.close(fd)
        except OSError:
            pass


def allowlist_for(argv0: str) -> frozenset[str]:
    """``argv0`` and, when it is a symlink, the target inside the install."""
    paths = {argv0}
    try:
        paths.add(os.path.realpath(argv0))
    except OSError:
        pass
    return frozenset(paths)


def argv0_allowed(argv0: str, allowed: frozenset[str]) -> bool:
    if argv0 in allowed:
        return True
    try:
        return os.path.realpath(argv0) in allowed
    except OSError:
        return False


class InlineSpawner:
    """Children via ``subprocess.Popen`` in this process. Isolation is shared."""

    isolation = "shared_uid"

    def __init__(self, allowed: frozenset[str], slot_root: str = "/tmp/box") -> None:
        self.allowed = allowed
        self.slot_root = slot_root
        self._procs: dict[int, subprocess.Popen[bytes]] = {}
        self._handles: dict[int, ChildHandle] = {}
        self._slot_of: dict[int, int] = {}

    def spawn(
        self,
        slot: int,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None = None,
    ) -> ChildHandle:
        del cwd
        if not isinstance(slot, int) or slot < 0 or slot >= MAX_SLOTS:
            raise SpawnerError("bad_request")
        if not argv or not argv0_allowed(argv[0], self.allowed):
            raise SpawnerError("bad_request")
        self.reap_slot(slot)
        path = slot_cwd(slot, self.slot_root)
        _reset_slot_directory(path, os.geteuid(), os.getegid())
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=path,
            start_new_session=True,
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        handle = ChildHandle(
            pid=proc.pid,
            slot=slot,
            stdin=_detach_pipe(proc.stdin),
            stdout=_detach_pipe(proc.stdout),
            stderr=_detach_pipe(proc.stderr),
            isolation="shared_uid",
        )
        self._procs[proc.pid] = proc
        self._handles[proc.pid] = handle
        self._slot_of[proc.pid] = slot
        return handle

    def stop(self, pid: int) -> None:
        proc = self._procs.get(pid)
        handle = self._handles.get(pid)
        if proc is None or handle is None:
            raise SpawnerError("unknown_pid")
        handle.close_stdin()
        if _wait_proc(proc, 2):
            return
        _killpg_quiet(pid, signal.SIGTERM)
        if _wait_proc(proc, 5):
            return
        _killpg_quiet(pid, signal.SIGKILL)
        _wait_proc(proc, 5)

    def signal(self, pid: int, sig: str) -> None:
        if pid not in self._procs or sig not in {"TERM", "KILL"}:
            raise SpawnerError("unknown_pid" if pid not in self._procs else "bad_request")
        _killpg_quiet(pid, signal.SIGTERM if sig == "TERM" else signal.SIGKILL)

    def reap_slot(self, slot: int) -> dict[str, object]:
        """Shared uid: kill the process group and recreate the slot directory.

        A uid-wide reap would kill the wrapper, which shares the uid.
        """
        killed = 0
        for pid, owned in list(self._slot_of.items()):
            if owned != slot:
                continue
            proc = self._procs.get(pid)
            if proc is None or proc.poll() is not None:
                continue
            _killpg_quiet(pid, signal.SIGKILL)
            if proc is not None:
                _wait_proc(proc, 2)
            killed += 1
        path = slot_cwd(slot, self.slot_root)
        os.makedirs(self.slot_root, mode=0o711, exist_ok=True)
        _reset_slot_directory(path, os.geteuid(), os.getegid())
        return {"ok": True, "killed": killed, "removed": 0}


class SpawnerClient:
    """Wrapper-side handle for a root spawner on an inherited socket."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._lock = threading.Lock()
        self._replies: queue.Queue[tuple[dict[str, object], list[int]]] = queue.Queue()
        self._exited: dict[int, int] = {}
        self._exit_events: dict[int, threading.Event] = {}
        self._handles: dict[int, ChildHandle] = {}
        self.closed = False
        self._thread = threading.Thread(target=self._read_loop, name="spawner-client", daemon=True)
        self._thread.start()

    def spawn(
        self,
        slot: int,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None = None,
    ) -> ChildHandle:
        if cwd is None:
            cwd = slot_cwd(slot)
        reply, fds = self._request(
            {"op": "spawn", "slot": slot, "argv": argv, "env": env, "cwd": cwd}
        )
        if not reply.get("ok"):
            _close_fds(fds)
            raise SpawnerError(str(reply.get("error", "bad_request")))
        if len(fds) != 3:
            _close_fds(fds)
            raise SpawnerError("missing_fds")
        isolation = str(reply.get("isolation", "per_slot_uid"))
        handle = ChildHandle(
            pid=int(reply["pid"]),  # type: ignore[arg-type]
            slot=slot,
            stdin=fds[0],
            stdout=fds[1],
            stderr=fds[2],
            isolation=isolation,
        )
        self._handles[handle.pid] = handle
        return handle

    def stop(self, pid: int) -> None:
        handle = self._handles.get(pid)
        if handle is None:
            raise SpawnerError("unknown_pid")
        handle.close_stdin()
        if self._wait_exit(pid, 2):
            return
        self.signal(pid, "TERM")
        if self._wait_exit(pid, 5):
            return
        self.signal(pid, "KILL")
        self._wait_exit(pid, 5)

    def signal(self, pid: int, sig: str) -> None:
        reply, fds = self._request({"op": "signal", "pid": pid, "sig": sig})
        _close_fds(fds)
        if not reply.get("ok"):
            raise SpawnerError(str(reply.get("error", "unknown_pid")))

    def reap_slot(self, slot: int) -> dict[str, object]:
        reply, fds = self._request({"op": "reap_slot", "slot": slot})
        _close_fds(fds)
        if not reply.get("ok"):
            raise SpawnerError(str(reply.get("error", "slot_reap_failed")))
        return reply

    def _request(self, msg: dict[str, object]) -> tuple[dict[str, object], list[int]]:
        with self._lock:
            send_message(self.sock, json.dumps(msg).encode())
            try:
                return self._replies.get(timeout=30)
            except queue.Empty as exc:
                raise SpawnerError("timeout") from exc

    def _wait_exit(self, pid: int, timeout: float) -> bool:
        event = self._exit_events.setdefault(pid, threading.Event())
        if pid in self._exited:
            event.set()
            return True
        return event.wait(timeout)

    def _note_exit(self, pid: int, status: int) -> None:
        self._exited[pid] = status
        self._exit_events.setdefault(pid, threading.Event()).set()

    def _read_loop(self) -> None:
        while True:
            try:
                payload, fds, too_long = recv_message(self.sock)
            except OSError:
                self.closed = True
                return
            if too_long:
                self._replies.put(({"ok": False, "error": "bad_request"}, fds))
                continue
            if not payload:
                self.closed = True
                _close_fds(fds)
                return
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                self._replies.put(({"ok": False, "error": "bad_request"}, fds))
                continue
            if isinstance(msg, dict) and msg.get("op") == "exited":
                _close_fds(fds)
                self._note_exit(int(msg["pid"]), int(msg["status"]))
                continue
            if not isinstance(msg, dict):
                self._replies.put(({"ok": False, "error": "bad_request"}, fds))
                continue
            self._replies.put((msg, fds))


class SpawnerServer:
    """Root side of the socketpair. One process, no network."""

    def __init__(
        self,
        sock: socket.socket,
        allowed: frozenset[str],
        *,
        wrapper_pid: int | None = None,
    ) -> None:
        self.sock = sock
        self.allowed = allowed
        self.wrapper_pid = wrapper_pid
        self.wrapper_status: int | None = None
        self.children: dict[int, int] = {}
        self.quarantined: set[int] = set()
        self.shared_uid = False
        self._setuid_ok: bool | None = None
        self._wake_r, self._wake_w = os.pipe()
        _set_cloexec(self._wake_r)
        _set_cloexec(self._wake_w)

    def serve_forever(self) -> int:
        previous = self._install_child_handler()
        try:
            while True:
                self._reap(forward=True)
                readable, _w, _x = select.select([self.sock, self._wake_r], [], [], 0.5)
                if self._wake_r in readable:
                    try:
                        os.read(self._wake_r, 256)
                    except OSError:
                        pass
                    self._reap(forward=True)
                if self.sock not in readable:
                    continue
                try:
                    payload, fds, too_long = recv_message(self.sock)
                except OSError:
                    break
                _close_fds(fds)
                if too_long:
                    self._reply({"ok": False, "error": "bad_request"}, reason="too_long")
                    continue
                if not payload:
                    break
                self._dispatch(payload)
        finally:
            self._shutdown_children()
            if previous is not None:
                signal.signal(signal.SIGCHLD, previous)
            _close_many([self._wake_r, self._wake_w])
        return self._wrapper_exit_code()

    def _dispatch(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            self._reply({"ok": False, "error": "bad_request"}, reason="invalid_json")
            return
        if not isinstance(msg, dict):
            self._reply({"ok": False, "error": "bad_request"}, reason="shape")
            return
        op = msg.get("op")
        if op == "spawn":
            self._spawn(msg)
        elif op == "signal":
            self._signal(msg)
        elif op == "reap_slot":
            self._reap_slot(msg)
        else:
            self._reply({"ok": False, "error": "bad_request"}, reason="unknown_op")

    def _spawn(self, msg: dict[str, object]) -> None:
        slot = msg.get("slot")
        argv = msg.get("argv")
        env = msg.get("env")
        cwd = msg.get("cwd")
        if type(slot) is not int or slot < 0 or slot >= MAX_SLOTS:
            self._reply({"ok": False, "error": "bad_request"}, reason="slot")
            return
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            self._reply({"ok": False, "error": "bad_request"}, reason="shape")
            return
        if not argv0_allowed(argv[0], self.allowed):
            self._reply({"ok": False, "error": "bad_request"}, reason="argv")
            return
        if cwd != slot_cwd(slot):
            self._reply({"ok": False, "error": "bad_request"}, reason="cwd")
            return
        if not _env_ok(env):
            self._reply({"ok": False, "error": "bad_request"}, reason="shape")
            return
        if slot in self.quarantined:
            self._reply({"ok": False, "error": "slot_quarantined"}, reason="quarantined")
            return
        if not self._prepare_slot(slot, send_reply=False):
            return
        uid = slot_uid(slot)
        drop = self._can_drop(uid)
        owner = uid if drop else os.geteuid()
        try:
            _reset_slot_directory(slot_cwd(slot), owner, owner if drop else os.getegid())
        except OSError:
            self._reply({"ok": False, "error": "bad_request"}, reason="cwd")
            return
        result = self._fork_child(slot, uid, list(argv), dict(env), drop)
        if result is None:
            return
        pid, isolation, fds = result
        self.children[pid] = slot
        if isolation == "shared_uid":
            self.shared_uid = True
        self._reply({"ok": True, "pid": pid, "isolation": isolation}, fds=fds)

    def _signal(self, msg: dict[str, object]) -> None:
        pid = msg.get("pid")
        sig_name = msg.get("sig")
        if type(pid) is not int or sig_name not in {"TERM", "KILL"}:
            self._reply({"ok": False, "error": "bad_request"}, reason="shape")
            return
        if pid not in self.children or not _alive(pid):
            self._reply({"ok": False, "error": "unknown_pid"})
            return
        sig = signal.SIGTERM if sig_name == "TERM" else signal.SIGKILL
        try:
            os.killpg(pid, sig)
        except OSError:
            self._reply({"ok": False, "error": "unknown_pid"})
            return
        self._reply({"ok": True})

    def _reap_slot(self, msg: dict[str, object]) -> None:
        slot = msg.get("slot")
        if type(slot) is not int or slot < 0 or slot >= MAX_SLOTS:
            self._reply({"ok": False, "error": "bad_request"}, reason="slot")
            return
        self._prepare_slot(slot, send_reply=True)

    def _prepare_slot(self, slot: int, *, send_reply: bool) -> bool:
        """Reap processes and files for ``slot``. Reply only when this is the op."""
        uid = slot_uid(slot)
        if self.shared_uid or not self._can_drop(uid):
            self.shared_uid = True
            killed = self._kill_slot_groups(slot)
            removed = 0
        else:
            killed, ok = _kill_uid_processes(uid)
            if not ok:
                self.quarantined.add(slot)
                _LOG.error("slot_reap_failed")
                self._reply({"ok": False, "error": "slot_reap_failed", "killed": killed})
                return False
            removed = 0
            for root in _OWNED_ROOTS:
                removed += _remove_owned_entries(root, uid)
        try:
            owner = os.geteuid() if self.shared_uid else uid
            group = os.getegid() if self.shared_uid else uid
            os.makedirs("/tmp/box", mode=0o711, exist_ok=True)
            _reset_slot_directory(slot_cwd(slot), owner, group)
        except OSError:
            _LOG.error("slot_reap_failed")
            self.quarantined.add(slot)
            self._reply({"ok": False, "error": "slot_reap_failed", "killed": killed})
            return False
        if send_reply:
            self._reply({"ok": True, "killed": killed, "removed": removed})
        return True

    def _kill_slot_groups(self, slot: int) -> int:
        killed = 0
        for pid, owned in list(self.children.items()):
            if owned != slot or not _alive(pid):
                continue
            _killpg_quiet(pid, signal.SIGKILL)
            killed += 1
        self._reap(forward=True)
        return killed

    def _fork_child(
        self,
        slot: int,
        uid: int,
        argv: list[str],
        env: dict[str, str],
        drop: bool,
    ) -> tuple[int, str, list[int]] | None:
        stdin_r, stdin_w = _pipe()
        stdout_r, stdout_w = _pipe()
        stderr_r, stderr_w = _pipe()
        flag_r, flag_w = _pipe()
        err_r, err_w = _pipe()
        _set_cloexec(err_w)
        pid = os.fork()
        if pid == 0:
            _close_many([stdin_w, stdout_r, stderr_r, flag_r, err_r, self.sock.fileno()])
            _exec_child(
                uid,
                slot_cwd(slot),
                argv,
                env,
                stdin_r,
                stdout_w,
                stderr_w,
                flag_w,
                err_w,
                drop,
            )
        _close_many([stdin_r, stdout_w, stderr_w, flag_w, err_w])
        kind = os.read(flag_r, 1)
        err = os.read(err_r, 64)
        _close_many([flag_r, err_r])
        if err or kind not in {b"P", b"S"}:
            _close_many([stdin_w, stdout_r, stderr_r])
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            self._reply({"ok": False, "error": "exec_failed"}, reason="exec_failed")
            return None
        isolation = "per_slot_uid" if kind == b"P" else "shared_uid"
        return pid, isolation, [stdin_w, stdout_r, stderr_r]

    def _can_drop(self, uid: int) -> bool:
        if self.shared_uid:
            return False
        if self._setuid_ok is not None:
            return self._setuid_ok
        self._setuid_ok = _probe_setuid(uid)
        if not self._setuid_ok:
            self.shared_uid = True
        return self._setuid_ok

    def _reply(
        self,
        msg: dict[str, object],
        fds: list[int] | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        if msg.get("error") == "bad_request":
            _LOG.warning("bad_request")
        files = list(fds or [])
        try:
            send_message(self.sock, json.dumps(msg).encode(), files)
        except OSError:
            _LOG.warning("reply failed")
        finally:
            _close_fds(files)

    def _install_child_handler(self) -> signal.Handlers | None:
        wake_w = self._wake_w

        def _on_child(_signum: int, _frame: object) -> None:
            try:
                os.write(wake_w, b"\0")
            except OSError:
                pass

        try:
            return signal.signal(signal.SIGCHLD, _on_child)
        except ValueError:
            # signal.signal only works on the main thread. Protocol tests run
            # the loop elsewhere and do not spawn children.
            return None

    def _reap(self, *, forward: bool) -> None:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            if pid == self.wrapper_pid:
                self.wrapper_status = status
                continue
            if pid in self.children:
                self.children.pop(pid, None)
                if forward:
                    self._reply({"op": "exited", "pid": pid, "status": status})

    def _shutdown_children(self) -> None:
        for pid in list(self.children):
            _killpg_quiet(pid, signal.SIGKILL)
        self._reap(forward=False)
        if self.wrapper_pid is not None and self.wrapper_status is None:
            try:
                _pid, status = os.waitpid(self.wrapper_pid, 0)
            except ChildProcessError:
                return
            self.wrapper_status = status

    def _wrapper_exit_code(self) -> int:
        status = self.wrapper_status
        if status is None:
            return 1
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return 1


def _exec_child(
    uid: int,
    cwd: str,
    argv: list[str],
    env: dict[str, str],
    stdin_r: int,
    stdout_w: int,
    stderr_w: int,
    flag_w: int,
    err_w: int,
    drop: bool,
) -> None:
    flag_open = True
    try:
        os.setsid()
        dropped = False
        if drop:
            try:
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
                dropped = os.geteuid() == uid
            except OSError as exc:
                if not is_eperm(exc):
                    raise
                dropped = False
        os.write(flag_w, b"P" if dropped else b"S")
        os.close(flag_w)
        flag_open = False
        resource.setrlimit(resource.RLIMIT_NPROC, (NPROC_LIMIT, NPROC_LIMIT))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.chdir(cwd)
        os.dup2(stdin_r, 0)
        os.dup2(stdout_w, 1)
        os.dup2(stderr_w, 2)
        for fd in (0, 1, 2):
            os.set_inheritable(fd, True)
        _close_except({0, 1, 2, err_w})
        os.execve(argv[0], argv, env)
        os.write(err_w, b"exec")
    except OSError as exc:
        try:
            if flag_open:
                os.write(flag_w, b"E")
                os.close(flag_w)
            os.write(err_w, str(exc.errno or 0).encode())
        except OSError:
            pass
    os._exit(127)


def _probe_setuid(uid: int) -> bool:
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCHLD})
    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
            except OSError:
                os._exit(1)
            os._exit(0 if os.geteuid() == uid else 1)
        _pid, status = os.waitpid(pid, 0)
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def _kill_uid_processes(uid: int) -> tuple[int, bool]:
    """SIGKILL every process of ``uid`` in this PID namespace. Stop after 5s."""
    import time

    deadline = time.monotonic() + _REAP_DEADLINE_SECONDS
    killed = 0
    while True:
        found = _pids_with_uid(uid)
        if not found:
            return killed, True
        if time.monotonic() >= deadline:
            return killed, False
        _kill_as_uid(uid)
        killed += len(found)
        time.sleep(0.05)


def _kill_as_uid(uid: int) -> None:
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCHLD})
    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
                if os.geteuid() != uid:
                    os._exit(1)
                os.kill(-1, signal.SIGKILL)
            except OSError:
                pass
            os._exit(0)
        os.waitpid(pid, 0)
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})


def _pids_with_uid(uid: int) -> list[int]:
    found: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            if not line.startswith("Uid:"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == str(uid):
                found.append(int(name))
            break
    return found


def _remove_owned_entries(root: str, uid: int) -> int:
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if info.st_uid != uid:
            continue
        removed += _remove_tree(entry.path)
    return removed


def _remove_tree(path: str) -> int:
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        try:
            os.unlink(path)
        except OSError:
            return 0
        return 1
    count = 0
    try:
        children = list(os.scandir(path))
    except OSError:
        children = []
    for child in children:
        count += _remove_tree(child.path)
    try:
        os.rmdir(path)
        count += 1
    except OSError:
        pass
    return count


def _reset_slot_directory(path: str, uid: int, gid: int) -> None:
    if os.path.lexists(path):
        _remove_tree(path)
    os.mkdir(path, 0o700)
    os.chown(path, uid, gid)
    os.chmod(path, 0o700)


def _env_ok(env: object) -> bool:
    if not isinstance(env, dict):
        return False
    return all(isinstance(key, str) and isinstance(value, str) for key, value in env.items())


def _detach_pipe(pipe: object) -> int:
    fileno = getattr(pipe, "fileno")
    fd = os.dup(fileno())
    pipe.close()
    return fd


def _wait_proc(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _killpg_quiet(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except OSError:
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def _pipe() -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    _set_cloexec(read_fd)
    _set_cloexec(write_fd)
    return read_fd, write_fd


def _set_cloexec(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)


def _close_fds(fds: list[int]) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _close_many(fds: list[int]) -> None:
    _close_fds(fds)


def _close_except(keep: set[int]) -> None:
    fds: list[int] = []
    try:
        fds = [int(name) for name in os.listdir("/proc/self/fd")]
    except OSError:
        fds = list(range(0, 256))
    for fd in fds:
        if fd not in keep and fd >= 3:
            try:
                os.close(fd)
            except OSError:
                pass
