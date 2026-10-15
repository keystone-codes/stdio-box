"""One child process per profile: lazy start, idle stop, eviction, and restart.

Stop order is closing the session first, then signalling the process. In-flight
calls must see the session close before a new call can observe the same object.
"""

import logging
import os
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Protocol

import anyio
import mcp.types as types
from mcp import ClientSession
from mcp.shared.exceptions import McpError

from stdio_box.catalog import Catalog
from stdio_box.child_io import open_child_session
from stdio_box.ids import slot_cwd
from stdio_box.provider import Provider
from stdio_box.refusals import (
    INSTANCE_BUSY,
    INSTANCE_FAILED,
    INSTANCE_UNAVAILABLE,
    INSTANCES_EXHAUSTED,
    UPSTREAM_TIMEOUT,
    Refusal,
)
from stdio_box.resolver import reentry_refusal, retry_after_seconds
from stdio_box.runtime import get_install, get_isolation, note_child_isolation
from stdio_box.spawner import ChildHandle, SpawnerError
from stdio_box.stderr import StderrPump, stderr_suppressed_total
from stdio_box.store.errors import SecretUnreadable
from stdio_box.store.repo import Profile, Store

_LOG = logging.getLogger("stdio_box.instances")

# Intervals and thresholds that are not BOX_* settings. Tests replace them.
CALL_CONCURRENCY = 8
CALL_ACQUIRE_TIMEOUT_SECONDS = 30
PING_INTERVAL_SECONDS = 15
PING_TIMEOUT_SECONDS = 5
PING_MISSES = 3
BACKOFF_INITIAL_SECONDS = 1
BACKOFF_MAX_SECONDS = 60
STABLE_RESET_SECONDS = 60
FAILED_START_LIMIT = 5
RESTART_DRAIN_SECONDS = 30
IDLE_SWEEP_SECONDS = 30
STATUS_INTERVAL_SECONDS = 10
REVISION_POLL_SECONDS = 2
AUDIT_PURGE_INTERVAL_SECONDS = 3600
SESSION_CLOSE_SECONDS = 3

_CONNECTION_CLOSED = -32000
_REQUEST_TIMEOUT = 408


class InstanceError(Exception):
    def __init__(self, refusal: Refusal) -> None:
        self.refusal = refusal
        super().__init__(refusal.code)


class Spawner(Protocol):
    def spawn(
        self,
        slot: int,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None = None,
    ) -> ChildHandle: ...

    def stop(self, pid: int) -> None: ...

    def reap_slot(self, slot: int) -> dict[str, object]: ...


class Instance:
    """One start of a profile. ``generation`` increases on every start attempt."""

    def __init__(self, profile_id: str, version: int, generation: int, slot: int) -> None:
        self.profile_id = profile_id
        self.version = version
        self.generation = generation
        self.slot = slot
        self.state = "starting"
        self.pid: int | None = None
        self.session: ClientSession | None = None
        self.handle: ChildHandle | None = None
        self.pump: StderrPump | None = None
        self.semaphore: anyio.Semaphore | None = None
        self.closed: anyio.Event | None = None
        self.owner_done: anyio.Event | None = None
        self.owner_started = False
        self.last_call_at = time.monotonic()
        self.started_at = 0.0
        self.in_flight = 0
        self.missed_pings = 0
        self.retry_at = 0.0
        self.tool_names: tuple[str, ...] = ()


