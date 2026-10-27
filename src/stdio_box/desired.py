"""The set of profiles and bindings a box should hold, as ``credsctl sync`` reads it.

The same YAML is read by ``credsctl sync`` on the server and by the
``creds_desired_set`` tool. On the server a secret is a ``{file: ...}`` or
``{env: ...}`` reference, or comes from ``env_file``. Through the tool no secret
is accepted at all: each one is entered on a one-time form.

Binding and prune rules are shared, so both paths treat the box the same way.
"""

import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from stdio_box.provider import Provider, Variant
from stdio_box.store.errors import BindingNotFound, ProfileInUse, ProfileNotFound
from stdio_box.store.repo import Profile, Store, normalize_subject

SYNC_MARK = "credsctl sync"
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

Audit = Callable[[str | None, dict], None]


class DesiredError(Exception):
    """A file or argument problem. ``credsctl`` exits 3 on it."""

    def __init__(self, error_id: str, message: str, result: dict | None = None) -> None:
        self.error_id = error_id
        self.message = message
        self.result = result
        super().__init__(message)


@dataclass
class WantedProfile:
    id: str
    variant: Variant
    public: dict[str, str]
    # None when the set came through the tool: secrets are entered on a form.
    secret: dict[str, str] | None
    description: str | None


@dataclass
class WantedBindings:
    users: dict[str, str] = field(default_factory=dict)
    roles: list[tuple[str, str]] = field(default_factory=list)
    default: str | None = None

    def all(self) -> list[tuple[str, str]]:
        found = list(self.users.items()) + list(self.roles)
        if self.default is not None:
            found.append(("default", self.default))
        return found


@dataclass
class DesiredSet:
    profiles: dict[str, WantedProfile]
    bindings: WantedBindings | None
    # How messages name the text: "file" for credsctl, "set" for the tools.
    noun: str = "file"


def parse_desired(
    provider: Provider, text: str, base: Path | None, *, secrets: bool = True
) -> DesiredSet:
    """Validate the whole file. ``secrets`` false refuses every secret reference.

    With ``secrets`` false the text came through a tool, so messages call it
    the set and never point at ``credsctl``-only keys.
    """
    noun = "file" if secrets else "set"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DesiredError("invalid_argument", f"the {noun} is not valid YAML") from exc
    if not isinstance(data, dict):
        raise DesiredError("invalid_argument", f"the {noun} must be a mapping")
    _only(data, {"version", "profiles", "bindings"}, noun)
    if data.get("version") != 1:
        raise DesiredError("invalid_argument", "version must be 1")
    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise DesiredError("invalid_argument", "profiles must be a mapping")
    profiles: dict[str, WantedProfile] = {}
    for profile_id, entry in raw_profiles.items():
        where = f"profiles.{profile_id}"
        try:
            if secrets:
                assert base is not None
                wanted = _wanted_profile(provider, str(profile_id), entry, base)
            else:
                wanted = _wanted_public_profile(provider, str(profile_id), entry)
        except DesiredError as exc:
            raise _located(exc, where) from exc
        profiles[str(profile_id)] = wanted
    bindings = None
    if "bindings" in data:
        try:
            bindings = _wanted_bindings(provider, data["bindings"])
        except DesiredError as exc:
            raise _located(exc, "bindings") from exc
    return DesiredSet(profiles=profiles, bindings=bindings, noun=noun)


def check_binding_targets(desired: DesiredSet, existing: set[str]) -> None:
    """Every bound profile is in the set or already in the box."""
    if desired.bindings is None:
        return
    for subject, profile_id in desired.bindings.all():
        if profile_id not in desired.profiles and profile_id not in existing:
            raise DesiredError(
                "profile_not_found",
                f"bindings: {subject}: {profile_id} is neither in the {desired.noun} "
                "nor in the box",
                {"at": "bindings", "subject": subject, "profile": profile_id},
            )


