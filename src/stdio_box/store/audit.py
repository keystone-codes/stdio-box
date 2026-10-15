"""Audit rows and the log line that replaces `sub` with `sub_hash`."""

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from stdio_box.store.crypto import sub_hash

logger = logging.getLogger("stdio_box.audit")


def record(
    conn: sqlite3.Connection,
    *,
    audit_key: bytes,
    field_names: set[str],
    now: str,
    event: str,
    sub: str | None = None,
    jti: str | None = None,
    tool: str | None = None,
    profile_id: str | None = None,
    tier: str | None = None,
    outcome: str,
    code: str | None = None,
    duration_ms: int | None = None,
    detail: dict | None = None,
) -> str | None:
    """Insert one audit row and emit the same record on the log.

    The log carries `sub_hash` (first 16 hex characters of HMAC-SHA256 over
    the audit subkey) and never the raw `sub`. `detail` must not use a
    provider field name as a key. Returns the hash, or None when `sub` is absent.
    """
    hashed = None if sub is None else sub_hash(audit_key, sub)
    _reject_detail(detail, field_names)
    conn.execute(
        "INSERT INTO audit (at, event, sub, jti, tool, profile_id, tier, outcome, code, "
        "duration_ms, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            event,
            sub,
            jti,
            tool,
            profile_id,
            tier,
            outcome,
            code,
            duration_ms,
            None if detail is None else json.dumps(detail, sort_keys=True, separators=(",", ":")),
        ),
    )
    payload = {
        "at": now,
        "event": event,
        "sub_hash": hashed,
        "jti": jti,
        "tool": tool,
        "profile_id": profile_id,
        "tier": tier,
        "outcome": outcome,
        "code": code,
        "duration_ms": duration_ms,
        "detail": detail,
    }
    logger.info("%s", json.dumps(payload, sort_keys=True, default=str))
    return hashed


def purge(conn: sqlite3.Connection, *, now: datetime, retention_days: int) -> int:
    """Delete audit rows older than `retention_days`. Return how many were deleted."""
    cutoff = now - timedelta(days=retention_days)
    if cutoff.tzinfo is None:
        stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        from datetime import timezone

        stamp = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = conn.execute("DELETE FROM audit WHERE at < ?", (stamp,))
    return cursor.rowcount if cursor.rowcount is not None else 0


def list_events(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    profile_id: str | None = None,
    event: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[str] = []
    if since is not None:
        clauses.append("at >= ?")
        params.append(since)
    if profile_id is not None:
        clauses.append("profile_id = ?")
        params.append(profile_id)
    if event is not None:
        clauses.append("event = ?")
        params.append(event)
    sql = (
        "SELECT id, at, event, sub, jti, tool, profile_id, tier, outcome, code, "
        "duration_ms, detail FROM audit"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    rows = []
    for row in conn.execute(sql, params):
        detail = None if row["detail"] is None else json.loads(row["detail"])
        rows.append(
            {
                "id": row["id"],
                "at": row["at"],
                "event": row["event"],
                "sub": row["sub"],
                "jti": row["jti"],
                "tool": row["tool"],
                "profile_id": row["profile_id"],
                "tier": row["tier"],
                "outcome": row["outcome"],
                "code": row["code"],
                "duration_ms": row["duration_ms"],
                "detail": detail,
            }
        )
    return rows


def _reject_detail(detail: dict | None, field_names: set[str]) -> None:
    if detail is None:
        return
    if not isinstance(detail, dict):
        raise ValueError("detail must be an object")
    forbidden = sorted(set(detail) & field_names)
    if forbidden:
        raise ValueError("detail contains a provider field name: " + ", ".join(forbidden))
