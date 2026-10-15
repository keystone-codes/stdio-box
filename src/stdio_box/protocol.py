"""Messages on the wrapper–spawner socketpair.

Linux uses ``AF_UNIX`` ``SOCK_SEQPACKET``: one ``send`` is one JSON object, and
file descriptors ride along with ``SCM_RIGHTS``. macOS has no sequenced-packet
unix sockets, so local tests use ``SOCK_STREAM`` with a 4-byte length prefix.
The container takes the sequenced-packet path.
"""

import socket
import struct

MAX_MESSAGE = 64 * 1024
_HEADER = struct.Struct(">I")


def control_socketpair() -> tuple[socket.socket, socket.socket]:
    try:
        return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


def is_seqpacket(sock: socket.socket) -> bool:
    return sock.type == socket.SOCK_SEQPACKET


def send_message(sock: socket.socket, payload: bytes, fds: list[int] | None = None) -> None:
    """Send one protocol message. ``fds`` is empty for every message except spawn."""
    files = list(fds or [])
    if is_seqpacket(sock):
        if files:
            socket.send_fds(sock, [payload], files)
        else:
            sock.sendall(payload)
        return
    frame = _HEADER.pack(len(payload)) + payload
    if files:
        socket.send_fds(sock, [frame], files)
    else:
        sock.sendall(frame)


def recv_message(sock: socket.socket, maxfds: int = 3) -> tuple[bytes, list[int], bool]:
    """Return ``(payload, fds, too_long)``.

    An empty payload with ``too_long`` false means the peer closed the socket.
    """
    if is_seqpacket(sock):
        data, fds, flags, _addr = socket.recv_fds(sock, MAX_MESSAGE + 1, maxfds)
        truncated = bool(flags & getattr(socket, "MSG_TRUNC", 0)) or len(data) > MAX_MESSAGE
        if truncated:
            return b"", list(fds), True
        return data, list(fds), False
    return _recv_stream(sock, maxfds)


def _recv_stream(sock: socket.socket, maxfds: int) -> tuple[bytes, list[int], bool]:
    first, fds, _flags, _addr = socket.recv_fds(sock, MAX_MESSAGE + 1 + _HEADER.size, maxfds)
    if not first:
        return b"", list(fds), False
    buf = bytearray(first)
    while len(buf) < _HEADER.size:
        more = sock.recv(_HEADER.size - len(buf))
        if not more:
            return b"", list(fds), False
        buf += more
    length = _HEADER.unpack_from(buf)[0]
    payload = bytes(buf[_HEADER.size :])
    if length > MAX_MESSAGE:
        _drain(sock, length - len(payload))
        return b"", list(fds), True
    while len(payload) < length:
        more = sock.recv(length - len(payload))
        if not more:
            break
        payload += more
    return payload, list(fds), False


def _drain(sock: socket.socket, remaining: int) -> None:
    left = remaining
    if left > 1024 * 1024:
        left = 1024 * 1024
    while left > 0:
        chunk = sock.recv(min(left, 65536))
        if not chunk:
            return
        left -= len(chunk)
