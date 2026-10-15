"""Profile, binding, and form-link repository.

One process holds one connection and a lock. Async callers should use
`anyio.to_thread.run_sync`; this object does not await.

A profile whose secret row was sealed with another key is returned with
`state` `needs_reentry`. That value is computed and is not written back.
`lookup` does not consult profile state.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Collection
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stdio_box.store import audit
from stdio_box.store.crypto import (
    INFO_AUDIT_SUB,
    INFO_PROFILE_SECRETS,
    derive,
    key_check,
    key_id,
    open_,
    seal,
)
from stdio_box.store.crypto import (
    sub_hash as hash_subject,
)
from stdio_box.store.db import DB_NAME, connect, enable_wal, secure_files
from stdio_box.store.errors import (
    BindingExists,
    BindingNotFound,
    LinkNotFound,
    ProfileExists,
    ProfileInUse,
    ProfileNotFound,
    SecretUnreadable,
)
from stdio_box.store.migrate import migrate

_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_ROLE_SUBJECT = re.compile(r"^role:[A-Za-z0-9_.-]{1,64}$")
_DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class Profile:
    id: str
    provider: str
    name: str
    variant: str
    description: str | None
    state: str
    public_fields: dict
    version: int
    created_at: str
    created_by: str
    rotated_at: str | None
    rotated_by: str | None
    last_check_at: str | None
    last_check_ok: bool | None
    last_check_identity: str | None


@dataclass(frozen=True)
class Binding:
    provider: str
    subject: str
    profile_id: str
    position: int | None
    created_at: str
    created_by: str


@dataclass(frozen=True)
class Bindings:
    user: list[Binding]
    role: list[Binding]
    default: Binding | None


@dataclass(frozen=True)
class BindResult:
    replaced: bool
    position: int | None


@dataclass(frozen=True)
class Match:
    profile: Profile
    tier: str
    binding: Binding
    not_used: list[Binding]


@dataclass(frozen=True)
class FormLink:
    profile_id: str
    purpose: str
    pending_public_fields: dict | None
    expires_at: str
    consumed_at: str | None
    attempts: int
    created_by: str
    created_at: str


class Store:
    """The on-volume store. `key_state` is `ok` or `key_mismatch`."""

    def __init__(
        self,
        data_dir: str | Path,
        store_key: str,
        *,
        field_names: Collection[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self.path = self._data_dir / DB_NAME
        self._store_key = store_key
        self._field_names = set(field_names)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.key_state = "ok"
        self._conn = connect(self.path)
        try:
            migrate(self._conn, self._data_dir, self._clock())
        except Exception:
            self._conn.close()
            raise
        enable_wal(self._conn, self.path)
        self._salt = b""
        self._key_id = ""
        self._secrets_key = b""
        self._audit_key = b""
        self._ensure_key()

    @classmethod
    def open(
        cls,
        data_dir: str | Path,
        store_key: str,
        *,
        field_names: Collection[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> "Store":
        return cls(data_dir, store_key, field_names=field_names, clock=clock)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def revision(self) -> int:
        with self._lock:
            return self._revision()

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row["value"])

    def create_profile(
        self,
        profile_id: str,
        provider: str,
        variant: str,
        public_fields: dict,
        description: str | None,
        by: str,
    ) -> Profile:
        name = _split_id(profile_id, provider)
        with self._transaction():
            if self._profile_row(profile_id) is not None:
                raise ProfileExists(profile_id)
            now = self._now()
            self._conn.execute(
                "INSERT INTO profiles (id, provider, name, variant, description, state, "
                "public_fields, version, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, 'awaiting_secrets', ?, 0, ?, ?)",
                (
                    profile_id,
                    provider,
                    name,
                    variant,
                    description,
                    json.dumps(public_fields, sort_keys=True, separators=(",", ":")),
                    now,
                    by,
                ),
            )
        profile = self.get_profile(profile_id)
        if profile is None:
            raise ProfileNotFound(profile_id)
        return profile

    def get_profile(self, profile_id: str) -> Profile | None:
        with self._lock:
            return self._present(profile_id)

    def list_profiles(self, provider: str | None = None) -> list[Profile]:
        with self._lock:
            if provider is None:
                rows = self._conn.execute("SELECT id FROM profiles ORDER BY id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id FROM profiles WHERE provider = ? ORDER BY id", (provider,)
                ).fetchall()
            return [profile for row in rows if (profile := self._present(row["id"])) is not None]

    def put_secrets(
        self,
        profile_id: str,
        values: dict,
        by: str,
        *,
        version: int,
        public_fields: dict | None = None,
        variant: str | None = None,
    ) -> None:
        """Seal `values` at `version` and, when that version is the next one, switch to it.

        In `key_mismatch`, the first successful write stores a new key check and
        the store returns to `ok`. Other rows stay sealed with the previous key.
        """
        flipped = False
        with self._transaction():
            flipped = self._put_secrets(
                profile_id,
                values,
                by,
                version=version,
                public_fields=public_fields,
                variant=variant,
            )
        if flipped:
            self.key_state = "ok"

    def rotate_profile(
        self,
        profile_id: str,
        *,
        variant: str,
        public_fields: dict,
        secrets: dict,
        by: str,
        description: str | None = None,
    ) -> None:
        """Replace public fields and secrets of an existing profile in one transaction."""
        flipped = False
        with self._transaction():
            row = self._profile_row(profile_id)
            if row is None:
                raise ProfileNotFound(profile_id)
            assignments = ["variant = ?", "public_fields = ?"]
            params: list = [
                variant,
                json.dumps(public_fields, sort_keys=True, separators=(",", ":")),
            ]
            if description is not None:
                assignments.append("description = ?")
                params.append(description)
            params.append(profile_id)
            self._conn.execute(f"UPDATE profiles SET {', '.join(assignments)} WHERE id = ?", params)
            flipped = self._put_secrets(
                profile_id,
                secrets,
                by,
                version=int(row["version"]) + 1,
            )
        if flipped:
            self.key_state = "ok"

    def read_secrets(self, profile_id: str) -> dict:
        with self._lock:
            row = self._profile_row(profile_id)
            if row is None:
                raise ProfileNotFound(profile_id)
            secret = self._secret_row(profile_id, int(row["version"]))
            if secret is None or secret["key_id"] != self._key_id:
                raise SecretUnreadable
            return open_(
                self._secrets_key,
                profile_id,
                int(secret["version"]),
                secret["nonce"],
                secret["ciphertext"],
            )

    def delete_profile(self, profile_id: str) -> None:
        with self._transaction():
            row = self._profile_row(profile_id)
            if row is None:
                raise ProfileNotFound(profile_id)
            subjects = [
                binding["subject"]
                for binding in self._conn.execute(
                    "SELECT subject FROM bindings WHERE profile_id = ? ORDER BY subject",
                    (profile_id,),
                )
            ]
            if subjects:
                raise ProfileInUse(subjects)
            self._conn.execute("DELETE FROM form_links WHERE profile_id = ?", (profile_id,))
            self._conn.execute("DELETE FROM profile_secrets WHERE profile_id = ?", (profile_id,))
            self._conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

    def set_check(
        self,
        profile_id: str,
        ok: bool | None,
        identity: str | None,
        at: datetime,
    ) -> None:
        with self._transaction():
            row = self._profile_row(profile_id)
            if row is None:
                raise ProfileNotFound(profile_id)
            stored: int | None
            if ok is None:
                stored = None
                state_sql = ""
                params: list = [self._iso(at), stored, identity, profile_id]
            else:
                stored = 1 if ok else 0
                state_sql = ", state = ?"
                params = [
                    self._iso(at),
                    stored,
                    identity,
                    "ready" if ok else "check_failed",
                    profile_id,
                ]
            self._conn.execute(
                "UPDATE profiles SET last_check_at = ?, last_check_ok = ?, "
                f"last_check_identity = ?{state_sql} WHERE id = ?",
                params,
            )

    def bind(
        self,
        provider: str,
        subject: str,
        profile_id: str,
        by: str,
        *,
        replace: bool = False,
        position: int | None = None,
    ) -> BindResult:
        normalized = normalize_subject(subject)
        if position is not None and (normalized == "default" or normalized.startswith("user:")):
            raise ValueError("position is only valid for a role binding")
        if position is not None and position < 1:
            raise ValueError("position must be 1 or greater")
        with self._transaction():
            profile = self._profile_row(profile_id)
            if profile is None:
                raise ProfileNotFound(profile_id)
            if profile["provider"] != provider:
                raise ValueError("profile belongs to another provider")
            now = self._now()
            if not normalized.startswith("role:"):
                return self._bind_singleton(provider, normalized, profile_id, by, now, replace)
            return self._bind_role(
                provider, normalized, profile_id, by, now, replace=replace, position=position
            )

    def unbind(self, provider: str, subject: str) -> str:
        """Remove the binding and close the gap in role positions. Return its profile id."""
        normalized = normalize_subject(subject)
        with self._transaction():
            row = self._conn.execute(
                "SELECT profile_id, position FROM bindings WHERE provider = ? AND subject = ?",
                (provider, normalized),
            ).fetchone()
            if row is None:
                raise BindingNotFound(normalized)
            profile_id = row["profile_id"]
            if normalized.startswith("role:"):
                remaining = [
                    (item.subject, item.profile_id)
                    for item in self._role_bindings(provider)
                    if item.subject != normalized
                ]
                self._rewrite_roles(provider, remaining, by="", now=self._now())
            else:
                self._conn.execute(
                    "DELETE FROM bindings WHERE provider = ? AND subject = ?",
                    (provider, normalized),
                )
            return profile_id

    def bindings(self, provider: str) -> Bindings:
        with self._lock:
            users = [
                self._binding_from_row(row)
                for row in self._conn.execute(
                    "SELECT * FROM bindings WHERE provider = ? AND subject LIKE 'user:%' "
                    "ORDER BY subject",
                    (provider,),
                )
            ]
            default_row = self._conn.execute(
                "SELECT * FROM bindings WHERE provider = ? AND subject = 'default'",
                (provider,),
            ).fetchone()
            return Bindings(
                user=users,
                role=self._role_bindings(provider),
                default=None if default_row is None else self._binding_from_row(default_row),
            )

    def bindings_for(self, profile_id: str) -> list[Binding]:
        with self._lock:
            return [
                self._binding_from_row(row)
                for row in self._conn.execute(
                    "SELECT * FROM bindings WHERE profile_id = ? ORDER BY subject",
                    (profile_id,),
                )
            ]

    def lookup(self, provider: str, sub: str, roles: Collection[str]) -> Match | None:
        """First matching binding: `user:<sub>`, then the earliest matching role, then default."""
        role_names = {_role_name(role) for role in roles}
        user_subject = "user:" + sub.lower()
        with self._lock:
            user_row = self._conn.execute(
                "SELECT * FROM bindings WHERE provider = ? AND subject = ?",
                (provider, user_subject),
            ).fetchone()
            if user_row is not None:
                binding = self._binding_from_row(user_row)
                profile = self._present(binding.profile_id)
                if profile is None:
                    return None
                return Match(profile, "user", binding, [])
            matched: Binding | None = None
            not_used: list[Binding] = []
            for binding in self._role_bindings(provider):
                if _role_name(binding.subject) not in role_names:
                    continue
                if matched is None:
                    matched = binding
                else:
                    not_used.append(binding)
            if matched is not None:
                profile = self._present(matched.profile_id)
                if profile is None:
                    return None
                return Match(profile, "role", matched, not_used)
            default_row = self._conn.execute(
                "SELECT * FROM bindings WHERE provider = ? AND subject = 'default'",
                (provider,),
            ).fetchone()
            if default_row is None:
                return None
            binding = self._binding_from_row(default_row)
            profile = self._present(binding.profile_id)
            if profile is None:
                return None
            return Match(profile, "default", binding, [])

    def create_link(
        self,
        profile_id: str,
        purpose: str,
        ttl: int,
        by: str,
        pending_public_fields: dict | None = None,
    ) -> str:
        """Return a new token. Only its SHA-256 is stored. Older live links are extinguished."""
        if purpose not in {"create", "rotate"}:
            raise ValueError("invalid link purpose")
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        with self._transaction():
            if self._profile_row(profile_id) is None:
                raise ProfileNotFound(profile_id)
            now_dt = self._clock()
            now = self._iso(now_dt)
            self._conn.execute(
                "UPDATE form_links SET consumed_at = ? "
                "WHERE profile_id = ? AND consumed_at IS NULL",
                (now, profile_id),
            )
            token = secrets.token_urlsafe(32)
            expires = self._iso(now_dt + timedelta(seconds=ttl))
            pending = (
                None
                if pending_public_fields is None
                else json.dumps(pending_public_fields, sort_keys=True, separators=(",", ":"))
            )
            self._conn.execute(
                "INSERT INTO form_links (token_hash, profile_id, purpose, pending_public_fields, "
                "expires_at, consumed_at, attempts, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?)",
                (_token_hash(token), profile_id, purpose, pending, expires, by, now),
            )
            return token

    def peek_link(self, token: str) -> FormLink | None:
        """Return a live link. A preview does not extinguish it."""
        with self._lock:
            return self._live_link(token)

    def consume_link(self, token: str) -> FormLink:
        with self._transaction():
            link = self._live_link(token)
            if link is None:
                raise LinkNotFound
            now = self._now()
            self._conn.execute(
                "UPDATE form_links SET consumed_at = ? WHERE token_hash = ?",
                (now, _token_hash(token)),
            )
            return FormLink(
                profile_id=link.profile_id,
                purpose=link.purpose,
                pending_public_fields=link.pending_public_fields,
                expires_at=link.expires_at,
                consumed_at=now,
                attempts=link.attempts,
                created_by=link.created_by,
                created_at=link.created_at,
            )

    def fail_link(self, token: str) -> int:
        """Count a failed attempt. The fifth failure extinguishes the link."""
        with self._transaction():
            link = self._live_link(token)
            if link is None:
                raise LinkNotFound
            attempts = link.attempts + 1
            consumed = self._now() if attempts >= 5 else None
            self._conn.execute(
                "UPDATE form_links SET attempts = ?, consumed_at = ? WHERE token_hash = ?",
                (attempts, consumed, _token_hash(token)),
            )
            return attempts

    def record(
        self,
        event: str,
        *,
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
        with self._transaction():
            return audit.record(
                self._conn,
                audit_key=self._audit_key,
                field_names=self._field_names,
                now=self._now(),
                event=event,
                sub=sub,
                jti=jti,
                tool=tool,
                profile_id=profile_id,
                tier=tier,
                outcome=outcome,
                code=code,
                duration_ms=duration_ms,
                detail=detail,
            )

    def purge(self, now: datetime | None = None) -> int:
        moment = self._clock() if now is None else now
        days = _retention_days()
        with self._transaction():
            return audit.purge(self._conn, now=moment, retention_days=days)

    def list_audit(
        self,
        *,
        since: str | None = None,
        profile_id: str | None = None,
        event: str | None = None,
    ) -> list[dict]:
        with self._lock:
            return audit.list_events(self._conn, since=since, profile_id=profile_id, event=event)

    def write_status(self, body: dict) -> None:
        """Replace the status row. This does not bump ``meta.revision``.

        The wrapper writes this snapshot on a timer. A revision bump would look
        like a credentials change to the instance watcher.
        """
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM status")
                self._conn.execute("INSERT INTO status (body) VALUES (?)", (encoded,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            secure_files(self.path)

    def read_status(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM status LIMIT 1").fetchone()
            if row is None:
                return None
            loaded = json.loads(row["body"])
            if isinstance(loaded, dict):
                return loaded
            return {"status": loaded}

    def rekey(self, old_key: str) -> None:
        """Re-seal every row the current key cannot read, using `old_key`."""
        old_secrets = derive(old_key, self._salt, INFO_PROFILE_SECRETS)
        with self._transaction():
            rows = self._conn.execute(
                "SELECT profile_id, version, nonce, ciphertext, key_id FROM profile_secrets"
            ).fetchall()
            for row in rows:
                if row["key_id"] == self._key_id:
                    continue
                values = open_(
                    old_secrets,
                    row["profile_id"],
                    int(row["version"]),
                    row["nonce"],
                    row["ciphertext"],
                )
                nonce, ciphertext = seal(
                    self._secrets_key, row["profile_id"], int(row["version"]), values
                )
                self._conn.execute(
                    "UPDATE profile_secrets SET nonce = ?, ciphertext = ?, key_id = ? "
                    "WHERE profile_id = ? AND version = ?",
                    (nonce, ciphertext, self._key_id, row["profile_id"], int(row["version"])),
                )
            self._write_key_check()
        self.key_state = "ok"

    def sub_hash(self, sub: str) -> str:
        return hash_subject(self._audit_key, sub)

    def _ensure_key(self) -> None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'store_salt'").fetchone()
        if row is None:
            salt = os.urandom(16)
            check = key_check(self._store_key, salt)
            now = self._now()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('store_salt', ?)", (salt.hex(),)
                )
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('key_check', ?)", (check,)
                )
                self._conn.execute("INSERT INTO meta (key, value) VALUES ('revision', '0')")
                self._conn.execute("INSERT INTO meta (key, value) VALUES ('created_at', ?)", (now,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            secure_files(self.path)
            self._install_keys(salt, check)
            self.key_state = "ok"
            return
        salt = bytes.fromhex(row["value"])
        expected = key_check(self._store_key, salt)
        stored = self._conn.execute("SELECT value FROM meta WHERE key = 'key_check'").fetchone()
        actual = "" if stored is None else stored["value"]
        self._install_keys(salt, expected)
        if not hmac.compare_digest(expected, actual):
            self.key_state = "key_mismatch"
        else:
            self.key_state = "ok"

    def _install_keys(self, salt: bytes, check_hex: str) -> None:
        self._salt = salt
        self._key_id = key_id(check_hex)
        self._secrets_key = derive(self._store_key, salt, INFO_PROFILE_SECRETS)
        self._audit_key = derive(self._store_key, salt, INFO_AUDIT_SUB)

    def _write_key_check(self) -> None:
        check = key_check(self._store_key, self._salt)
        self._conn.execute("UPDATE meta SET value = ? WHERE key = 'key_check'", (check,))

    def _put_secrets(
        self,
        profile_id: str,
        values: dict,
        by: str,
        *,
        version: int,
        public_fields: dict | None = None,
        variant: str | None = None,
    ) -> bool:
        row = self._profile_row(profile_id)
        if row is None:
            raise ProfileNotFound(profile_id)
        current = int(row["version"])
        if version != current and version != current + 1:
            raise ValueError("secret version must be the current version or the next one")
        if version < 1:
            raise ValueError("secret version must be 1 or greater")
        now = self._now()
        nonce, ciphertext = seal(self._secrets_key, profile_id, version, values)
        self._conn.execute(
            "INSERT INTO profile_secrets (profile_id, version, nonce, ciphertext, key_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(profile_id, version) DO UPDATE SET nonce = excluded.nonce, "
            "ciphertext = excluded.ciphertext, key_id = excluded.key_id, created_at = excluded.created_at",
            (profile_id, version, nonce, ciphertext, self._key_id, now),
        )
        switched = version != current
        if switched:
            self._conn.execute(
                "UPDATE profiles SET version = ?, state = 'ready', rotated_at = ?, rotated_by = ? "
                "WHERE id = ?",
                (version, now, by, profile_id),
            )
            if current >= 1:
                self._conn.execute(
                    "DELETE FROM profile_secrets WHERE profile_id = ? AND version = ?",
                    (profile_id, current),
                )
        if public_fields is not None or variant is not None:
            assignments = []
            params: list = []
            if public_fields is not None:
                assignments.append("public_fields = ?")
                params.append(json.dumps(public_fields, sort_keys=True, separators=(",", ":")))
            if variant is not None:
                assignments.append("variant = ?")
                params.append(variant)
            params.append(profile_id)
            self._conn.execute(f"UPDATE profiles SET {', '.join(assignments)} WHERE id = ?", params)
        if self.key_state != "key_mismatch":
            return False
        self._write_key_check()
        return True

    def _bind_singleton(
        self,
        provider: str,
        subject: str,
        profile_id: str,
        by: str,
        now: str,
        replace: bool,
    ) -> BindResult:
        row = self._conn.execute(
            "SELECT profile_id FROM bindings WHERE provider = ? AND subject = ?",
            (provider, subject),
        ).fetchone()
        if row is not None and not replace:
            raise BindingExists(row["profile_id"])
        if row is None:
            self._conn.execute(
                "INSERT INTO bindings (provider, subject, profile_id, position, created_at, created_by) "
                "VALUES (?, ?, ?, NULL, ?, ?)",
                (provider, subject, profile_id, now, by),
            )
            return BindResult(False, None)
        self._conn.execute(
            "UPDATE bindings SET profile_id = ? WHERE provider = ? AND subject = ?",
            (profile_id, provider, subject),
        )
        return BindResult(True, None)

    def _bind_role(
        self,
        provider: str,
        subject: str,
        profile_id: str,
        by: str,
        now: str,
        *,
        replace: bool,
        position: int | None,
    ) -> BindResult:
        current = self._role_bindings(provider)
        existed = next((item for item in current if item.subject == subject), None)
        if existed is not None and not replace:
            raise BindingExists(existed.profile_id)
        remaining = [(item.subject, item.profile_id) for item in current if item.subject != subject]
        if position is None:
            if existed is not None:
                place = min(
                    next(index for index, item in enumerate(current) if item.subject == subject),
                    len(remaining),
                )
            else:
                place = len(remaining)
        elif position > len(remaining) + 1:
            place = len(remaining)
        else:
            place = position - 1
        remaining.insert(place, (subject, profile_id))
        self._rewrite_roles(provider, remaining, by=by, now=now)
        return BindResult(existed is not None, place + 1)

    def _rewrite_roles(
        self,
        provider: str,
        ordered: list[tuple[str, str]],
        *,
        by: str,
        now: str,
    ) -> None:
        """Write role positions with no gaps.

        UNIQUE (provider, position) is enforced at the end of each statement,
        so rows move through distinct negative positions first.
        """
        existing = {
            row["subject"]: row
            for row in self._conn.execute(
                "SELECT subject, profile_id, created_at, created_by FROM bindings "
                "WHERE provider = ? AND subject LIKE 'role:%'",
                (provider,),
            )
        }
        for index, subject in enumerate(existing, start=1):
            self._conn.execute(
                "UPDATE bindings SET position = ? WHERE provider = ? AND subject = ?",
                (-index, provider, subject),
            )
        final = {subject for subject, _profile_id in ordered}
        for subject in existing:
            if subject not in final:
                self._conn.execute(
                    "DELETE FROM bindings WHERE provider = ? AND subject = ?",
                    (provider, subject),
                )
        for position, (subject, profile_id) in enumerate(ordered, start=1):
            if subject in existing:
                self._conn.execute(
                    "UPDATE bindings SET profile_id = ?, position = ? "
                    "WHERE provider = ? AND subject = ?",
                    (profile_id, position, provider, subject),
                )
            else:
                self._conn.execute(
                    "INSERT INTO bindings (provider, subject, profile_id, position, created_at, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (provider, subject, profile_id, position, now, by),
                )

    def _role_bindings(self, provider: str) -> list[Binding]:
        return [
            self._binding_from_row(row)
            for row in self._conn.execute(
                "SELECT * FROM bindings WHERE provider = ? AND position IS NOT NULL "
                "ORDER BY position",
                (provider,),
            )
        ]

    def _present(self, profile_id: str) -> Profile | None:
        row = self._profile_row(profile_id)
        if row is None:
            return None
        state = row["state"]
        secret = self._secret_row(profile_id, int(row["version"]))
        if secret is not None and secret["key_id"] != self._key_id:
            state = "needs_reentry"
        ok = row["last_check_ok"]
        return Profile(
            id=row["id"],
            provider=row["provider"],
            name=row["name"],
            variant=row["variant"],
            description=row["description"],
            state=state,
            public_fields=json.loads(row["public_fields"]),
            version=int(row["version"]),
            created_at=row["created_at"],
            created_by=row["created_by"],
            rotated_at=row["rotated_at"],
            rotated_by=row["rotated_by"],
            last_check_at=row["last_check_at"],
            last_check_ok=None if ok is None else bool(ok),
            last_check_identity=row["last_check_identity"],
        )

    def _profile_row(self, profile_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()

    def _secret_row(self, profile_id: str, version: int) -> sqlite3.Row | None:
        if version < 1:
            return None
        return self._conn.execute(
            "SELECT * FROM profile_secrets WHERE profile_id = ? AND version = ?",
            (profile_id, version),
        ).fetchone()

    def _live_link(self, token: str) -> FormLink | None:
        row = self._conn.execute(
            "SELECT * FROM form_links WHERE token_hash = ?", (_token_hash(token),)
        ).fetchone()
        if row is None or row["consumed_at"] is not None:
            return None
        if row["expires_at"] <= self._now():
            return None
        pending = row["pending_public_fields"]
        return FormLink(
            profile_id=row["profile_id"],
            purpose=row["purpose"],
            pending_public_fields=None if pending is None else json.loads(pending),
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
            attempts=int(row["attempts"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def _binding_from_row(self, row: sqlite3.Row) -> Binding:
        position = row["position"]
        return Binding(
            provider=row["provider"],
            subject=row["subject"],
            profile_id=row["profile_id"],
            position=None if position is None else int(position),
            created_at=row["created_at"],
            created_by=row["created_by"],
        )

    def _revision(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
        return 0 if row is None else int(row["value"])

    def _bump_revision(self) -> None:
        self._conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'revision'",
            (str(self._revision() + 1),),
        )

    def _now(self) -> str:
        return self._iso(self._clock())

    @staticmethod
    def _iso(moment: datetime) -> str:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        else:
            moment = moment.astimezone(timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    @contextmanager
    def _transaction(self):
        self._lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._bump_revision()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        finally:
            secure_files(self.path)
            self._lock.release()


def normalize_subject(subject: str) -> str:
    """`user:` is lowercased. `role:` must match the role pattern. `default` is itself."""
    if subject.lower().startswith("user:"):
        rest = subject.split(":", 1)[1].lower()
        if not rest or any(character.isspace() for character in rest):
            raise ValueError("invalid subject")
        return "user:" + rest
    if subject == "default":
        return subject
    if _ROLE_SUBJECT.fullmatch(subject):
        return subject
    raise ValueError("invalid subject")


def _split_id(profile_id: str, provider: str) -> str:
    prefix, separator, name = profile_id.partition("/")
    if separator != "/" or prefix != provider or not _PROFILE_NAME.fullmatch(name):
        raise ValueError("invalid profile id")
    return name


def _role_name(subject: str) -> str:
    if subject.startswith("role:"):
        return subject[5:]
    return subject


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _retention_days() -> int:
    raw = os.environ.get("BOX_AUDIT_RETENTION_DAYS", str(_DEFAULT_RETENTION_DAYS))
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError("BOX_AUDIT_RETENTION_DAYS must be an integer") from exc
    if days < 1:
        raise ValueError("BOX_AUDIT_RETENTION_DAYS must be positive")
    return days
