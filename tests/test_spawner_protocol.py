"""Spawner rejects malformed requests and stays up. No root, no children."""

import json
import os
import threading

from stdio_box.protocol import MAX_MESSAGE, control_socketpair, recv_message, send_message
from stdio_box.spawner import SpawnerServer

_ALLOWED = frozenset({"/bin/echo"})


def test_rejects_bad_requests_and_stays_alive() -> None:
    client, server = control_socketpair()
    spawner = SpawnerServer(server, _ALLOWED)
    thread = threading.Thread(target=spawner.serve_forever, daemon=True)
    thread.start()
    try:
        assert _roundtrip(client, {"op": "nope"}) == {"ok": False, "error": "bad_request"}
        assert _roundtrip(client, b"not-json") == {"ok": False, "error": "bad_request"}
        send_message(client, b"x" * (MAX_MESSAGE + 1))
        assert _receive(client) == {"ok": False, "error": "bad_request"}
        assert _roundtrip(
            client,
            {
                "op": "spawn",
                "slot": 32,
                "argv": ["/bin/echo"],
                "env": {},
                "cwd": "/tmp/box/slot-32",
            },
        ) == {"ok": False, "error": "bad_request"}
        assert _roundtrip(
            client,
            {
                "op": "spawn",
                "slot": -1,
                "argv": ["/bin/echo"],
                "env": {},
                "cwd": "/tmp/box/slot--1",
            },
        ) == {"ok": False, "error": "bad_request"}
        assert _roundtrip(
            client,
            {
                "op": "spawn",
                "slot": 0,
                "argv": ["/not/allowed"],
                "env": {},
                "cwd": "/tmp/box/slot-0",
            },
        ) == {"ok": False, "error": "bad_request"}
        assert _roundtrip(client, {"op": "signal", "pid": 1, "sig": "TERM"}) == {
            "ok": False,
            "error": "unknown_pid",
        }
        assert _roundtrip(client, {"op": "still-here"}) == {"ok": False, "error": "bad_request"}
    finally:
        client.close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def _roundtrip(sock, message: dict | bytes) -> dict:
    payload = message if isinstance(message, bytes) else json.dumps(message).encode()
    send_message(sock, payload)
    return _receive(sock)


def _receive(sock) -> dict:
    payload, fds, too_long = recv_message(sock)
    for fd in fds:
        os.close(fd)
    assert too_long is False
    return json.loads(payload)
