"""Operator CLI for profiles, ordered bindings, and the store key.

Field values are never printed. ``profile import`` runs the variant's HTTP
checks before it writes, unless ``--no-check`` is set. A provider refusal
exits 1 and writes nothing. A check that cannot be reached still writes and
exits 0 with ``check: unreachable``.

``sync`` brings profiles and bindings to the state a YAML file describes. A
profile the provider rejects is skipped and the rest is applied; that run
exits 6.
"""

import asyncio
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from stdio_box.checks import run_checks, variant_title
from stdio_box.provider import Provider, ProviderError, Variant, load_provider
from stdio_box.store.errors import (
    BindingExists,
    BindingNotFound,
    ProfileExists,
    ProfileInUse,
    ProfileNotFound,
    SecretUnreadable,
    StoreSchemaNewer,
)
from stdio_box.store.repo import Store, normalize_subject
from stdio_box.url_policy import url_policy_from_mapping

_USAGE = """usage: credsctl [--json] <command> [<args>]
       credsctl profile list
       credsctl profile delete <id>
       credsctl profile import <id> [--variant <name>] --from-file <path|->
           [--if-absent] [--ignore-unknown] [--no-check] [--description <text>]
       credsctl bind <subject> <profile> [--replace] [--position N]
       credsctl unbind <subject>
       credsctl bindings
       credsctl sync --file <path|-> [--dry-run] [--prune] [--no-check]
       credsctl store status
       credsctl store rekey --old-key-file <path|->
       credsctl audit [--since 24h] [--profile <id>] [--event <name>]
       credsctl snapshot list
       credsctl snapshot export <id>
       credsctl status"""