def _wanted_profile(provider: Provider, profile_id: str, entry, base: Path) -> WantedProfile:
    check_profile_id(profile_id, provider.name)
    if not isinstance(entry, dict):
        raise DesiredError("invalid_argument", "a profile must be a mapping")
    _only(entry, {"variant", "description", "fields", "secrets", "env_file"}, "profile")
    fields = {item.name: item for item in provider.fields}
    values: dict[str, str] = {}

    def put(name: str, value: str) -> None:
        if name in values:
            raise DesiredError("invalid_argument", f"{name} is given twice")
        values[name] = value

    env_file = entry.get("env_file")
    if env_file is not None:
        parsed = parse_env(read_private_file(_sync_path(env_file, base, "env_file")))
        unknown = sorted(name for name in parsed if name not in fields and parsed[name] != "")
        if unknown:
            raise DesiredError(
                "unknown_fields",
                "unknown fields: " + ", ".join(unknown),
                {"unknown_fields": unknown},
            )
        for name, value in parsed.items():
            if value != "":
                put(name, value)
    for name, value in _public_entries(provider, entry, secrets=True):
        put(name, value)
    secret_refs = entry.get("secrets") or {}
    if not isinstance(secret_refs, dict):
        raise DesiredError("invalid_argument", "secrets must be a mapping")
    for name, ref in secret_refs.items():
        if name not in fields:
            raise DesiredError(
                "unknown_fields", f"unknown fields: {name}", {"unknown_fields": [name]}
            )
        if not fields[name].secret:
            raise DesiredError(
                "invalid_argument", f"{name} is not a secret field; put it under fields"
            )
        put(name, _secret_value(name, ref, base))
    variant = select_variant(provider, set(values), entry.get("variant"))
    missing = [name for name in variant.fields if name not in values]
    if missing:
        raise DesiredError(
            "missing_fields", "missing fields: " + ", ".join(missing), {"missing_fields": missing}
        )
    _no_extra(variant, set(values))
    public: dict[str, str] = {}
    secret: dict[str, str] = {}
    for name in variant.fields:
        if len(values[name]) > fields[name].max_length:
            raise DesiredError(
                "invalid_argument", f"{name} is longer than {fields[name].max_length}"
            )
        if fields[name].secret:
            secret[name] = values[name]
        else:
            public[name] = values[name]
    return WantedProfile(profile_id, variant, public, secret, _description(entry))


def _wanted_public_profile(provider: Provider, profile_id: str, entry) -> WantedProfile:
    check_profile_id(profile_id, provider.name)
    if not isinstance(entry, dict):
        raise DesiredError("invalid_argument", "a profile must be a mapping")
    if "secrets" in entry or "env_file" in entry:
        raise DesiredError(
            "secret_reference",
            "secrets are not part of the set, so secrets, {file: ...}, {env: ...} and "
            "env_file are not accepted; creds_sync sends a one-time form link where the "
            "account owner enters each secret",
        )
    _only(entry, {"variant", "description", "fields"}, "profile")
    fields = {item.name: item for item in provider.fields}
    public = dict(_public_entries(provider, entry, secrets=False))
    requested = entry.get("variant")
    if requested is None and len(provider.variants) == 1:
        variant = provider.variants[0]
    elif requested is None:
        # Only the public fields are known here, so match on those.
        matches = [
            item
            for item in provider.variants
            if {name for name in item.fields if not fields[name].secret} == set(public)
        ]
        if len(matches) != 1:
            raise DesiredError(
                "variant_ambiguous",
                "give variant: no single variant has exactly these fields",
                {"variants": [item.name for item in provider.variants]},
            )
        variant = matches[0]
    else:
        variant = select_variant(provider, set(), requested)
    missing = [name for name in variant.fields if not fields[name].secret and name not in public]
    if missing:
        raise DesiredError(
            "missing_fields", "missing fields: " + ", ".join(missing), {"missing_fields": missing}
        )
    _no_extra(variant, set(public))
    for name, value in public.items():
        if len(value) > fields[name].max_length:
            raise DesiredError(
                "invalid_argument", f"{name} is longer than {fields[name].max_length}"
            )
    return WantedProfile(profile_id, variant, public, None, _description(entry))


def _public_entries(provider: Provider, entry: dict, *, secrets: bool) -> list[tuple[str, str]]:
    fields = {item.name: item for item in provider.fields}
    public_fields = entry.get("fields") or {}
    if not isinstance(public_fields, dict):
        raise DesiredError("invalid_argument", "fields must be a mapping")
    found = []
    for name, value in public_fields.items():
        if name not in fields:
            raise DesiredError(
                "unknown_fields", f"unknown fields: {name}", {"unknown_fields": [name]}
            )
        if fields[name].secret and secrets:
            raise DesiredError(
                "secret_in_file",
                f"{name} is a secret field; give it under secrets as a file or env reference",
            )
        if fields[name].secret:
            raise DesiredError(
                "secret_in_file",
                f"{name} is a secret field and is not part of the set; creds_sync sends "
                "a one-time form link where the account owner enters it",
            )
        if not isinstance(value, str) or value == "":
            raise DesiredError("invalid_argument", f"{name} must be a non-empty string")
        if "\r" in value or "\n" in value:
            raise DesiredError("invalid_argument", f"{name} must be one line")
        found.append((name, value))
    return found


def _no_extra(variant: Variant, present: set[str]) -> None:
    extra = sorted(present - set(variant.fields))
    if extra:
        raise DesiredError(
            "invalid_argument",
            "the profile has fields that are not part of the variant",
            {"fields": extra},
        )


