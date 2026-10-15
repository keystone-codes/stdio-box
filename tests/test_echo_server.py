"""The echo stdio server answers who, writes the token to stderr, and can exit."""

import hashlib
import secrets
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ECHO_SERVER = Path(__file__).resolve().parent / "support" / "echo_server.py"


async def test_who_crash_and_spam(tmp_path: Path) -> None:
    token = secrets.token_hex(16)
    url = "https://echo.invalid"
    expected = hashlib.sha256(token.encode()).hexdigest()[:8]
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ECHO_SERVER)],
        env={"ECHO_TOKEN": token, "ECHO_URL": url},
    )
    err_path = tmp_path / "stderr.txt"

    with err_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                who = await session.call_tool("who", {})
                assert who.isError is False
                payload = who.structuredContent
                assert payload is not None
                assert payload["token_sha8"] == expected
                assert payload["url"] == url
                assert payload["pid"] > 0
                assert payload["uid"] >= 0
                assert "ECHO_TOKEN" in payload["env_keys"]
                meta = await session.call_tool("meta", {}, meta={"marker": "s01"})
                assert meta.structuredContent is not None
                assert meta.structuredContent.get("marker") == "s01"
                spam = await session.call_tool("spam", {"lines": 3})
                assert spam.isError is False
    stderr_lines = err_path.read_text(encoding="utf-8").splitlines()
    assert stderr_lines.count(token) == 1
    assert len(stderr_lines) == 3

    crash_err = tmp_path / "crash.txt"
    with crash_err.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                with pytest.raises(BaseException) as exc:
                    await session.call_tool(
                        "crash",
                        {},
                        read_timeout_seconds=timedelta(seconds=5),
                    )
    assert not isinstance(exc.value, KeyboardInterrupt)