_ACTOR = "cli:credsctl"
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_SINCE = re.compile(r"^(\d+)([smhd])$")
_WRAPPER_UID = 10001
_SYNC_MARK = "credsctl sync"
_SYNC_USAGE = "usage: credsctl sync --file <path|-> [--dry-run] [--prune] [--no-check]"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Usage(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class _Input(Exception):
    def __init__(self, error_id: str, message: str, result: dict | None = None) -> None:
        self.error_id = error_id
        self.message = message
        self.result = result
        super().__init__(message)


class _Exists(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class _Unavailable(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class _Rejected(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class _Partial(Exception):
    def __init__(self, message: str, result: dict) -> None:
        self.message = message
        self.result = result
        super().__init__(message)


def credsctl_main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode, rest = _extract_json(args)
    if not rest or rest[0] in {"-h", "--help"}:
        _exit_usage(json_mode)
    # Drop before any file or database is opened.
    _drop_root()
    try:
        result = _dispatch(rest)
    except _Usage as exc:
        _exit(json_mode, ok=False, code=2, error=_error("usage", exc.message), result=None)
    except _Rejected as exc:
        _exit(json_mode, ok=False, code=1, error=_error("check_failed", exc.message), result=None)
    except _Input as exc:
        _exit(
            json_mode, ok=False, code=3, error=_error(exc.error_id, exc.message), result=exc.result
        )
    except _Exists as exc:
        _exit(json_mode, ok=False, code=4, error=_error("profile_exists", exc.message), result=None)
    except _Partial as exc:
        _exit(
            json_mode,
            ok=False,
            code=6,
            error=_error("partially_applied", exc.message),
            result=exc.result,
        )
    except StoreSchemaNewer:
        _exit(
            json_mode,
            ok=False,
            code=5,
            error=_error(
                "store_schema_newer",
                "The store was written by a newer version of this program.",
            ),
            result=None,
        )
    except _Unavailable as exc:
        _exit(
            json_mode, ok=False, code=5, error=_error("store_unavailable", exc.message), result=None
        )
    else:
        _exit(json_mode, ok=True, code=0, error=None, result=result)


def _dispatch(args: list[str]) -> dict:
    command = args[0]
    rest = args[1:]
    if command == "profile":
        return _profile(rest)
    if command == "bind":
        return _bind(rest)
    if command == "unbind":
        return _unbind(rest)
    if command == "bindings":
        return _bindings(rest)
    if command == "sync":
        return _sync(rest)
    if command == "store":
        return _store(rest)
    if command == "audit":
        return _audit_command(rest)
    if command == "snapshot":
        return _snapshot(rest)
    if command == "status":
        return _status(rest)
    raise _Usage(_USAGE)


def _profile(args: list[str]) -> dict:
    if not args:
        raise _Usage(_USAGE)
    action = args[0]
    if action == "list":
        if args[1:]:
            raise _Usage(_USAGE)
        store = _open_store()
        try:
            return {"profiles": [_public_profile(profile) for profile in store.list_profiles()]}
        finally:
            store.close()
    if action == "delete":
        if len(args) != 2:
            raise _Usage(_USAGE)
        return _delete_profile(args[1])
    if action == "import":
        return _import_profile(args[1:])
    raise _Usage(_USAGE)


def _import_profile(args: list[str]) -> dict:
    flags, positional = _parse_flags(
        args,
        options={"--variant", "--from-file", "--description"},
        switches={"--if-absent", "--ignore-unknown", "--no-check"},
    )
    if len(positional) != 1 or flags["--from-file"] is None:
        raise _Usage(
            "usage: credsctl profile import <id> --from-file <path|-> "
            "[--variant <name>] [--if-absent] [--ignore-unknown] [--no-check] "
            "[--description <text>]"
        )
    provider = _load_provider()
    profile_id = positional[0]
    _check_profile_id(profile_id, provider.name)
    description = flags["--description"]
    if description is not None and len(description) > 200:
        raise _Input("invalid_argument", "description must be at most 200 characters")
    text = _read_private_file(flags["--from-file"])
    parsed = _parse_env(text)
    fields = {field.name: field for field in provider.fields}
    unknown = sorted(name for name in parsed if name not in fields and parsed[name] != "")
    ignored: list[str] = []
    if unknown and not flags["--ignore-unknown"]:
        raise _Input(
            "unknown_fields",
            "unknown fields: " + ", ".join(unknown),
            {"unknown_fields": unknown},
        )
    if flags["--ignore-unknown"]:
        ignored = unknown
    present = {name for name, value in parsed.items() if name in fields and value != ""}
    variant = _select_variant(provider, present, flags["--variant"])
    public: dict[str, str] = {}
    secret: dict[str, str] = {}
    missing = [name for name in variant.fields if name not in present]
    if missing:
        raise _Input(
            "missing_fields",
            "missing fields: " + ", ".join(missing),
            {"missing_fields": missing},
        )
    extra = sorted(present - set(variant.fields))
    if extra:
        raise _Input(
            "invalid_argument",
            "file has fields that are not part of the variant",
            {"fields": extra},
        )
    for name in variant.fields:
        value = parsed[name]
        field = fields[name]
        if len(value) > field.max_length:
            raise _Input("invalid_argument", f"{name} is longer than {field.max_length}")
        if field.secret:
            secret[name] = value
        else:
            public[name] = value
    store = _open_store(provider)
    try:
        existing = store.get_profile(profile_id)
        if existing is not None and flags["--if-absent"]:
            raise _Exists("profile already exists")
        check_result = None
        if not flags["--no-check"]:
            check_result = asyncio.run(
                run_checks(
                    provider,
                    variant,
                    {**public, **secret},
                    instances=None,
                    config=url_policy_from_mapping(os.environ),
                )
            )
            if check_result.ok is False:
                raise _Rejected(_rejection_message(variant, check_result))
        if existing is None:
            try:
                store.create_profile(
                    profile_id, provider.name, variant.name, public, description, _ACTOR
                )
            except ProfileExists as exc:
                raise _Exists("profile already exists") from exc
            store.put_secrets(profile_id, secret, _ACTOR, version=1)
            action = "created"
        else:
            store.rotate_profile(
                profile_id,
                variant=variant.name,
                public_fields=public,
                secrets=secret,
                by=_ACTOR,
                description=description,
            )
            action = "rotated"
        if check_result is not None:
            store.set_check(
                profile_id,
                check_result.ok,
                check_result.identity,
                datetime.now(timezone.utc),
            )
        check = "skipped" if flags["--no-check"] else _check_label(check_result)
        result = {
            "profile": profile_id,
            "variant": variant.name,
            "action": action,
            "check": check,
            "ignored": ignored,
        }
        store.record(
            "admin",
            sub=_ACTOR,
            outcome="ok",
            profile_id=profile_id,
            detail={
                "action": action,
                "check": check,
                "command": "profile import",
                "ignored": ignored,
                "variant": variant.name,
            },
        )
        return result
    finally:
        store.close()


def _delete_profile(profile_id: str) -> dict:
    provider = _load_provider()
    _check_profile_id(profile_id, provider.name)
    store = _open_store(provider)
    try:
        try:
            store.delete_profile(profile_id)
        except ProfileNotFound as exc:
            raise _Input("profile_not_found", "profile not found") from exc
        except ProfileInUse as exc:
            raise _Input(
                "profile_in_use",
                "profile is bound and cannot be deleted",
                {"subjects": exc.subjects},
            ) from exc
        store.record(
            "admin",
            sub=_ACTOR,
            outcome="ok",
            profile_id=profile_id,
            detail={"command": "profile delete"},
        )
        return {"profile": profile_id, "deleted": True}
    finally:
        store.close()


def _bind(args: list[str]) -> dict:
    flags, positional = _parse_flags(args, options={"--position"}, switches={"--replace"})
    if len(positional) != 2:
        raise _Usage("usage: credsctl bind <subject> <profile> [--replace] [--position N]")
    subject, profile_id = positional
    position = _position(flags["--position"])
    provider = _load_provider()
    _check_profile_id(profile_id, provider.name)
    store = _open_store(provider)
    try:
        try:
            outcome = store.bind(
                provider.name,
                subject,
                profile_id,
                _ACTOR,
                replace=bool(flags["--replace"]),
                position=position,
            )
        except ValueError as exc:
            raise _Input("invalid_argument", str(exc)) from exc
        except BindingExists as exc:
            raise _Input(
                "binding_exists",
                "binding already exists",
                {"current": exc.current},
            ) from exc
        except ProfileNotFound as exc:
            raise _Input("profile_not_found", "profile not found") from exc
        normalized = normalize_subject(subject)
        result = {
            "subject": normalized,
            "profile": profile_id,
            "replaced": outcome.replaced,
            "position": outcome.position,
        }
        store.record(
            "admin",
            sub=_ACTOR,
            outcome="ok",
            profile_id=profile_id,
            detail={
                "command": "bind",
                "position": outcome.position,
                "replaced": outcome.replaced,
                "subject": normalized,
            },
        )
        return result
    finally:
        store.close()


def _unbind(args: list[str]) -> dict:
    if len(args) != 1:
        raise _Usage("usage: credsctl unbind <subject>")
    subject = args[0]
    provider = _load_provider()
    store = _open_store(provider)
    try:
        try:
            removed = store.unbind(provider.name, subject)
        except ValueError as exc:
            raise _Input("invalid_argument", str(exc)) from exc
        except BindingNotFound as exc:
            raise _Input("binding_not_found", "binding not found") from exc
        normalized = normalize_subject(subject)
        store.record(
            "admin",
            sub=_ACTOR,
            outcome="ok",
            profile_id=removed,
            detail={"command": "unbind", "subject": normalized},
        )
        return {"subject": normalized, "removed_profile": removed}
    finally:
        store.close()


def _bindings(args: list[str]) -> dict:
    if args:
        raise _Usage(_USAGE)
    provider = _load_provider()
    store = _open_store(provider)
    try:
        found = store.bindings(provider.name)
        default = None if found.default is None else {"profile": found.default.profile_id}
        return {
            "user": [{"subject": item.subject, "profile": item.profile_id} for item in found.user],
            "role": [
                {"position": item.position, "subject": item.subject, "profile": item.profile_id}
                for item in found.role
            ],
            "default": default,
        }
    finally:
        store.close()


@dataclass
class _WantedProfile:
    id: str
    variant: Variant
    public: dict[str, str]
    secret: dict[str, str]
    description: str | None


@dataclass
class _WantedBindings:
    users: dict[str, str] = field(default_factory=dict)
    roles: list[tuple[str, str]] = field(default_factory=list)
    default: str | None = None

    def all(self) -> list[tuple[str, str]]:
        found = list(self.users.items()) + list(self.roles)
        if self.default is not None:
            found.append(("default", self.default))
        return found


@dataclass
class _SyncFile:
    profiles: dict[str, _WantedProfile]
    bindings: _WantedBindings | None


def _sync(args: list[str]) -> dict:
    flags, positional = _parse_flags(
        args, options={"--file"}, switches={"--dry-run", "--prune", "--no-check"}
    )
    if positional or flags["--file"] is None:
        raise _Usage(_SYNC_USAGE)
    provider = _load_provider()
    path = flags["--file"]
    if path == "-":
        text = sys.stdin.read()
        base = Path.cwd()
    else:
        file_path = Path(path)
        if not file_path.is_file():
            raise _Input("invalid_argument", "file not found")
        text = file_path.read_text(encoding="utf-8")
        base = file_path.resolve().parent
    wanted = _read_sync_file(provider, text, base)
    store = _open_store(provider)
    try:
        return _apply_sync(
            provider,
            store,
            wanted,
            dry_run=bool(flags["--dry-run"]),
            prune=bool(flags["--prune"]),
            check=not flags["--no-check"],
        )
    finally:
        store.close()


def _read_sync_file(provider: Provider, text: str, base: Path) -> _SyncFile:
    """Validate the whole file before the store is opened. Any problem exits 3."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _Input("invalid_argument", "the file is not valid YAML") from exc
    if not isinstance(data, dict):
        raise _Input("invalid_argument", "the file must be a mapping")
    _only(data, {"version", "profiles", "bindings"}, "file")
    if data.get("version") != 1:
        raise _Input("invalid_argument", "version must be 1")
    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise _Input("invalid_argument", "profiles must be a mapping")
    profiles: dict[str, _WantedProfile] = {}
    for profile_id, entry in raw_profiles.items():
        where = f"profiles.{profile_id}"
        try:
            profiles[str(profile_id)] = _wanted_profile(provider, str(profile_id), entry, base)
        except _Input as exc:
            raise _located(exc, where) from exc
    bindings = None
    if "bindings" in data:
        try:
            bindings = _wanted_bindings(provider, data["bindings"])
        except _Input as exc:
            raise _located(exc, "bindings") from exc
    return _SyncFile(profiles=profiles, bindings=bindings)


def _wanted_profile(provider: Provider, profile_id: str, entry, base: Path) -> _WantedProfile:
    _check_profile_id(profile_id, provider.name)
    if not isinstance(entry, dict):
        raise _Input("invalid_argument", "a profile must be a mapping")
    _only(entry, {"variant", "description", "fields", "secrets", "env_file"}, "profile")
    fields = {item.name: item for item in provider.fields}
    values: dict[str, str] = {}

    def put(name: str, value: str) -> None:
        if name in values:
            raise _Input("invalid_argument", f"{name} is given twice")
        values[name] = value

    env_file = entry.get("env_file")
    if env_file is not None:
        parsed = _parse_env(_read_private_file(_sync_path(env_file, base, "env_file")))
        unknown = sorted(name for name in parsed if name not in fields and parsed[name] != "")
        if unknown:
            raise _Input(
                "unknown_fields",
                "unknown fields: " + ", ".join(unknown),
                {"unknown_fields": unknown},
            )
        for name, value in parsed.items():
            if value != "":
                put(name, value)
    public_fields = entry.get("fields") or {}
    if not isinstance(public_fields, dict):
        raise _Input("invalid_argument", "fields must be a mapping")
    for name, value in public_fields.items():
        if name not in fields:
            raise _Input("unknown_fields", f"unknown fields: {name}", {"unknown_fields": [name]})
        if fields[name].secret:
            raise _Input(
                "secret_in_file",
                f"{name} is a secret field; give it under secrets as a file or env reference",
            )
        if not isinstance(value, str) or value == "":
            raise _Input("invalid_argument", f"{name} must be a non-empty string")
        put(name, value)
    secret_refs = entry.get("secrets") or {}
    if not isinstance(secret_refs, dict):
        raise _Input("invalid_argument", "secrets must be a mapping")
    for name, ref in secret_refs.items():
        if name not in fields:
            raise _Input("unknown_fields", f"unknown fields: {name}", {"unknown_fields": [name]})
        if not fields[name].secret:
            raise _Input("invalid_argument", f"{name} is not a secret field; put it under fields")
        put(name, _secret_value(name, ref, base))
    variant = _select_variant(provider, set(values), entry.get("variant"))
    missing = [name for name in variant.fields if name not in values]
    if missing:
        raise _Input(
            "missing_fields", "missing fields: " + ", ".join(missing), {"missing_fields": missing}
        )
    extra = sorted(set(values) - set(variant.fields))
    if extra:
        raise _Input(
            "invalid_argument",
            "the profile has fields that are not part of the variant",
            {"fields": extra},
        )
    public: dict[str, str] = {}
    secret: dict[str, str] = {}
    for name in variant.fields:
        if len(values[name]) > fields[name].max_length:
            raise _Input("invalid_argument", f"{name} is longer than {fields[name].max_length}")
        if fields[name].secret:
            secret[name] = values[name]
        else:
            public[name] = values[name]
    description = entry.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > 200):
        raise _Input("invalid_argument", "description must be a string of at most 200 characters")
    return _WantedProfile(profile_id, variant, public, secret, description)


def _secret_value(name: str, ref, base: Path) -> str:
    if not isinstance(ref, dict) or len(ref) != 1 or next(iter(ref)) not in {"file", "env"}:
        raise _Input("invalid_argument", f"{name} must be {{file: <path>}} or {{env: <NAME>}}")
    kind, target = next(iter(ref.items()))
    if kind == "file":
        try:
            value = _read_private_file(_sync_path(target, base, name)).rstrip("\r\n")
        except _Input as exc:
            raise _Input(exc.error_id, f"{name}: {exc.message}") from exc
    else:
        if not isinstance(target, str) or not _ENV_NAME.fullmatch(target):
            raise _Input("invalid_argument", f"{name}: invalid environment variable name")
        if target.upper().startswith("BOX_"):
            raise _Input("invalid_argument", f"{name}: BOX_ variables cannot be used")
        value = os.environ.get(target, "")
        if value == "":
            raise _Input("invalid_argument", f"{name}: environment variable {target} is not set")
    if value == "":
        raise _Input("invalid_argument", f"{name}: the secret is empty")
    if "\n" in value or "\r" in value:
        raise _Input("invalid_argument", f"{name}: the secret must be one line")
    return value


def _sync_path(raw, base: Path, what: str) -> str:
    if not isinstance(raw, str) or raw in {"", "-"}:
        raise _Input("invalid_argument", f"{what}: a file path is required")
    path = Path(raw)
    return str(path if path.is_absolute() else base / path)


def _wanted_bindings(provider: Provider, raw) -> _WantedBindings:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _Input("invalid_argument", "bindings must be a mapping")
    _only(raw, {"users", "roles", "default"}, "bindings")
    wanted = _WantedBindings()
    users = raw.get("users") or {}
    if not isinstance(users, dict):
        raise _Input("invalid_argument", "users must be a mapping of subject to profile")
    for name, profile_id in users.items():
        subject = _sync_subject(str(name), "user:")
        if subject in wanted.users:
            raise _Input("invalid_argument", f"{subject} is given twice")
        wanted.users[subject] = _sync_target(provider, subject, profile_id)
    roles = raw.get("roles") or []
    if not isinstance(roles, list):
        raise _Input("invalid_argument", "roles must be a list, in binding order")
    seen: set[str] = set()
    for item in roles:
        if not isinstance(item, dict) or len(item) != 1:
            raise _Input("invalid_argument", "each role is one mapping of role to profile")
        name, profile_id = next(iter(item.items()))
        subject = _sync_subject(str(name), "role:")
        if subject in seen:
            raise _Input("invalid_argument", f"{subject} is given twice")
        seen.add(subject)
        wanted.roles.append((subject, _sync_target(provider, subject, profile_id)))
    if raw.get("default") is not None:
        wanted.default = _sync_target(provider, "default", raw["default"])
    return wanted


def _sync_subject(name: str, prefix: str) -> str:
    subject = name if name.lower().startswith(prefix) else prefix + name
    try:
        normalized = normalize_subject(subject)
    except ValueError as exc:
        raise _Input("invalid_argument", f"invalid subject {name}") from exc
    if not normalized.startswith(prefix):
        raise _Input("invalid_argument", f"invalid subject {name}")
    return normalized


def _sync_target(provider: Provider, subject: str, profile_id) -> str:
    if not isinstance(profile_id, str):
        raise _Input("invalid_argument", f"{subject}: the profile must be a profile id")
    try:
        _check_profile_id(profile_id, provider.name)
    except _Input as exc:
        raise _Input("invalid_argument", f"{subject}: invalid profile id") from exc
    return profile_id


def _only(mapping: dict, allowed: set[str], what: str) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise _Input("invalid_argument", f"unknown keys in {what}: " + ", ".join(unknown))


def _located(exc: _Input, where: str) -> _Input:
    return _Input(exc.error_id, f"{where}: {exc.message}", {**(exc.result or {}), "at": where})


def _apply_sync(
    provider: Provider,
    store: Store,
    wanted: _SyncFile,
    *,
    dry_run: bool,
    prune: bool,
    check: bool,
) -> dict:
    existing = {profile.id: profile for profile in store.list_profiles(provider.name)}
    if wanted.bindings is not None:
        for subject, profile_id in wanted.bindings.all():
            if profile_id not in wanted.profiles and profile_id not in existing:
                raise _Input(
                    "profile_not_found",
                    f"bindings: {subject}: {profile_id} is neither in the file nor in the box",
                    {"at": "bindings", "subject": subject, "profile": profile_id},
                )
    errors: list[dict] = []
    rows: list[dict] = []
    skipped: set[str] = set()
    for profile_id, profile in wanted.profiles.items():
        row = _sync_profile(provider, store, existing.get(profile_id), profile, dry_run, check)
        rows.append(row)
        if row["action"] == "skipped":
            skipped.add(profile_id)
            errors.append(row.pop("error"))
    # A new profile that was skipped does not exist; bindings to it are skipped too.
    missing = {profile_id for profile_id in skipped if profile_id not in existing}
    binding_rows: list[dict] = []
    final: dict[str, str] | None = None
    if wanted.bindings is not None:
        final = _sync_bindings(
            provider, store, wanted.bindings, missing, binding_rows, errors, dry_run, prune
        )
    if final is None:
        found = store.bindings(provider.name)
        final = {item.subject: item.profile_id for item in [*found.user, *found.role]}
        if found.default is not None:
            final["default"] = found.default.profile_id
    left: list[dict] = []
    in_use = set(final.values())
    for profile_id, profile in existing.items():
        if profile_id in wanted.profiles:
            continue
        managed = profile.managed_by == _SYNC_MARK
        entry = {"profile": profile_id, "managed": managed, "action": "kept"}
        if prune and managed:
            if profile_id in in_use:
                entry["action"] = "delete_failed"
                errors.append(
                    {
                        "profile": profile_id,
                        "id": "profile_in_use",
                        "message": "profile is still bound and was not deleted",
                    }
                )
            elif not dry_run:
                try:
                    store.delete_profile(profile_id)
                except (ProfileInUse, ProfileNotFound):
                    entry["action"] = "delete_failed"
                    errors.append(
                        {
                            "profile": profile_id,
                            "id": "profile_in_use",
                            "message": "profile is still bound and was not deleted",
                        }
                    )
                else:
                    entry["action"] = "deleted"
                    _sync_audit(store, profile_id, {"action": "deleted"})
            else:
                entry["action"] = "deleted"
        left.append(entry)
    result = {
        "dry_run": dry_run,
        "prune": prune,
        "profiles": rows,
        "bindings": binding_rows,
        "not_in_file": left,
        "errors": errors,
    }
    if errors:
        raise _Partial("some changes were not applied; see errors", result)
    return result


def _sync_profile(
    provider: Provider,
    store: Store,
    current,
    wanted: _WantedProfile,
    dry_run: bool,
    check: bool,
) -> dict:
    row: dict = {"profile": wanted.id, "variant": wanted.variant.name}
    if current is not None and _same_profile(store, current, wanted):
        row["action"] = "unchanged"
        return row
    action = "created" if current is None else "rotated"
    row["action"] = action
    if dry_run:
        return row
    check_result = None
    if check:
        check_result = asyncio.run(
            run_checks(
                provider,
                wanted.variant,
                {**wanted.public, **wanted.secret},
                instances=None,
                config=url_policy_from_mapping(os.environ),
            )
        )
        if check_result.ok is False:
            row.update(action="skipped", check="rejected")
            row["error"] = {
                "profile": wanted.id,
                "id": "check_failed",
                "message": _rejection_message(wanted.variant, check_result),
            }
            return row
    try:
        if current is None:
            store.create_profile(
                wanted.id,
                provider.name,
                wanted.variant.name,
                wanted.public,
                wanted.description,
                _ACTOR,
                managed_by=_SYNC_MARK,
            )
            store.put_secrets(wanted.id, wanted.secret, _ACTOR, version=1)
        else:
            store.rotate_profile(
                wanted.id,
                variant=wanted.variant.name,
                public_fields=wanted.public,
                secrets=wanted.secret,
                by=_ACTOR,
                description=wanted.description,
                managed_by=_SYNC_MARK,
            )
    except (ProfileExists, ProfileNotFound):
        row["action"] = "skipped"
        row["error"] = {
            "profile": wanted.id,
            "id": "profile_changed",
            "message": "the profile was created or deleted by someone else during sync",
        }
        return row
    if check_result is not None:
        store.set_check(
            wanted.id, check_result.ok, check_result.identity, datetime.now(timezone.utc)
        )
    row["check"] = "skipped" if not check else _check_label(check_result)
    _sync_audit(
        store, wanted.id, {"action": action, "check": row["check"], "variant": wanted.variant.name}
    )
    return row


def _same_profile(store: Store, current, wanted: _WantedProfile) -> bool:
    if current.variant != wanted.variant.name or current.public_fields != wanted.public:
        return False
    if wanted.description is not None and current.description != wanted.description:
        return False
    try:
        stored = store.read_secrets(current.id)
    except SecretUnreadable:
        return False
    # Compared in memory; neither side is printed or logged.
    return hmac.compare_digest(_canonical(stored), _canonical(wanted.secret))


def _canonical(values: dict) -> bytes:
    return json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sync_bindings(
    provider: Provider,
    store: Store,
    wanted: _WantedBindings,
    missing: set[str],
    rows: list[dict],
    errors: list[dict],
    dry_run: bool,
    prune: bool,
) -> dict[str, str]:
    """Apply the file's bindings and return every binding as it stands afterwards."""
    found = store.bindings(provider.name)
    final: dict[str, str] = {}
    kept: set[str] = set()

    def unusable(subject: str, profile_id: str) -> bool:
        if profile_id not in missing:
            return False
        kept.add(subject)
        rows.append({"subject": subject, "profile": profile_id, "action": "skipped"})
        errors.append(
            {
                "subject": subject,
                "profile": profile_id,
                "id": "profile_skipped",
                "message": "the profile was not created, so this binding was not made",
            }
        )
        return True

    users = {item.subject: item.profile_id for item in found.user}
    for subject, profile_id in wanted.users.items():
        if unusable(subject, profile_id):
            continue
        final[subject] = profile_id
        if users.get(subject) == profile_id:
            continue
        action = "rebound" if subject in users else "bound"
        if not dry_run:
            store.bind(provider.name, subject, profile_id, _ACTOR, replace=True)
            _sync_audit(store, profile_id, {"action": action, "subject": subject})
        rows.append({"subject": subject, "profile": profile_id, "action": action})
    for subject, profile_id in users.items():
        if subject in wanted.users and subject not in kept:
            continue
        if prune and subject not in kept:
            _sync_unbind(provider, store, subject, profile_id, rows, dry_run)
        else:
            final[subject] = profile_id

    before = [(item.subject, item.profile_id) for item in found.role]
    ordered = [
        (subject, profile_id)
        for subject, profile_id in wanted.roles
        if not unusable(subject, profile_id)
    ]
    listed = {subject for subject, _profile_id in ordered}
    ordered += [
        (subject, profile_id)
        for subject, profile_id in before
        if subject not in listed and (not prune or subject in kept)
    ]
    if ordered != before:
        if not dry_run:
            store.set_roles(provider.name, ordered, _ACTOR)
            _sync_audit(store, None, {"action": "roles", "order": [s for s, _p in ordered]})
        old = {
            subject: (index, profile_id) for index, (subject, profile_id) in enumerate(before, 1)
        }
        new = {
            subject: (index, profile_id) for index, (subject, profile_id) in enumerate(ordered, 1)
        }
        for subject, (position, profile_id) in new.items():
            if subject not in old:
                action = "bound"
            elif old[subject][1] != profile_id:
                action = "rebound"
            elif old[subject][0] != position:
                action = "moved"
            else:
                continue
            rows.append(
                {"subject": subject, "profile": profile_id, "action": action, "position": position}
            )
        for subject, (_position, profile_id) in old.items():
            if subject not in new:
                rows.append({"subject": subject, "profile": profile_id, "action": "unbound"})
    final.update(dict(ordered))

    current_default = None if found.default is None else found.default.profile_id
    if wanted.default is not None and not unusable("default", wanted.default):
        final["default"] = wanted.default
        if current_default != wanted.default:
            action = "bound" if current_default is None else "rebound"
            if not dry_run:
                store.bind(provider.name, "default", wanted.default, _ACTOR, replace=True)
                _sync_audit(store, wanted.default, {"action": action, "subject": "default"})
            rows.append({"subject": "default", "profile": wanted.default, "action": action})
    elif current_default is not None:
        if prune and "default" not in kept:
            _sync_unbind(provider, store, "default", current_default, rows, dry_run)
        else:
            final["default"] = current_default
    return final


def _sync_unbind(
    provider: Provider,
    store: Store,
    subject: str,
    profile_id: str,
    rows: list[dict],
    dry_run: bool,
) -> None:
    if not dry_run:
        try:
            store.unbind(provider.name, subject)
        except BindingNotFound:
            pass
        else:
            _sync_audit(store, profile_id, {"action": "unbound", "subject": subject})
    rows.append({"subject": subject, "profile": profile_id, "action": "unbound"})


def _sync_audit(store: Store, profile_id: str | None, detail: dict) -> None:
    store.record(
        "admin",
        sub=_ACTOR,
        outcome="ok",
        profile_id=profile_id,
        detail={"command": "sync", **detail},
    )


def _store(args: list[str]) -> dict:
    if not args:
        raise _Usage(_USAGE)
    if args[0] == "status" and len(args) == 1:
        provider = _load_provider()
        store = _open_store(provider)
        try:
            profiles = store.list_profiles()
            return {
                "key_state": store.key_state,
                "schema_version": store.schema_version(),
                "profiles": len(profiles),
                "needs_reentry": sum(1 for profile in profiles if profile.state == "needs_reentry"),
            }
        finally:
            store.close()
    if args[0] == "rekey":
        flags, positional = _parse_flags(args[1:], options={"--old-key-file"}, switches=set())
        if positional or flags["--old-key-file"] is None:
            raise _Usage("usage: credsctl store rekey --old-key-file <path|->")
        old_key = _read_private_file(flags["--old-key-file"]).strip()
        if not old_key:
            raise _Input("invalid_argument", "old key file is empty")
        provider = _load_provider()
        store = _open_store(provider)
        try:
            try:
                store.rekey(old_key)
            except SecretUnreadable as exc:
                raise _Input("invalid_argument", "old key cannot read the stored secrets") from exc
            store.record(
                "admin",
                sub=_ACTOR,
                outcome="ok",
                detail={"command": "store rekey"},
            )
            return {"key_state": store.key_state}
        finally:
            store.close()
    raise _Usage(_USAGE)


def _audit_command(args: list[str]) -> dict:
    flags, positional = _parse_flags(
        args, options={"--since", "--profile", "--event"}, switches=set()
    )
    if positional:
        raise _Usage("usage: credsctl audit [--since 24h] [--profile <id>] [--event <name>]")
    since = None if flags["--since"] is None else _since_stamp(flags["--since"])
    provider = _load_provider()
    store = _open_store(provider)
    try:
        store.purge()
        events = store.list_audit(
            since=since, profile_id=flags["--profile"], event=flags["--event"]
        )
        return {"events": events}
    finally:
        store.close()


def _snapshot(args: list[str]) -> dict:
    """``list`` shows stored tool snapshots without their JSON. ``export`` returns one.

    Without ``--json``, ``export`` prints the stored JSON exactly as stored.
    """
    if args == ["list"]:
        provider = _load_provider()
        store = _open_store(provider)
        try:
            return {
                "snapshots": [
                    {
                        "id": item.id,
                        "sha256": item.sha256,
                        "taken_at": item.taken_at,
                        "profile": item.profile,
                    }
                    for item in store.list_tool_snapshots()
                ]
            }
        finally:
            store.close()
    if len(args) == 2 and args[0] == "export":
        raw = args[1]
        provider = _load_provider()
        store = _open_store(provider)
        try:
            found = None
            if raw.isdigit() and raw.isascii():
                found = store.get_tool_snapshot(int(raw))
            if found is None:
                raise _Input("snapshot_not_found", "snapshot not found")
            return {"id": found.id, "sha256": found.sha256, "snapshot": found.body}
        finally:
            store.close()
    raise _Usage("usage: credsctl snapshot list | credsctl snapshot export <id>")


def _status(args: list[str]) -> dict:
    if args:
        raise _Usage(_USAGE)
    provider = _load_provider()
    store = _open_store(provider)
    try:
        document = store.read_status()
        if document is None:
            return {"status": "not running"}
        return document
    finally:
        store.close()


def _rejection_message(variant, result) -> str:
    title = variant_title(variant)
    status = next((item["http_status"] for item in result.results if item["ok"] is False), None)
    if isinstance(status, int):
        return f"{title} did not accept these values (HTTP {status})"
    return f"{title} did not accept these values"


def _check_label(result) -> str:
    if result is None or result.ok is None:
        return "unreachable"
    return "ok"


def _select_variant(provider: Provider, present: set[str], requested: str | None):
    if requested is None:
        matches = [variant for variant in provider.variants if set(variant.fields) == present]
        if len(matches) != 1:
            listing = [
                {"name": variant.name, "fields": list(variant.fields)}
                for variant in provider.variants
            ]
            raise _Input(
                "variant_ambiguous",
                "no variant has exactly these fields",
                {"variants": listing},
            )
        return matches[0]
    for variant in provider.variants:
        if variant.name == requested:
            return variant
    raise _Input("invalid_argument", "unknown variant")


def _public_profile(profile) -> dict:
    return {
        "id": profile.id,
        "provider": profile.provider,
        "name": profile.name,
        "variant": profile.variant,
        "description": profile.description,
        "state": profile.state,
        "public_fields": profile.public_fields,
        "version": profile.version,
        "created_at": profile.created_at,
        "created_by": profile.created_by,
        "rotated_at": profile.rotated_at,
        "rotated_by": profile.rotated_by,
        "last_check_at": profile.last_check_at,
        "last_check_ok": profile.last_check_ok,
        "last_check_identity": profile.last_check_identity,
        "managed_by": profile.managed_by,
    }


def _load_provider() -> Provider:
    name = os.environ.get("BOX_PROVIDER", "")
    if not _PROVIDER_NAME.fullmatch(name):
        raise _Usage("BOX_PROVIDER is required")
    directory = os.environ.get("BOX_PROVIDER_DIR") or f"/opt/box/packages/{name}"
    try:
        loaded = load_provider(directory)
    except ProviderError as exc:
        message = exc.messages[0] if exc.messages else "invalid provider"
        raise _Usage(message) from exc
    if loaded.name != name:
        raise _Usage("BOX_PROVIDER does not match the provider directory")
    return loaded


def _open_store(provider: Provider | None = None) -> Store:
    if provider is None:
        provider = _load_provider()
    data_dir = os.environ.get("BOX_DATA_DIR", "/data")
    store_key = os.environ.get("BOX_STORE_KEY")
    if not store_key:
        raise _Usage("BOX_STORE_KEY is required")
    if len(store_key.encode("utf-8")) < 32:
        raise _Usage("BOX_STORE_KEY must be at least 32 bytes")
    try:
        return Store.open(
            data_dir,
            store_key,
            field_names=[field.name for field in provider.fields],
        )
    except StoreSchemaNewer:
        raise
    except OSError as exc:
        raise _Unavailable("the store could not be opened") from exc


def _check_profile_id(profile_id: str, provider_name: str) -> None:
    prefix, separator, name = profile_id.partition("/")
    if separator != "/" or prefix != provider_name or not _PROFILE_NAME.fullmatch(name):
        raise _Input("invalid_argument", "invalid profile id")


def _read_private_file(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    file_path = Path(path)
    if not file_path.is_file():
        raise _Input("invalid_argument", "file not found")
    mode = file_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise _Input("invalid_argument", "file must be mode 600")
    return file_path.read_text(encoding="utf-8")


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise _Input("invalid_argument", "invalid env line")
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not name or any(character.isspace() for character in name):
            raise _Input("invalid_argument", "invalid env line")
        # Quotes are part of the value. The file is not a shell script.
        values[name] = value
    return values


def _position(raw: str | None) -> int | None:
    if raw is None:
        return None
    if not raw.isdigit():
        raise _Input("invalid_argument", "invalid position")
    value = int(raw)
    if value < 1:
        raise _Input("invalid_argument", "invalid position")
    return value


def _since_stamp(text: str) -> str:
    match = _SINCE.fullmatch(text)
    if match is None:
        raise _Usage("invalid --since value")
    amount = int(match.group(1))
    unit = match.group(2)
    delta = {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]
    moment = datetime.now(timezone.utc) - delta
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_flags(
    args: list[str], *, options: set[str], switches: set[str]
) -> tuple[dict, list[str]]:
    parsed: dict = {name: None for name in options}
    for name in switches:
        parsed[name] = False
    positional: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in switches:
            parsed[token] = True
            index += 1
            continue
        if token in options:
            if parsed[token] is not None:
                raise _Usage(f"duplicate {token}")
            if index + 1 >= len(args):
                raise _Usage(f"{token} needs a value")
            parsed[token] = args[index + 1]
            index += 2
            continue
        if token.startswith("-"):
            raise _Usage(f"unknown argument {token}")
        positional.append(token)
        index += 1
    return parsed, positional


def _extract_json(args: list[str]) -> tuple[bool, list[str]]:
    json_mode = False
    rest: list[str] = []
    for token in args:
        if token == "--json":
            json_mode = True
        else:
            rest.append(token)
    return json_mode, rest


def _drop_root() -> None:
    if os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(_WRAPPER_UID)
    os.setuid(_WRAPPER_UID)


def _exit_usage(json_mode: bool) -> None:
    _exit(json_mode, ok=False, code=2, error=_error("usage", _USAGE), result=None)


def _exit(json_mode: bool, *, ok: bool, code: int, error: dict | None, result: dict | None) -> None:
    if json_mode:
        print(
            json.dumps(
                {"ok": ok, "code": code, "error": error, "result": result},
                ensure_ascii=False,
            )
        )
    else:
        if error is not None:
            if result is not None and "not_in_file" in result:
                _print_human(result)
            print(error["message"], file=sys.stderr)
        elif result is not None:
            _print_human(result)
    raise SystemExit(code)


def _print_sync(result: dict) -> None:
    if result["dry_run"]:
        print("dry run: nothing was written")
    for row in result["profiles"]:
        check = f"  check: {row['check']}" if "check" in row else ""
        print(f"profile  {row['profile']}  {row['action']}{check}")
    for row in result["bindings"]:
        position = f"  position {row['position']}" if "position" in row else ""
        print(f"binding  {row['subject']}  {row['action']}  {row['profile']}{position}")
    for row in result["not_in_file"]:
        managed = "managed by sync" if row["managed"] else "not managed by sync"
        print(f"not in file  {row['profile']}  {row['action']}  ({managed})")
    for row in result["errors"]:
        target = row.get("subject") or row.get("profile")
        print(f"error  {target}  {row['id']}  {row['message']}")


def _error(error_id: str, message: str) -> dict:
    return {"id": error_id, "message": message}


def _print_human(result: dict) -> None:
    if "not_in_file" in result:
        _print_sync(result)
        return
    if "profiles" in result and "key_state" not in result:
        for profile in result["profiles"]:
            print(f"{profile['id']}  {profile['variant']}  {profile['state']}")
        return
    if "user" in result and "role" in result and "default" in result:
        print("user")
        for item in result["user"]:
            print(f"  {item['subject']}  {item['profile']}")
        print("role")
        for item in result["role"]:
            print(f"  {item['position']}  {item['subject']}  {item['profile']}")
        print("default")
        if result["default"] is None:
            print("  none")
        else:
            print(f"  {result['default']['profile']}")
        return
    if "snapshot" in result and isinstance(result["snapshot"], str):
        sys.stdout.write(result["snapshot"])
        return
    if "snapshots" in result:
        for item in result["snapshots"]:
            print(f"{item['id']}  {item['sha256']}  {item['taken_at']}  {item['profile']}")
        return
    if "events" in result:
        for event in result["events"]:
            profile_id = event.get("profile_id") or ""
            print(f"{event['at']}  {event['event']}  {event['outcome']}  {profile_id}")
        return
    shown = {key: value for key, value in result.items() if key != "public_fields"}
    print(json.dumps(shown, ensure_ascii=False))