def _description(entry: dict) -> str | None:
    description = entry.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > 200):
        raise DesiredError(
            "invalid_argument", "description must be a string of at most 200 characters"
        )
    return description


def _secret_value(name: str, ref, base: Path) -> str:
    if not isinstance(ref, dict) or len(ref) != 1 or next(iter(ref)) not in {"file", "env"}:
        raise DesiredError(
            "invalid_argument", f"{name} must be {{file: <path>}} or {{env: <NAME>}}"
        )
    kind, target = next(iter(ref.items()))
    if kind == "file":
        try:
            value = read_private_file(_sync_path(target, base, name)).rstrip("\r\n")
        except DesiredError as exc:
            raise DesiredError(exc.error_id, f"{name}: {exc.message}") from exc
    else:
        if not isinstance(target, str) or not _ENV_NAME.fullmatch(target):
            raise DesiredError("invalid_argument", f"{name}: invalid environment variable name")
        if target.upper().startswith("BOX_"):
            raise DesiredError("invalid_argument", f"{name}: BOX_ variables cannot be used")
        value = os.environ.get(target, "")
        if value == "":
            raise DesiredError(
                "invalid_argument", f"{name}: environment variable {target} is not set"
            )
    if value == "":
        raise DesiredError("invalid_argument", f"{name}: the secret is empty")
    if "\n" in value or "\r" in value:
        raise DesiredError("invalid_argument", f"{name}: the secret must be one line")
    return value


def _sync_path(raw, base: Path, what: str) -> str:
    if not isinstance(raw, str) or raw in {"", "-"}:
        raise DesiredError("invalid_argument", f"{what}: a file path is required")
    path = Path(raw)
    return str(path if path.is_absolute() else base / path)


def _wanted_bindings(provider: Provider, raw) -> WantedBindings:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise DesiredError("invalid_argument", "bindings must be a mapping")
    _only(raw, {"users", "roles", "default"}, "bindings")
    wanted = WantedBindings()
    users = raw.get("users") or {}
    if not isinstance(users, dict):
        raise DesiredError("invalid_argument", "users must be a mapping of subject to profile")
    for name, profile_id in users.items():
        subject = _sync_subject(str(name), "user:")
        if subject in wanted.users:
            raise DesiredError("invalid_argument", f"{subject} is given twice")
        wanted.users[subject] = _sync_target(provider, subject, profile_id)
    roles = raw.get("roles") or []
    if not isinstance(roles, list):
        raise DesiredError("invalid_argument", "roles must be a list, in binding order")
    seen: set[str] = set()
    for item in roles:
        if not isinstance(item, dict) or len(item) != 1:
            raise DesiredError("invalid_argument", "each role is one mapping of role to profile")
        name, profile_id = next(iter(item.items()))
        subject = _sync_subject(str(name), "role:")
        if subject in seen:
            raise DesiredError("invalid_argument", f"{subject} is given twice")
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
        raise DesiredError("invalid_argument", f"invalid subject {name}") from exc
    if not normalized.startswith(prefix):
        raise DesiredError("invalid_argument", f"invalid subject {name}")
    return normalized


def _sync_target(provider: Provider, subject: str, profile_id) -> str:
    if not isinstance(profile_id, str):
        raise DesiredError("invalid_argument", f"{subject}: the profile must be a profile id")
    try:
        check_profile_id(profile_id, provider.name)
    except DesiredError as exc:
        raise DesiredError("invalid_argument", f"{subject}: invalid profile id") from exc
    return profile_id


def _only(mapping: dict, allowed: set[str], what: str) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise DesiredError("invalid_argument", f"unknown keys in {what}: " + ", ".join(unknown))


def _located(exc: DesiredError, where: str) -> DesiredError:
    return DesiredError(
        exc.error_id, f"{where}: {exc.message}", {**(exc.result or {}), "at": where}
    )