def build_child_launch(
    base_argv: list[str],
    *,
    slot: int,
    slot_root: str,
    provider: Provider,
    profile: Profile,
    secrets: Mapping[str, object],
) -> tuple[list[str], dict[str, str], tuple[str, ...]]:
    """Argv, environment, and values the stderr pump must mask.

    The environment is only the slot paths, ``LANG``, ``static_env``, and the
    fields of this variant. Fields that belong to another variant are omitted.
    """
    home = slot_cwd(slot, slot_root)
    argv = list(base_argv)
    bindir = os.path.dirname(os.path.abspath(argv[0])) if argv else ""
    env: dict[str, str] = {
        "PATH": f"{bindir}:/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": f"{home}/tmp",
        "LANG": "C.UTF-8",
    }
    for key, value in provider.server.static_env:
        env[key] = value
    allowed = set()
    for variant in provider.variants:
        if variant.name == profile.variant:
            allowed = set(variant.fields)
            break
    masks: list[str] = []
    fields = {field.name: field for field in provider.fields}
    for name in allowed:
        field = fields.get(name)
        if field is None:
            continue
        if field.secret:
            if name not in secrets:
                continue
            raw = secrets[name]
        else:
            if name not in profile.public_fields:
                continue
            raw = profile.public_fields[name]
        value = str(raw)
        masks.append(value)
        if field.arg:
            argv.extend([field.arg, value])
        else:
            env[name] = value
    return argv, env, tuple(masks)


