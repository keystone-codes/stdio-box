#!/opt/stdio-box/.venv/bin/python
"""Stdio MCP server used by the box tests.

Tools stay small and explicit so later cards can drive a child without a network install.
"""

import hashlib
import os
import sys
import time

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("echo")


def _object_schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    schema: dict = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties or {},
    }
    if required:
        schema["required"] = required
    return schema


def _tool(
    name: str, description: str, properties: dict | None = None, required: list[str] | None = None
) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema=_object_schema(properties, required),
    )


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    seconds = {"seconds": {"type": "number", "minimum": 0}}
    return [
        _tool("who", "Report pid, uid, a token hash, the url, and environment names."),
        _tool("slow", "Sleep without blocking the event loop.", seconds, ["seconds"]),
        _tool("block", "Sleep on the process, blocking the event loop.", seconds, ["seconds"]),
        _tool("crash", "Exit the process with status 3."),
        _tool(
            "spam",
            "Write lines to stderr. The first line is ECHO_TOKEN.",
            {"lines": {"type": "integer", "minimum": 0}},
            ["lines"],
        ),
        _tool("meta", "Return the _meta object received with this call."),
        _tool("escape", "Fork a session leader that holds files open under /tmp and /dev/shm."),
        _tool(
            "forkbomb",
            "Fork n sleeping children.",
            {"n": {"type": "integer", "minimum": 0}},
            ["n"],
        ),
        _tool("extra_tool", "Present only on the echo provider's full variant."),
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict) -> dict:
    if name == "who":
        return _who()
    if name == "slow":
        await anyio.sleep(float(arguments["seconds"]))
        return {"slept": arguments["seconds"]}
    if name == "block":
        time.sleep(float(arguments["seconds"]))
        return {"blocked": arguments["seconds"]}
    if name == "crash":
        os._exit(3)
    if name == "spam":
        return _spam(int(arguments["lines"]))
    if name == "meta":
        return _received_meta()
    if name == "escape":
        return {"child_pid": _spawn_escape()}
    if name == "forkbomb":
        return _forkbomb(int(arguments["n"]))
    if name == "extra_tool":
        return {"name": "extra_tool"}
    raise ValueError(f"unknown tool {name}")


def _who() -> dict:
    token = os.environ.get("ECHO_TOKEN", "")
    return {
        "pid": os.getpid(),
        "uid": os.getuid(),
        "token_sha8": hashlib.sha256(token.encode()).hexdigest()[:8],
        "url": os.environ.get("ECHO_URL", ""),
        "env_keys": sorted(os.environ),
    }


def _spam(lines: int) -> dict:
    token = os.environ.get("ECHO_TOKEN", "")
    for index in range(lines):
        sys.stderr.write(token + "\n" if index == 0 else f"spam {index}\n")
    sys.stderr.flush()
    return {"lines": lines}


def _received_meta() -> dict:
    meta = server.request_context.meta
    if meta is None:
        return {}
    return meta.model_dump(exclude_none=True)


def _spawn_escape() -> int:
    """Child calls setsid, holds two files open, and sleeps. Used by slot-reap tests."""
    pid = os.fork()
    if pid != 0:
        return pid
    try:
        os.setsid()
        child = os.getpid()
        for path in (f"/tmp/escape-{child}", f"/dev/shm/escape-{child}"):
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.write(fd, b"escape\n")
        time.sleep(600)
    finally:
        os._exit(0)


def _forkbomb(count: int) -> dict:
    pids: list[int] = []
    errno: int | None = None
    for _ in range(count):
        try:
            pid = os.fork()
        except OSError as exc:
            errno = exc.errno
            break
        if pid == 0:
            try:
                time.sleep(600)
            finally:
                os._exit(0)
        pids.append(pid)
    result: dict = {"forked": len(pids)}
    if errno is not None:
        result["errno"] = errno
    return result


async def _serve() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    anyio.run(_serve)


if __name__ == "__main__":
    main()
