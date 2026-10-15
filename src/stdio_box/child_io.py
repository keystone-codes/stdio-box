"""MCP client session on the three descriptors the spawner already opened."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import anyio.lowlevel
import mcp.types as types
from anyio.streams.file import FileReadStream, FileWriteStream
from anyio.streams.text import TextReceiveStream
from mcp import ClientSession
from mcp.shared.message import SessionMessage

from stdio_box.spawner import ChildHandle

_LOG = logging.getLogger("stdio_box.child")


@asynccontextmanager
async def open_child_session(handle: ChildHandle) -> AsyncIterator[ClientSession]:
    """Speak JSON-RPC to a child over the descriptors ``handle`` already owns.

    The shape matches ``mcp.client.stdio.stdio_client``: newline-delimited
    JSON-RPC read from the child's stdout and written to its stdin. Stderr is
    left for ``StderrPump``. This does not start a process.
    """
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
    if handle.stdin is None:
        raise RuntimeError("stdin already taken")
    stdin_file = os.fdopen(handle.stdin, "wb", buffering=0)
    handle.bind_stdin(stdin_file)
    stdout = FileReadStream(os.fdopen(handle.take_stdout(), "rb", buffering=0))
    stdin = FileWriteStream(stdin_file)

    async def stdout_reader() -> None:
        try:
            async with read_stream_writer:
                buffer = ""
                async for chunk in TextReceiveStream(stdout):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            _LOG.exception("failed to parse JSON-RPC from child")
                            await read_stream_writer.send(exc)
                            continue
                        await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def stdin_writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await stdin.send((payload + "\n").encode())
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(stdout_reader)
        task_group.start_soon(stdin_writer)
        try:
            async with ClientSession(read_stream, write_stream) as session:
                yield session
        finally:
            await _close_quietly(stdin)
            await _close_quietly(stdout)
            await _close_quietly(read_stream)
            await _close_quietly(write_stream)
            await _close_quietly(read_stream_writer)
            await _close_quietly(write_stream_reader)
            task_group.cancel_scope.cancel()


async def _close_quietly(resource: object) -> None:
    close = getattr(resource, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        return
