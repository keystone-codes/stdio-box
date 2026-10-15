"""Rate-limit and mask a child's stderr before it reaches the wrapper log."""

import logging
import os
import re
import select
import threading
import time
from collections.abc import Iterable

LINE_MAX_CHARS = 2000
_READ_CAP = 8192
RATE_PER_SECOND = 50
BURST = 200

# Well-known token shapes. Ordinary words such as sk-learn and secret_santa
# stay because the tail after the prefix is shorter than the minimum.
_TOKEN = re.compile(
    r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|xox[abprs]-|sk-(?:proj-|ant-)?)"
    r"[A-Za-z0-9_\-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ATATT[A-Za-z0-9_\-=]{20,}"
    r"|ntn_[A-Za-z0-9]{20,}"
    r"|secret_[A-Za-z0-9]{20,}"
    r"|y0_[A-Za-z0-9_\-]{20,}"
    r"|tvly-[A-Za-z0-9]{20,}"
)

_LOG = logging.getLogger("stdio_box.child.stderr")


class SuppressedTotal:
    """Process-wide count of stderr lines dropped by the rate limit."""

    def __init__(self) -> None:
        self.value = 0

    def add(self, count: int) -> None:
        self.value += count


stderr_suppressed_total = SuppressedTotal()


class TokenBucket:
    def __init__(self, rate: int = RATE_PER_SECOND, burst: int = BURST) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = burst * 1000
        self._updated = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        added = int((now - self._updated) * self.rate * 1000)
        if added > 0:
            self._updated = now
            self._tokens = min(self.burst * 1000, self._tokens + added)
        if self._tokens >= 1000:
            self._tokens -= 1000
            return True
        return False


def mask_line(text: str, mask_values: Iterable[str]) -> str:
    """Replace profile field values and known token shapes with ``***``."""
    values = sorted({item for item in mask_values if len(item) >= 6}, key=len, reverse=True)
    for value in values:
        text = text.replace(value, "***")
    return _TOKEN.sub("***", text)


class StderrPump:
    """Read a stderr pipe, mask each line, and log it under a token bucket.

    Fifty lines per second are kept, with a burst of 200. Further lines are
    counted and, once a second, reported as ``suppressed N stderr line(s)``.
    """

    def __init__(
        self,
        fd: int,
        profile_id: str,
        generation: int,
        mask_values: Iterable[str],
        slot: int,
    ) -> None:
        self._fd = fd
        self.profile_id = profile_id
        self.generation = generation
        self.slot = slot
        self._mask_values = tuple(mask_values)
        self._bucket = TokenBucket()
        self._pending = 0
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="stdio-box-stderr", daemon=True)
        self._thread.start()

    def close(self) -> None:
        fd = self._fd
        self._fd = -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        self.wait()
        self._closed.set()

    def wait(self, timeout: float = 2) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        buf = bytearray()
        next_report = time.monotonic() + 1
        while self._fd >= 0:
            now = time.monotonic()
            if now >= next_report:
                self._flush_suppressed()
                next_report = now + 1
            try:
                readable, _w, _x = select.select([self._fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                chunk = os.read(self._fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                newline = buf.find(b"\n")
                if newline < 0:
                    if len(buf) >= _READ_CAP:
                        self._emit(bytes(buf[:_READ_CAP]))
                        del buf[:_READ_CAP]
                    break
                raw = bytes(buf[:newline])
                del buf[: newline + 1]
                self._emit(raw)
        if buf:
            self._emit(bytes(buf))
        self._flush_suppressed()

    def _emit(self, raw: bytes) -> None:
        if not self._bucket.take():
            self._pending += 1
            return
        text = raw.decode("utf-8", errors="replace")
        masked = mask_line(text, self._mask_values)
        if len(masked) > LINE_MAX_CHARS:
            masked = masked[:LINE_MAX_CHARS]
        _LOG.info(
            "%s",
            masked,
            extra={
                "profile": self.profile_id,
                "generation": self.generation,
                "slot": self.slot,
            },
        )

    def _flush_suppressed(self) -> None:
        pending = self._pending
        if pending == 0:
            return
        self._pending = 0
        stderr_suppressed_total.add(pending)
        _LOG.warning(
            "suppressed %d stderr line(s)",
            pending,
            extra={
                "profile": self.profile_id,
                "generation": self.generation,
                "slot": self.slot,
            },
        )
