"""Numeric users the image creates and the slot range children use."""

import errno
import os
import signal

WRAPPER_UID = 10001
WRAPPER_GID = 10001
INSTALLER_UID = 10002
INSTALLER_GID = 10002
SLOT_UID_BASE = 20000
MAX_SLOTS = 32
NPROC_LIMIT = 512

# The installer uid is never a child and slot reap does not touch it.
RESERVED_UIDS = frozenset({WRAPPER_UID, INSTALLER_UID})


def slot_uid(slot: int) -> int:
    return SLOT_UID_BASE + slot


def slot_cwd(slot: int, root: str = "/tmp/box") -> str:
    return f"{root}/slot-{slot}"


def can_setuid(uid: int) -> bool:
    """Return whether this process can drop to ``uid``.

    The provider install step uses this to decide whether the installer runs
    as uid 10002. A false result means the child and the installer stay on the
    current uid (``shared_uid``).
    """
    if os.geteuid() != 0:
        return False
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCHLD})
    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
            except OSError:
                os._exit(1)
            os._exit(0 if os.geteuid() == uid else 1)
        _pid, status = os.waitpid(pid, 0)
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def is_eperm(exc: OSError) -> bool:
    return exc.errno == errno.EPERM
