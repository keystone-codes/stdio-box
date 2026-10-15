"""Lazy instances: one child per profile, eviction, backoff, and pings."""

import time
from pathlib import Path

import anyio
import pytest
from tests.support.routing import body, make_runtime, open_store, person, seed, sha8

import stdio_box.instances as instances
from stdio_box.refusals import (
    INSTANCE_FAILED,
    INSTANCE_UNAVAILABLE,
    INSTANCES_EXHAUSTED,
    UPSTREAM_TIMEOUT,
)

TOKEN_A = "token-ann-aaaaaa"
TOKEN_B = "token-bob-bbbbbb"
TOKEN_C = "token-cam-cccccc"


async def _call(forwarder: object, sub: str, tool: str, arguments: dict | None = None) -> object:
    identity = person(sub, tool=tool, jti=f"{sub}-{tool}-{time.monotonic_ns()}")
    return await forwarder.call(identity, tool, tool, arguments or {})  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_two_profiles_two_instances(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    seed(store, "ann", TOKEN_A, "user:ann@example.com")
    seed(store, "bob", TOKEN_B, "user:bob@example.com")
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store)
    async with manager.run():
        ann = await _call(forwarder, "ann@example.com", "who")
        bob = await _call(forwarder, "bob@example.com", "who")
    assert not ann.isError
    assert not bob.isError
    assert body(ann)["pid"] != body(bob)["pid"]
    assert body(ann)["token_sha8"] == sha8(TOKEN_A)
    assert body(bob)["token_sha8"] == sha8(TOKEN_B)
    store.close()


@pytest.mark.asyncio
async def test_lazy_start_and_idle_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instances, "IDLE_SWEEP_SECONDS", 0.2)
    store = open_store(tmp_path)
    profile_id = seed(store, "ann", TOKEN_A, "user:ann@example.com")
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store, idle_seconds=1)
    async with manager.run():
        assert manager.snapshot()["instances"]["running"] == 0
        result = await _call(forwarder, "ann@example.com", "who")
        assert not result.isError
        assert manager.snapshot()["instances"]["running"] == 1
        await anyio.sleep(3)
        assert manager.current(profile_id) is None
        assert manager.snapshot()["instances"]["running"] == 0
    store.close()


@pytest.mark.asyncio
async def test_limit_evicts_lru(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    ann = seed(store, "ann", TOKEN_A, "user:ann@example.com")
    bob = seed(store, "bob", TOKEN_B, "user:bob@example.com")
    cam = seed(store, "cam", TOKEN_C, "user:cam@example.com")
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store, max_instances=2)
    async with manager.run():
        first = await _call(forwarder, "ann@example.com", "who")
        second = await _call(forwarder, "bob@example.com", "who")
        third = await _call(forwarder, "cam@example.com", "who")
        assert not first.isError and not second.isError and not third.isError
        assert manager.current(ann) is None
        assert manager.current(bob) is not None
        assert manager.current(cam) is not None
        assert manager.snapshot()["instance_evictions_total"] == 1

        async def _slow(sub: str) -> None:
            await _call(forwarder, sub, "slow", {"seconds": 5})

        async with anyio.create_task_group() as tg:
            tg.start_soon(_slow, "bob@example.com")
            tg.start_soon(_slow, "cam@example.com")
            for _ in range(100):
                busy = [
                    item
                    for item in (manager.current(bob), manager.current(cam))
                    if item is not None and item.in_flight > 0
                ]
                if len(busy) == 2:
                    break
                await anyio.sleep(0.05)
            blocked = await _call(forwarder, "ann@example.com", "who")
            assert body(blocked)["code"] == INSTANCES_EXHAUSTED
    store.close()


@pytest.mark.asyncio
async def test_crash_backoff_and_recover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instances, "BACKOFF_INITIAL_SECONDS", 0.4)
    store = open_store(tmp_path)
    seed(store, "ann", TOKEN_A, "user:ann@example.com")
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store)
    async with manager.run():
        started = await _call(forwarder, "ann@example.com", "who")
        assert not started.isError
        first_pid = body(started)["pid"]
        crashed = await _call(forwarder, "ann@example.com", "crash")
        assert crashed.isError
        waiting = await _call(forwarder, "ann@example.com", "who")
        payload = body(waiting)
        assert payload["code"] == INSTANCE_UNAVAILABLE
        assert payload["retry_after_seconds"] >= 1
        await anyio.sleep(payload["retry_after_seconds"] + 0.3)
        recovered = await _call(forwarder, "ann@example.com", "who")
        assert not recovered.isError
        assert body(recovered)["pid"] != first_pid
    store.close()


