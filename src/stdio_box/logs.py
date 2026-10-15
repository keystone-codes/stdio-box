"""JSON logs on stdout. Tokens in ``/creds/`` paths are redacted."""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_RESERVED = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
_QUIET_LOGGERS = ("uvicorn.access", "httpx", "httpcore")
_ACCESS = logging.getLogger("stdio_box.access")
_LINK_SEGMENTS = frozenset({"creds"})


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, event, msg, plus extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_") or key in payload:
                continue
            payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Send JSON logs to stdout. The access logger of the HTTP server stays quiet."""
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def redact_path(path: str) -> str:
    """Hide the segment after ``/creds``.

    ``/creds/<token>`` becomes ``/creds/***``. The prefix match is
    case-insensitive. ``.``, ``..``, repeated slashes, and percent-encoded
    slashes are collapsed first, which keeps ``/./creds/…``, ``//creds/…``,
    and ``/x/../creds/…`` redacted. If a link segment appeared anywhere, the
    original string is not returned.
    """
    decoded = _percent_decode(path)
    if not decoded.startswith("/"):
        decoded = "/" + decoded
    parts, saw_link = _collapse_hiding_tokens(decoded)
    normalized = "/" + "/".join(parts)
    lowered = normalized.lower()
    prefix = "/creds"
    if lowered == prefix or lowered.startswith(prefix + "/"):
        return f"{prefix}/***"
    if saw_link:
        return normalized
    return path


def _percent_decode(path: str) -> str:
    decoded = path
    for _ in range(3):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    return decoded


def _collapse_hiding_tokens(decoded: str) -> tuple[list[str], bool]:
    """Collapse dot segments and replace the segment that follows ``creds``.

    The token is removed before ``..`` can carry the original string out of the
    prefix. A later copy of that same segment is removed as well.
    """
    parts: list[str] = []
    secrets: set[str] = set()
    saw_link = False
    hide_next = False
    for part in decoded.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        if part.lower() in _LINK_SEGMENTS:
            saw_link = True
            hide_next = True
            parts.append(part.lower())
            continue
        if hide_next:
            hide_next = False
            secrets.add(part)
            parts.append("***")
            continue
        parts.append(part)
    if secrets:
        parts = ["***" if part in secrets else part for part in parts]
    return parts, saw_link


class RequestLogMiddleware:
    """Log one JSON line per HTTP response, including 401 from the bearer check."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            _ACCESS.info(
                "request",
                extra={
                    "event": "http_request",
                    "method": scope.get("method", ""),
                    "path": redact_path(scope.get("path", "")),
                    "status": status,
                    "duration_ms": duration_ms,
                },
            )
