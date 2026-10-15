"""Facts the wrapper publishes for the HTTP facade and status.

``isolation`` is ``unknown`` until a child starts, then ``per_slot_uid`` or
``shared_uid``. In-process readers (health and the providers tool) call
``get_isolation``. A separate process in a debug container asks the wrapper
over the debug channel instead.
"""

import logging
import threading

_lock = threading.Lock()
_isolation = "unknown"
_warned = False

_LOG = logging.getLogger("stdio_box.wrapper")


def get_isolation() -> str:
    with _lock:
        return _isolation


def note_child_isolation(value: str) -> None:
    """Remember how children are isolated. Warn once when they share a uid."""
    global _isolation, _warned
    if value not in {"per_slot_uid", "shared_uid"}:
        raise ValueError(value)
    with _lock:
        if value == "shared_uid":
            _isolation = "shared_uid"
            warn = not _warned
            _warned = True
        elif _isolation != "shared_uid":
            _isolation = "per_slot_uid"
            warn = False
        else:
            warn = False
    if warn:
        _LOG.warning("child_isolation_unavailable")