class InstanceManager:
    """The process-wide table of profile servers."""

    def __init__(
        self,
        spawner: Spawner,
        store: Store,
        provider: Provider,
        catalog: Catalog,
        argv: list[str],
        *,
        snapshot_names: frozenset[str],
        max_instances: int,
        idle_seconds: float,
        start_timeout_seconds: float,
        slot_root: str = "/tmp/box",
    ) -> None:
        self._spawner = spawner
        self._store = store
        self._provider = provider
        self._catalog = catalog
        self._argv = list(argv)
        self._snapshot_names = snapshot_names
        self._max_instances = max_instances
        self._idle_seconds = idle_seconds
        self._start_timeout = start_timeout_seconds
        self._slot_root = slot_root
        self._guard = anyio.Lock()
        self._locks: dict[str, anyio.Lock] = {}
        self._current: dict[str, Instance] = {}
        self._slots: dict[int, str] = {}
        self._quarantine: set[int] = set()
        self._generations: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._delay: dict[str, float] = {}
        self._calls = {"ok": 0, "error": 0, "refused": 0}
        self._refusals: dict[str, int] = {}
        self._starts = 0
        self._crashes = 0
        self._evictions = 0
        self.forms_issued_total = 0
        self.forms_submitted_total: dict[str, int] = {}
        self._tg: object | None = None

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with anyio.create_task_group() as tg:
            self._tg = tg
            tg.start_soon(self._ping_loop)
            tg.start_soon(self._idle_loop)
            tg.start_soon(self._status_loop)
            tg.start_soon(self._revision_loop)
            tg.start_soon(self._purge_loop)
            try:
                yield
            finally:
                await self.shutdown()
                tg.cancel_scope.cancel()
                self._tg = None

    def current(self, profile_id: str) -> Instance | None:
        return self._current.get(profile_id)

    def reset_failed(self, profile_id: str) -> None:
        """Clear a profile that exhausted its start attempts.

        A credential check or a rotation calls this. The next call may start again.
        """
        self._failures.pop(profile_id, None)
        self._delay.pop(profile_id, None)
        inst = self._current.get(profile_id)
        if inst is not None and inst.state == "failed":
            self._current.pop(profile_id, None)

    def note_call(self, outcome: str) -> None:
        self._calls[outcome] = self._calls.get(outcome, 0) + 1

    def note_refusal(self, code: str) -> None:
        self._refusals[code] = self._refusals.get(code, 0) + 1
        self.note_call("refused")

    def snapshot(self) -> dict[str, object]:
        by_state = {"starting": 0, "running": 0, "stopping": 0, "backoff": 0, "failed": 0}
        running = 0
        for inst in self._current.values():
            if inst.state in by_state:
                by_state[inst.state] += 1
            if inst.state == "running":
                running += 1
        return {
            "instances": {
                "running": running,
                "limit": self._max_instances,
                "by_state": by_state,
            },
            "calls_total": dict(self._calls),
            "refusals_total": dict(self._refusals),
            "instance_starts_total": self._starts,
            "instance_crashes_total": self._crashes,
            "instance_evictions_total": self._evictions,
            "stderr_suppressed_total": stderr_suppressed_total.value,
            "forms_issued_total": self.forms_issued_total,
            "forms_submitted_total": dict(self.forms_submitted_total),
            "store": {"key_state": self._store.key_state},
            "isolation": get_isolation(),
            "install": get_install(),
        }

    async def ensure_running(self, profile: Profile) -> Instance:
        async with await self._profile_lock(profile.id):
            return await self._ensure_locked(profile)

    async def restart_profile(self, profile_id: str) -> None:
        """Let in-flight calls finish, then stop. New calls wait for a new start."""
        self.reset_failed(profile_id)
        async with await self._profile_lock(profile_id):
            inst = self._current.get(profile_id)
            if inst is None:
                return
            if inst.state == "running":
                await self._drain(inst, RESTART_DRAIN_SECONDS)
                await self._stop_locked(inst, reason="restart", backoff=False)
            elif inst.state in {"backoff", "failed"}:
                self._current.pop(profile_id, None)

    async def invoke(
        self,
        inst: Instance,
        name: str,
        arguments: dict[str, object],
        timeout_seconds: float,
        meta: dict[str, str] | None,
    ) -> types.CallToolResult:
        if inst.state != "running" or inst.session is None or inst.semaphore is None:
            raise InstanceError(_unavailable(1))
        inst.in_flight += 1
        inst.last_call_at = time.monotonic()
        acquired = False
        try:
            try:
                with anyio.fail_after(CALL_ACQUIRE_TIMEOUT_SECONDS):
                    await inst.semaphore.acquire()
            except TimeoutError as exc:
                raise InstanceError(_busy()) from exc
            acquired = True
            if inst.state != "running" or inst.session is None:
                raise InstanceError(_unavailable(1))
            return await _call_child(inst.session, name, arguments, timeout_seconds, meta)
        finally:
            if acquired and inst.semaphore is not None:
                inst.semaphore.release()
            inst.in_flight = max(0, inst.in_flight - 1)
            inst.last_call_at = time.monotonic()

    async def observe_call_error(self, inst: Instance, exc: BaseException) -> bool:
        """Record a crash when the child is gone. Return whether it is gone."""
        lost = _child_lost(exc) or self._process_dead(inst.pid)
        if not lost or inst.state != "running":
            return lost
        await self._on_child_exit(inst)
        return True

    async def shutdown(self) -> None:
        for profile_id in list(self._current):
            try:
                await self._stop_profile(profile_id)
            except Exception:
                _LOG.warning("shutdown stop failed for %s", profile_id)

    async def _ensure_locked(self, profile: Profile) -> Instance:
        inst = self._current.get(profile.id)
        if inst is not None and inst.version != profile.version:
            self._failures.pop(profile.id, None)
            if inst.state == "running":
                await self._drain(inst, RESTART_DRAIN_SECONDS)
                await self._stop_locked(inst, reason="restart", backoff=False)
            elif inst.state in {"backoff", "failed"}:
                self._current.pop(profile.id, None)
            inst = self._current.get(profile.id)
        if inst is not None and inst.state == "failed":
            raise InstanceError(_failed(profile.id))
        if inst is not None and inst.state == "backoff":
            remaining = inst.retry_at - time.monotonic()
            if remaining > 0:
                raise InstanceError(_unavailable(remaining))
        if (
            inst is not None
            and inst.state == "running"
            and inst.version == profile.version
            and inst.session is not None
        ):
            return inst
        return await self._start_locked(profile)

    async def _start_locked(self, profile: Profile) -> Instance:
        if not self._argv:
            raise InstanceError(_failed(profile.id))
        try:
            secrets = await anyio.to_thread.run_sync(lambda: self._store.read_secrets(profile.id))
        except SecretUnreadable as exc:
            raise InstanceError(reentry_refusal(profile)) from exc
        slot = await self._take_slot(profile.id)
        argv, env, masks = build_child_launch(
            self._argv,
            slot=slot,
            slot_root=self._slot_root,
            provider=self._provider,
            profile=profile,
            secrets=secrets,
        )
        generation = self._generations.get(profile.id, 0) + 1
        self._generations[profile.id] = generation
        inst = Instance(profile.id, profile.version, generation, slot)
        inst.semaphore = anyio.Semaphore(CALL_CONCURRENCY)
        inst.closed = anyio.Event()
        inst.owner_done = anyio.Event()
        inst.owner_started = False
        self._current[profile.id] = inst
        self._starts += 1
        home = slot_cwd(slot, self._slot_root)
        try:
            await anyio.to_thread.run_sync(lambda: self._spawner.reap_slot(slot))
            handle = await anyio.to_thread.run_sync(
                lambda: self._spawner.spawn(slot, argv, env, home)
            )
            inst.handle = handle
            inst.pid = handle.pid
            note_child_isolation(handle.isolation)
            tmp = os.path.join(home, "tmp")
            await anyio.to_thread.run_sync(lambda: os.makedirs(tmp, mode=0o700, exist_ok=True))
            inst.pump = StderrPump(handle.take_stderr(), profile.id, generation, masks, slot)
            inst.pump.start()
            if self._tg is None:
                raise RuntimeError("instance manager is not running")
            ready = anyio.Event()
            inst.owner_started = True
            self._tg.start_soon(self._own_session, inst, ready)
            await ready.wait()
            if inst.session is None:
                raise RuntimeError("child session did not open")
            with anyio.fail_after(self._start_timeout):
                await inst.session.initialize()
                listed = await inst.session.list_tools()
            names = tuple(tool.name for tool in listed.tools)
            inst.tool_names = names
            self._warn_drift(profile, set(names))
            inst.state = "running"
            inst.started_at = time.monotonic()
            inst.last_call_at = inst.started_at
            self._failures.pop(profile.id, None)
            return inst
        except InstanceError:
            raise
        except Exception as exc:
            _LOG.warning("instance %s failed to start (%s)", profile.id, type(exc).__name__)
            refusal = await self._abort_start(inst)
            raise InstanceError(refusal) from None

    async def _own_session(self, inst: Instance, ready: anyio.Event) -> None:
        if inst.handle is None:
            ready.set()
            if inst.owner_done is not None:
                inst.owner_done.set()
            return
        try:
            async with open_child_session(inst.handle) as session:
                _arm_cancel(session)
                inst.session = session
                ready.set()
                if inst.closed is not None:
                    await inst.closed.wait()
                # Fail in-flight calls before the session task is cancelled.
                # Cancellation drops the receive loop's cleanup, so a blocked
                # tools/call would otherwise wait out its own timeout.
                await _fail_pending(session)
        except Exception:
            if not ready.is_set():
                _LOG.warning("child session failed to open for %s", inst.profile_id)
            if not ready.is_set():
                ready.set()
        finally:
            inst.session = None
            if inst.owner_done is not None and not inst.owner_done.is_set():
                inst.owner_done.set()

    async def _abort_start(self, inst: Instance) -> Refusal:
        await self._close_session(inst)
        if inst.pid is not None:
            await self._stop_pid(inst.pid)
        if inst.slot >= 0:
            quarantined = await self._reap(inst.slot)
            self._slots.pop(inst.slot, None)
            if quarantined:
                self._quarantine.add(inst.slot)
                _LOG.warning("slot %s quarantined", inst.slot)
        if inst.pump is not None:
            await anyio.to_thread.run_sync(inst.pump.close)
            inst.pump = None
        inst.pid = None
        count = self._failures.get(inst.profile_id, 0) + 1
        self._failures[inst.profile_id] = count
        if count >= FAILED_START_LIMIT:
            inst.state = "failed"
            self._current[inst.profile_id] = inst
            return _failed(inst.profile_id)
        self._enter_backoff(inst)
        return _unavailable(max(0.0, inst.retry_at - time.monotonic()))

    def _enter_backoff(self, inst: Instance) -> None:
        if inst.started_at and time.monotonic() - inst.started_at >= STABLE_RESET_SECONDS:
            delay = BACKOFF_INITIAL_SECONDS
        else:
            delay = self._delay.get(inst.profile_id, BACKOFF_INITIAL_SECONDS)
        self._delay[inst.profile_id] = min(delay * 2, BACKOFF_MAX_SECONDS)
        inst.state = "backoff"
        inst.retry_at = time.monotonic() + delay
        inst.session = None
        inst.pid = None
        self._current[inst.profile_id] = inst

    async def _take_slot(self, profile_id: str) -> int:
        for _ in range(self._max_instances + 1):
            free = self._free_slot()
            if free is not None:
                self._slots[free] = profile_id
                return free
            victim = self._oldest_idle()
            if victim is None:
                raise InstanceError(_exhausted())
            await self._evict(victim)
        raise InstanceError(_exhausted())

    def _free_slot(self) -> int | None:
        for slot in range(self._max_instances):
            if slot in self._quarantine or slot in self._slots:
                continue
            return slot
        return None

    def _oldest_idle(self) -> Instance | None:
        idle = [
            inst
            for inst in self._current.values()
            if inst.state == "running" and inst.in_flight == 0 and inst.slot >= 0
        ]
        if not idle:
            return None
        return min(idle, key=lambda inst: (inst.last_call_at, inst.profile_id))

    async def _evict(self, victim: Instance) -> None:
        async with await self._profile_lock(victim.profile_id):
            if victim.state != "running" or victim.in_flight != 0:
                return
            await self._stop_locked(victim, reason="evicted", backoff=False)

    async def _stop_profile(self, profile_id: str) -> None:
        async with await self._profile_lock(profile_id):
            inst = self._current.get(profile_id)
            if inst is None:
                return
            if inst.state in {"backoff", "failed"}:
                self._current.pop(profile_id, None)
                return
            if inst.state in {"stopped"}:
                self._current.pop(profile_id, None)
                return
            await self._stop_locked(inst, reason="stop", backoff=False)

    async def _stop_locked(self, inst: Instance, *, reason: str, backoff: bool) -> None:
        if inst.state in {"stopping", "stopped"}:
            return
        inst.state = "stopping"
        # Close the session before signals so in-flight calls fail on this object
        # and cannot keep using it after the process is asked to exit.
        await self._close_session(inst)
        if inst.pid is not None:
            await self._stop_pid(inst.pid)
        quarantined = False
        if inst.slot >= 0:
            quarantined = await self._reap(inst.slot)
            self._slots.pop(inst.slot, None)
            if quarantined:
                self._quarantine.add(inst.slot)
                _LOG.warning("slot %s quarantined", inst.slot)
        if inst.pump is not None:
            await anyio.to_thread.run_sync(inst.pump.close)
            inst.pump = None
        if reason == "evicted":
            self._evictions += 1
            _LOG.info("evicted %s", inst.profile_id)
            await self._record_instance(inst, "evicted")
        if reason == "crash":
            self._crashes += 1
        if backoff:
            self._enter_backoff(inst)
            return
        if self._current.get(inst.profile_id) is inst:
            self._current.pop(inst.profile_id, None)
        inst.state = "stopped"
        inst.pid = None

    async def _close_session(self, inst: Instance) -> None:
        if not inst.owner_started:
            inst.session = None
            return
        if inst.closed is not None and not inst.closed.is_set():
            inst.closed.set()
        if inst.owner_done is not None and not inst.owner_done.is_set():
            with anyio.move_on_after(SESSION_CLOSE_SECONDS):
                await inst.owner_done.wait()
        inst.session = None

    async def _drain(self, inst: Instance, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while inst.in_flight > 0 and time.monotonic() < deadline:
            await anyio.sleep(0.05)

    async def _on_child_exit(self, inst: Instance) -> None:
        async with await self._profile_lock(inst.profile_id):
            if inst.state != "running":
                return
            await self._stop_locked(inst, reason="crash", backoff=True)

    async def _restart_after_pings(self, profile_id: str) -> None:
        profile = await anyio.to_thread.run_sync(lambda: self._store.get_profile(profile_id))
        async with await self._profile_lock(profile_id):
            inst = self._current.get(profile_id)
            if inst is None or inst.state != "running":
                return
            self._crashes += 1
            await self._stop_locked(inst, reason="ping", backoff=False)
            if profile is None or profile.state in {"needs_reentry", "awaiting_secrets"}:
                return
            try:
                await self._start_locked(profile)
            except InstanceError:
                _LOG.warning("restart after missed pings did not start %s", profile_id)

    async def _ping_loop(self) -> None:
        try:
            while True:
                await anyio.sleep(PING_INTERVAL_SECONDS)
                for inst in list(self._current.values()):
                    if inst.state != "running" or inst.session is None:
                        continue
                    try:
                        await self._ping_one(inst)
                    except Exception:
                        _LOG.warning("ping failed for %s", inst.profile_id)
        except anyio.get_cancelled_exc_class():
            return

    async def _ping_one(self, inst: Instance) -> None:
        session = inst.session
        if session is None or inst.state != "running":
            return
        try:
            await session.send_request(
                types.ClientRequest(types.PingRequest()),
                types.EmptyResult,
                request_read_timeout_seconds=timedelta(seconds=PING_TIMEOUT_SECONDS),
            )
        except McpError as exc:
            if not _is_timeout(exc) and not _child_lost(exc):
                raise
            if _child_lost(exc) or self._process_dead(inst.pid):
                await self.observe_call_error(inst, exc)
                return
            inst.missed_pings += 1
            _LOG.warning(
                "instance %s missed ping %s/3",
                inst.profile_id,
                inst.missed_pings,
            )
            if inst.missed_pings >= PING_MISSES and self._tg is not None:
                inst.missed_pings = 0
                self._tg.start_soon(self._restart_after_pings, inst.profile_id)
            return
        except Exception as exc:
            if _child_lost(exc) or self._process_dead(inst.pid):
                await self.observe_call_error(inst, exc)
            return
        inst.missed_pings = 0

    async def _idle_loop(self) -> None:
        try:
            while True:
                await anyio.sleep(IDLE_SWEEP_SECONDS)
                now = time.monotonic()
                victims = [
                    inst
                    for inst in list(self._current.values())
                    if inst.state == "running"
                    and inst.in_flight == 0
                    and now - inst.last_call_at >= self._idle_seconds
                ]
                for inst in victims:
                    try:
                        async with await self._profile_lock(inst.profile_id):
                            if (
                                inst.state == "running"
                                and inst.in_flight == 0
                                and time.monotonic() - inst.last_call_at >= self._idle_seconds
                            ):
                                await self._stop_locked(inst, reason="idle", backoff=False)
                    except Exception:
                        _LOG.warning("idle stop failed for %s", inst.profile_id)
        except anyio.get_cancelled_exc_class():
            return

    async def _status_loop(self) -> None:
        try:
            while True:
                await self._write_status()
                await anyio.sleep(STATUS_INTERVAL_SECONDS)
        except anyio.get_cancelled_exc_class():
            return

    async def _write_status(self) -> None:
        body = self.snapshot()

        def _write(payload: dict[str, object] = body) -> None:
            self._store.write_status(payload)

        try:
            await anyio.to_thread.run_sync(_write)
        except Exception:
            _LOG.warning("status snapshot failed")

    async def _revision_loop(self) -> None:
        try:
            seen = await anyio.to_thread.run_sync(self._store.revision)
        except Exception:
            seen = 0
        try:
            while True:
                await anyio.sleep(REVISION_POLL_SECONDS)
                try:
                    current = await anyio.to_thread.run_sync(self._store.revision)
                except Exception:
                    _LOG.warning("revision read failed")
                    continue
                if current == seen:
                    continue
                seen = current
                await self._apply_revision()
        except anyio.get_cancelled_exc_class():
            return

    async def _apply_revision(self) -> None:
        for inst in list(self._current.values()):
            if inst.state not in {"running", "starting", "backoff"}:
                continue
            profile_id = inst.profile_id
            profile = await anyio.to_thread.run_sync(
                lambda profile_id=profile_id: self._store.get_profile(profile_id)
            )
            if profile is None or profile.state in {"needs_reentry", "awaiting_secrets"}:
                await self._stop_profile(profile_id)
            elif profile.version != inst.version:
                await self.restart_profile(profile_id)

    async def _purge_loop(self) -> None:
        try:
            while True:
                await anyio.sleep(AUDIT_PURGE_INTERVAL_SECONDS)
                try:
                    await anyio.to_thread.run_sync(self._store.purge)
                except Exception:
                    _LOG.warning("audit purge failed")
        except anyio.get_cancelled_exc_class():
            return

    async def _record_instance(self, inst: Instance, code: str) -> None:
        def _write() -> None:
            self._store.record(
                "instance",
                outcome="ok",
                code=code,
                profile_id=inst.profile_id,
                detail={"slot": inst.slot, "generation": inst.generation},
            )

        try:
            await anyio.to_thread.run_sync(_write)
        except Exception:
            _LOG.warning("instance audit failed")

    def _warn_drift(self, profile: Profile, live: set[str]) -> None:
        try:
            expected = {
                name
                for name in self._snapshot_names
                if self._catalog.in_variant(name, profile.variant)
            }
        except Exception:
            _LOG.warning("tools_snapshot_drift could not be compared for %s", profile.id)
            return
        missing = sorted(expected - live)
        extra = sorted(live - self._snapshot_names)
        if not missing and not extra:
            return
        _LOG.warning(
            "tools_snapshot_drift extra=%s missing=%s",
            extra,
            missing,
        )

    async def _profile_lock(self, profile_id: str) -> anyio.Lock:
        async with self._guard:
            lock = self._locks.get(profile_id)
            if lock is None:
                lock = anyio.Lock()
                self._locks[profile_id] = lock
            return lock

    async def _stop_pid(self, pid: int) -> None:
        def _stop() -> None:
            try:
                self._spawner.stop(pid)
            except SpawnerError:
                return

        await anyio.to_thread.run_sync(_stop)

    async def _reap(self, slot: int) -> bool:
        def _run() -> bool:
            try:
                result = self._spawner.reap_slot(slot)
            except SpawnerError:
                return True
            return isinstance(result, dict) and result.get("ok") is False

        return await anyio.to_thread.run_sync(_run)

    def _process_dead(self, pid: int | None) -> bool:
        if pid is None:
            return False
        exited = getattr(self._spawner, "_exited", None)
        if isinstance(exited, dict) and pid in exited:
            return True
        procs = getattr(self._spawner, "_procs", None)
        if isinstance(procs, dict) and pid in procs:
            proc = procs[pid]
            return proc.poll() is not None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False


def _unavailable(remaining: float) -> Refusal:
    return Refusal(
        INSTANCE_UNAVAILABLE,
        "This account's server is restarting.",
        "Retry after the number of seconds in retry_after_seconds.",
        retryable=True,
        data={"retry_after_seconds": retry_after_seconds(remaining)},
    )


def _failed(profile_id: str) -> Refusal:
    return Refusal(
        INSTANCE_FAILED,
        f"The server for account {profile_id} failed to start.",
        "Check the account, then run a credential check or rotate its secrets.",
    )


def _exhausted() -> Refusal:
    return Refusal(
        INSTANCES_EXHAUSTED,
        "Every server slot is in use.",
        "Retry shortly, or raise BOX_MAX_INSTANCES.",
        retryable=True,
    )


def _busy() -> Refusal:
    return Refusal(
        INSTANCE_BUSY,
        "This account's server is already handling too many calls.",
        "Retry shortly.",
        retryable=True,
    )


def _upstream_timeout() -> Refusal:
    return Refusal(
        UPSTREAM_TIMEOUT,
        "The server did not answer in time.",
        "Try a shorter request, or raise BOX_CALL_TIMEOUT_SECONDS.",
        retryable=True,
    )


def _is_timeout(exc: McpError) -> bool:
    return getattr(exc.error, "code", None) == _REQUEST_TIMEOUT


def _child_lost(exc: BaseException) -> bool:
    if isinstance(exc, McpError) and getattr(exc.error, "code", None) == _CONNECTION_CLOSED:
        return True
    return isinstance(
        exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)
    )


