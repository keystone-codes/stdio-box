"""Signed caller identity for one tool call, including a one-use ``jti`` cache."""

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import NoReturn

import jwt

from stdio_box._vendor.keystone_identity import (
    IdentityError,
    KeystoneIdentity,
    verify_keystone_identity,
)
from stdio_box.config import Config
from stdio_box.refusals import IDENTITY_REJECTED, Refusal

_LOG = logging.getLogger("stdio_box.identity")
_REPLAY_GRACE_SECONDS = 30
_DEFAULT_REPLAY_LIMIT = 100_000


class CallRejected(Exception):
    """The tool call stops here. ``refusal`` is the client-facing result."""

    def __init__(self, refusal: Refusal) -> None:
        self.refusal = refusal
        super().__init__(refusal.message)


class ReplayCache:
    """Remember each ``jti`` until its ``exp`` plus 30 seconds.

    When the cache is full, older entries stay. The new call is rejected.
    Evicting the oldest entry would let a caller replay it.
    """

    def __init__(
        self,
        *,
        limit: int = _DEFAULT_REPLAY_LIMIT,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError("replay cache limit must be at least 1")
        self._limit = limit
        self._clock = time.time if clock is None else clock
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def remember(self, jti: str, exp: int) -> str:
        """Return ``ok``, ``replay``, or ``full``. Only ``ok`` stores ``jti``."""
        now = self._clock()
        deadline = float(exp) + _REPLAY_GRACE_SECONDS
        with self._lock:
            self._drop_expired(now)
            until = self._until.get(jti)
            if until is not None and until > now:
                return "replay"
            if len(self._until) >= self._limit:
                return "full"
            self._until[jti] = deadline
            return "ok"

    def _drop_expired(self, now: float) -> None:
        expired = [key for key, until in self._until.items() if until <= now]
        for key in expired:
            del self._until[key]


def verify_call(
    headers: Mapping[str, str],
    tool_name: str,
    config: Config,
    replay_cache: ReplayCache,
) -> KeystoneIdentity:
    """Verify ``X-Keystone-Identity`` for this tool name. Raise :class:`CallRejected` otherwise."""
    token = headers.get("x-keystone-identity")
    if isinstance(token, str):
        token = token.strip()
    try:
        identity = verify_keystone_identity(
            token,
            secrets=[config.identity_secret],
            audience=config.identity_audience,
        )
    except IdentityError as exc:
        _reject(_safe_reason(str(exc), token))
    if identity.mcp_target != tool_name:
        _reject("mcp_target does not match this tool")
    if not identity.jti:
        _reject("missing jti")
    exp = _expiry(token, config)
    outcome = replay_cache.remember(identity.jti, exp)
    if outcome == "replay":
        _reject("assertion was already used")
    if outcome == "full":
        _LOG.warning("replay_cache_full")
        _reject("replay cache is full")
    return identity


def _expiry(token: str | None, config: Config) -> int:
    # The signature was checked above. Read exp from that same token.
    assert token is not None
    try:
        claims = jwt.decode(
            token,
            config.identity_secret,
            algorithms=["HS256"],
            audience=config.identity_audience,
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError as exc:
        _reject(_safe_reason(str(exc), token))
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int):
        _reject("missing exp")
    return exp


def _safe_reason(reason: str, token: str | None) -> str:
    if token and token in reason:
        return reason.replace(token, "[redacted]")
    return reason


def _reject(reason: str) -> NoReturn:
    raise CallRejected(
        Refusal(
            IDENTITY_REJECTED,
            f"identity rejected: {reason}",
            "Send a fresh signed identity for this tool.",
        )
    )
