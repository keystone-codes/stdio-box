"""Child stderr is masked and kept to a burst plus one second of lines."""

import logging
import os
import sys
import time
from pathlib import Path

import pytest

from stdio_box.child_io import open_child_session
from stdio_box.runtime import note_child_isolation
from stdio_box.spawner import InlineSpawner, allowlist_for
from stdio_box.stderr import (
    BURST,
    RATE_PER_SECOND,
    StderrPump,
    mask_line,
    stderr_suppressed_total,
)

ECHO_SERVER = Path(__file__).resolve().parent / "support" / "echo_server.py"
_LOG = "stdio_box.child.stderr"

_TOKENS = (
    "ghp_" + "A" * 20,
    "gho_" + "B" * 20,
    "ghu_" + "C" * 20,
    "ghs_" + "D" * 20,
    "ghr_" + "E" * 20,
    "github_pat_" + "F" * 20,
    "glpat-" + "G" * 20,
    "xoxb-" + "H" * 20,
    "sk-" + "I" * 20,
    "sk-proj-" + "J" * 20,
    "sk-ant-" + "K" * 20,
    "AKIA" + "L" * 16,
    "ATATT" + "M" * 20,
    "ntn_" + "N" * 20,
    "secret_" + "P" * 20,
    "y0_" + "Q" * 20,
    "tvly-" + "R" * 20,
)
_PLAIN = "sk-learn risk-free secret_santa"


def test_mask_replaces_tokens_and_keeps_ordinary_words() -> None:
    secret = "echo-token-value"
    url = "https://echo.invalid"
    for token in _TOKENS:
        masked = mask_line(f"prefix {token} suffix", [])
        assert token not in masked
        assert "***" in masked
    kept = mask_line(_PLAIN, [secret, url])
    assert kept == _PLAIN
    hidden = mask_line(f"{secret} {url} {_PLAIN}", [secret, url])
    assert secret not in hidden
    assert url not in hidden
    assert "***" in hidden
    assert "sk-learn" in hidden
    assert "risk-free" in hidden
    assert "secret_santa" in hidden


async def test_spam_is_rate_limited_and_masked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token = "echo-token-value"
    url = "https://echo.invalid/very-long"
    note_child_isolation("per_slot_uid")
    spawner = InlineSpawner(allowlist_for(sys.executable), slot_root=str(tmp_path))
    handle = spawner.spawn(
        0,
        [sys.executable, str(ECHO_SERVER)],
        {"ECHO_TOKEN": token, "ECHO_URL": url, "PATH": "/usr/bin:/bin"},
    )
    pump = StderrPump(handle.take_stderr(), "echo/local", 3, [token, url], handle.slot)
    before = stderr_suppressed_total.value
    caplog.set_level(logging.INFO, logger=_LOG)
    pump.start()
    try:
        async with open_child_session(handle) as session:
            await session.initialize()
            spam = await session.call_tool("spam", {"lines": 1000})
            assert spam.isError is False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if any("suppressed" in record.getMessage() for record in caplog.records):
                    break
                await _sleep()
    finally:
        pump.close()
        spawner.stop(handle.pid)

    records = [record for record in caplog.records if record.name == _LOG]
    info = [record for record in records if record.levelno == logging.INFO]
    assert info, "expected stderr lines in the log"
    first = min(record.created for record in info)
    in_first_second = [record for record in info if record.created - first <= 1]
    assert len(in_first_second) <= BURST + RATE_PER_SECOND + 10
    assert len(in_first_second) <= 260
    assert any("suppressed" in record.getMessage() for record in records)
    blob = "\n".join(record.getMessage() for record in records)
    assert token not in blob
    assert url not in blob
    assert "***" in blob
    assert stderr_suppressed_total.value > before
    assert any(getattr(record, "profile", None) == "echo/local" for record in info)
    assert any(getattr(record, "slot", None) == 0 for record in info)


def test_pump_masks_token_shapes(caplog: pytest.LogCaptureFixture) -> None:
    read_fd, write_fd = os.pipe()
    pump = StderrPump(read_fd, "echo/local", 1, ["echo-token-value"], 2)
    caplog.set_level(logging.INFO, logger=_LOG)
    pump.start()
    try:
        lines = [f"saw {token}" for token in _TOKENS]
        lines.append(_PLAIN)
        lines.append("echo-token-value and https://echo.invalid")
        os.write(write_fd, ("\n".join(lines) + "\n").encode())
        os.close(write_fd)
        write_fd = -1
        pump.wait()
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        pump.close()
    blob = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == _LOG and record.levelno == logging.INFO
    )
    for token in _TOKENS:
        assert token not in blob
    assert "sk-learn" in blob
    assert "risk-free" in blob
    assert "secret_santa" in blob
    assert "echo-token-value" not in blob
    assert "***" in blob


async def _sleep() -> None:
    import anyio

    await anyio.sleep(0.05)