def _is_tools_call(request: object) -> bool:
    root = getattr(request, "root", None)
    return getattr(root, "method", None) == "tools/call"


def _arm_cancel(session: ClientSession) -> None:
    """Send ``notifications/cancelled`` when a tool call times out or is cancelled."""
    original = session.send_request

    async def send_request(
        request: object,
        result_type: type[object],
        request_read_timeout_seconds: timedelta | None = None,
        metadata: object = None,
        progress_callback: object = None,
    ) -> object:
        request_id = session._request_id  # noqa: SLF001
        try:
            return await original(
                request,
                result_type,
                request_read_timeout_seconds=request_read_timeout_seconds,
                metadata=metadata,
                progress_callback=progress_callback,
            )
        except McpError as exc:
            if _is_timeout(exc) and _is_tools_call(request):
                await _notify_cancelled(session, request_id)
            raise
        except anyio.get_cancelled_exc_class():
            if _is_tools_call(request):
                with anyio.CancelScope(shield=True):
                    await _notify_cancelled(session, int(request_id))
            raise

    session.send_request = send_request  # type: ignore[method-assign]


async def _fail_pending(session: ClientSession) -> None:
    """Unblock ``send_request`` waiters with a connection-closed error."""
    streams = getattr(session, "_response_streams", None)
    if not isinstance(streams, dict):
        return
    for request_id, stream in list(streams.items()):
        try:
            await stream.send(
                types.JSONRPCError(
                    jsonrpc="2.0",
                    id=request_id,
                    error=types.ErrorData(code=_CONNECTION_CLOSED, message="Connection closed"),
                )
            )
        except Exception:
            continue


async def _notify_cancelled(session: ClientSession, request_id: int) -> None:
    try:
        await session.send_notification(
            types.ClientNotification(
                types.CancelledNotification(
                    params=types.CancelledNotificationParams(requestId=request_id)
                )
            )
        )
    except Exception:
        return


async def _call_child(
    session: ClientSession,
    name: str,
    arguments: dict[str, object],
    timeout_seconds: float,
    meta: dict[str, str] | None,
) -> types.CallToolResult:
    payload: dict[str, object] = {"name": name, "arguments": arguments}
    if meta:
        payload["_meta"] = meta
    params = types.CallToolRequestParams.model_validate(payload)
    try:
        result = await session.send_request(
            types.ClientRequest(types.CallToolRequest(params=params)),
            types.CallToolResult,
            request_read_timeout_seconds=timedelta(seconds=timeout_seconds),
        )
    except McpError as exc:
        if _is_timeout(exc):
            raise InstanceError(_upstream_timeout()) from exc
        raise
    if not isinstance(result, types.CallToolResult):
        raise RuntimeError("child returned an unexpected result")
    return result