def sync_bindings(
    provider: Provider,
    store: Store,
    wanted: WantedBindings,
    missing: set[str],
    rows: list[dict],
    errors: list[dict],
    *,
    dry_run: bool,
    prune: bool,
    actor: str,
    audit: Audit,
) -> dict[str, str]:
    """Apply the file's bindings and return every binding as it stands afterwards.

    A binding to a profile in ``missing`` is skipped and reported in ``errors``.
    A binding the file does not name is ``unbound`` with ``prune`` and ``kept``
    without it, so the report shows it either way.
    """
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
            store.bind(provider.name, subject, profile_id, actor, replace=True)
            audit(profile_id, {"action": action, "subject": subject})
        rows.append({"subject": subject, "profile": profile_id, "action": action})
    for subject, profile_id in users.items():
        if subject in wanted.users and subject not in kept:
            continue
        if prune and subject not in kept:
            _sync_unbind(provider, store, subject, profile_id, rows, dry_run, audit)
        else:
            final[subject] = profile_id
            if subject not in wanted.users:
                rows.append({"subject": subject, "profile": profile_id, "action": "kept"})

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
            store.set_roles(provider.name, ordered, actor)
            audit(None, {"action": "roles", "order": [s for s, _p in ordered]})
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
    reported = {row["subject"] for row in rows}
    named = {subject for subject, _profile_id in wanted.roles}
    for position, (subject, profile_id) in enumerate(ordered, 1):
        if subject not in named and subject not in reported:
            rows.append(
                {"subject": subject, "profile": profile_id, "action": "kept", "position": position}
            )
    final.update(dict(ordered))

    current_default = None if found.default is None else found.default.profile_id
    if wanted.default is not None and not unusable("default", wanted.default):
        final["default"] = wanted.default
        if current_default != wanted.default:
            action = "bound" if current_default is None else "rebound"
            if not dry_run:
                store.bind(provider.name, "default", wanted.default, actor, replace=True)
                audit(wanted.default, {"action": action, "subject": "default"})
            rows.append({"subject": "default", "profile": wanted.default, "action": action})
    elif current_default is not None:
        if prune and "default" not in kept:
            _sync_unbind(provider, store, "default", current_default, rows, dry_run, audit)
        else:
            final["default"] = current_default
            if wanted.default is None:
                rows.append({"subject": "default", "profile": current_default, "action": "kept"})
    return final


def _sync_unbind(
    provider: Provider,
    store: Store,
    subject: str,
    profile_id: str,
    rows: list[dict],
    dry_run: bool,
    audit: Audit,
) -> None:
    if not dry_run:
        try:
            store.unbind(provider.name, subject)
        except BindingNotFound:
            pass
        else:
            audit(profile_id, {"action": "unbound", "subject": subject})
    rows.append({"subject": subject, "profile": profile_id, "action": "unbound"})


def current_bindings(provider: Provider, store: Store) -> dict[str, str]:
    found = store.bindings(provider.name)
    final = {item.subject: item.profile_id for item in [*found.user, *found.role]}
    if found.default is not None:
        final["default"] = found.default.profile_id
    return final


def not_in_set(
    store: Store,
    existing: dict[str, Profile],
    wanted: set[str],
    final: dict[str, str],
    errors: list[dict],
    *,
    dry_run: bool,
    prune: bool,
    audit: Audit,
) -> list[dict]:
    """Profiles the set does not name. ``prune`` deletes only those sync wrote."""
    left: list[dict] = []
    in_use = set(final.values())
    for profile_id, profile in existing.items():
        if profile_id in wanted:
            continue
        managed = profile.managed_by == SYNC_MARK
        entry = {"profile": profile_id, "managed": managed, "action": "kept"}
        if prune and managed:
            failed = {
                "profile": profile_id,
                "id": "profile_in_use",
                "message": "profile is still bound and was not deleted",
            }
            if profile_id in in_use:
                entry["action"] = "delete_failed"
                errors.append(failed)
            elif not dry_run:
                try:
                    store.delete_profile(profile_id)
                except (ProfileInUse, ProfileNotFound):
                    entry["action"] = "delete_failed"
                    errors.append(failed)
                else:
                    entry["action"] = "deleted"
                    audit(profile_id, {"action": "deleted"})
            else:
                entry["action"] = "deleted"
        left.append(entry)
    return left


def select_variant(provider: Provider, present: set[str], requested: str | None) -> Variant:
    if requested is None:
        matches = [variant for variant in provider.variants if set(variant.fields) == present]
        if len(matches) != 1:
            listing = [
                {"name": variant.name, "fields": list(variant.fields)}
                for variant in provider.variants
            ]
            raise DesiredError(
                "variant_ambiguous",
                "no variant has exactly these fields",
                {"variants": listing},
            )
        return matches[0]
    for variant in provider.variants:
        if variant.name == requested:
            return variant
    raise DesiredError("invalid_argument", "unknown variant")


def check_profile_id(profile_id: str, provider_name: str) -> None:
    prefix, separator, name = profile_id.partition("/")
    if separator != "/" or prefix != provider_name or not _PROFILE_NAME.fullmatch(name):
        raise DesiredError("invalid_argument", "invalid profile id")


def read_private_file(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    file_path = Path(path)
    if not file_path.is_file():
        raise DesiredError("invalid_argument", "file not found")
    mode = file_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise DesiredError("invalid_argument", "file must be mode 600")
    return file_path.read_text(encoding="utf-8")


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise DesiredError("invalid_argument", "invalid env line")
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not name or any(character.isspace() for character in name):
            raise DesiredError("invalid_argument", "invalid env line")
        # Quotes are part of the value. The file is not a shell script.
        values[name] = value
    return values
