"""Operator CLI for profiles, ordered bindings, and the store key.

Field values are never printed. A credential check is not performed here: an
import that asks for one reports ``check: unreachable`` and still exits 0.
Exit 1 is reserved for a provider that rejects the values.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stdio_box.provider import Provider, ProviderError, load_provider
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

_USAGE = """usage: credsctl [--json] <command> [<args>]
       credsctl profile list
       credsctl profile delete <id>
       credsctl profile import <id> [--variant <name>] --from-file <path|->
           [--if-absent] [--ignore-unknown] [--no-check] [--description <text>]
       credsctl bind <subject> <profile> [--replace] [--position N]
       credsctl unbind <subject>
       credsctl bindings
       credsctl store status
       credsctl store rekey --old-key-file <path|->
       credsctl audit [--since 24h] [--profile <id>] [--event <name>]
       credsctl status"""

_ACTOR = "cli:credsctl"
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_SINCE = re.compile(r"^(\d+)([smhd])$")
_WRAPPER_UID = 10001


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
    except _Input as exc:
        _exit(
            json_mode, ok=False, code=3, error=_error(exc.error_id, exc.message), result=exc.result
        )
    except _Exists as exc:
        _exit(json_mode, ok=False, code=4, error=_error("profile_exists", exc.message), result=None)
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
    if command == "store":
        return _store(rest)
    if command == "audit":
        return _audit_command(rest)
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
        check = "skipped" if flags["--no-check"] else "unreachable"
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
            print(error["message"], file=sys.stderr)
        elif result is not None:
            _print_human(result)
    raise SystemExit(code)


def _error(error_id: str, message: str) -> dict:
    return {"id": error_id, "message": message}


def _print_human(result: dict) -> None:
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
    if "events" in result:
        for event in result["events"]:
            profile_id = event.get("profile_id") or ""
            print(f"{event['at']}  {event['event']}  {event['outcome']}  {profile_id}")
        return
    shown = {key: value for key, value in result.items() if key != "public_fields"}
    print(json.dumps(shown, ensure_ascii=False))
