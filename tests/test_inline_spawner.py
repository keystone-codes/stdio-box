"""The inline spawner runs the echo server and stop kills its process group."""

import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import anyio

from stdio_box.child_io import open_child_session
from stdio_box.spawner import InlineSpawner, allowlist_for
from stdio_box.stderr import StderrPump

ECHO_SERVER = Path(__file__).resolve().parent / "support" / "echo_server.py"


async def test_inline_who_and_stop_kills_the_group(tmp_path: Path) -> None:
    spawner = InlineSpawner(allowlist_for(sys.executable), slot_root=str(tmp_path))
    handle = spawner.spawn(
        0,
        [sys.executable, str(ECHO_SERVER)],
        {"ECHO_TOKEN": "inline-token", "ECHO_URL": "https://echo.invalid", "PATH": "/usr/bin:/bin"},
    )
    pump = StderrPump(handle.take_stderr(), "echo/inline", 1, ["inline-token"], handle.slot)
    pump.start()
    try:
        async with open_child_session(handle) as session:
            await session.initialize()
            who = await session.call_tool("who", {})
            assert who.isError is False
            assert who.structuredContent is not None
            assert who.structuredContent["pid"] == handle.pid
            started = anyio.Event()

            async def block() -> None:
                started.set()
                with anyio.CancelScope(shield=False):
                    try:
                        await session.call_tool(
                            "block",
                            {"seconds": 30},
                            read_timeout_seconds=timedelta(seconds=15),
                        )
                    except Exception:
                        return

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(block)
                await started.wait()
                await anyio.sleep(0.2)
                started_at = time.monotonic()
                await anyio.to_thread.run_sync(spawner.stop, handle.pid)
                assert time.monotonic() - started_at < 8
                task_group.cancel_scope.cancel()
    finally:
        pump.close()
    with anyio.fail_after(2):
        pass
    try:
        os.killpg(handle.pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError("process group is still alive")