@pytest.mark.asyncio
async def test_three_missed_pings_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(instances, "PING_INTERVAL_SECONDS", 0.3)
    monkeypatch.setattr(instances, "PING_TIMEOUT_SECONDS", 0.3)
    caplog.set_level("WARNING")
    store = open_store(tmp_path)
    profile_id = seed(store, "ann", TOKEN_A, "user:ann@example.com")
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store, call_timeout=30)
    async with manager.run():
        started = time.monotonic()
        with anyio.fail_after(12):
            blocked = await _call(forwarder, "ann@example.com", "block", {"seconds": 8})
        assert time.monotonic() - started < 8
        assert blocked.isError
        assert "instance echo/ann missed ping 3/3" in caplog.text
        again = await _call(forwarder, "ann@example.com", "who")
        assert not again.isError
        inst = manager.current(profile_id)
        assert inst is not None
        assert inst.generation >= 2
    store.close()


@pytest.mark.asyncio
async def test_single_missed_ping_no_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "PING_INTERVAL_SECONDS", 0.3)
    monkeypatch.setattr(instances, "PING_TIMEOUT_SECONDS", 0.3)
    store = open_store(tmp_path)
    profile_id = seed(store, "ann", TOKEN_A, "user:ann@example.com")
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store)
    async with manager.run():
        result = await _call(forwarder, "ann@example.com", "block", {"seconds": 0.5})
        assert not result.isError
        assert body(result)["blocked"] == 0.5
        inst = manager.current(profile_id)
        assert inst is not None
        assert inst.generation == 1
        assert inst.state == "running"
    store.close()


@pytest.mark.asyncio
async def test_five_failed_starts_mark_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "BACKOFF_INITIAL_SECONDS", 0)
    store = open_store(tmp_path)
    seed(store, "ann", TOKEN_A, "user:ann@example.com")
    missing = tmp_path / "missing-server"
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store, argv=[str(missing)])
    async with manager.run():
        codes = []
        for _ in range(5):
            result = await _call(forwarder, "ann@example.com", "who")
            codes.append(body(result)["code"])
        assert codes == [INSTANCE_UNAVAILABLE] * 4 + [INSTANCE_FAILED]
    store.close()


@pytest.mark.asyncio
async def test_child_env_is_exact(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    seed(store, "ann", TOKEN_A, "user:ann@example.com")
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store)
    async with manager.run():
        result = await _call(forwarder, "ann@example.com", "who")
    assert not result.isError
    keys = [key for key in body(result)["env_keys"] if key != "__CF_USER_TEXT_ENCODING"]
    assert keys == ["ECHO_TOKEN", "ECHO_URL", "HOME", "LANG", "PATH", "TMPDIR"]
    store.close()


@pytest.mark.asyncio
async def test_call_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instances, "CALL_ACQUIRE_TIMEOUT_SECONDS", 2)
    store = open_store(tmp_path)
    seed(store, "ann", TOKEN_A, "user:ann@example.com")
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store, call_timeout=1)
    async with manager.run():
        started = time.monotonic()
        result = await _call(forwarder, "ann@example.com", "slow", {"seconds": 5})
        assert time.monotonic() - started < 4
        assert body(result)["code"] == UPSTREAM_TIMEOUT
    store.close()


def test_form_counters_stay_on_the_status_writer(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    _provider, _catalog, manager, _forwarder = make_runtime(tmp_path, store)
    before = store.revision()
    manager.note_form_issued()
    manager.note_form_submitted("saved")
    manager.note_form_submitted("rejected")
    snapshot = manager.snapshot()
    assert snapshot["forms_issued_total"] == 1
    assert snapshot["forms_submitted_total"] == {"saved": 1, "rejected": 1}
    store.write_status(snapshot)
    assert store.revision() == before
    assert store.read_status()["forms_issued_total"] == 1
    store.close()
