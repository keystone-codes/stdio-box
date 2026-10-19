"""Credential tools and the sample policy printed by ``stdio-box policy-example``.

``creds_whoami`` is rendered by the call router. The handlers here are the other
ten tools. The box does not check roles.
"""

import fnmatch
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import anyio
import mcp.types as types

from stdio_box._vendor.keystone_identity import KeystoneIdentity
from stdio_box.catalog import Catalog, CatalogError, build_catalog
from stdio_box.checks import run_checks
from stdio_box.config import Config
from stdio_box.instances import InstanceManager, exhausted_refusal
from stdio_box.provider import Provider, Variant
from stdio_box.refusals import (
    BINDING_EXISTS,
    BINDING_NOT_FOUND,
    CHECK_FAILED,
    FORM_UNAVAILABLE,
    INSTANCE_FAILED,
    INVALID_ARGUMENT,
    MISSING_FIELDS,
    NOT_AVAILABLE,
    PROFILE_EXISTS,
    PROFILE_IN_USE,
    PROFILE_NOT_FOUND,
    SECRET_FIELD_IN_CHAT,
    SECRET_REENTRY_REQUIRED,
    SNAPSHOT_INVALID,
    UNKNOWN_PROVIDER,
    UPSTREAM_TIMEOUT,
    Refusal,
)
from stdio_box.resolver import Resolution, no_credentials_refusal, reentry_refusal, resolve
from stdio_box.runtime import get_install, get_isolation
from stdio_box.snapshot import build_snapshot, listed_tools, render_snapshot, snapshot_sha256
from stdio_box.store.errors import (
    BindingExists,
    BindingNotFound,
    ProfileExists,
    ProfileInUse,
    ProfileNotFound,
    SecretUnreadable,
)
from stdio_box.store.repo import Profile, Store, normalize_subject
from stdio_box.url_policy import UrlPolicy, check_url

_LOG = logging.getLogger("stdio_box.creds")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PENDING_BASE = "https://public-domain-pending.invalid"
_NEXT = (
    "Send this link to the person who owns the account. It works once and expires in 15 minutes."
)
_FORM_UNAVAILABLE = (
    "This box has no public address yet. Enter the secrets on the server "
    "with credsctl profile import."
)
_CHANGING = (
    "creds_profile_create",
    "creds_profile_rotate",
    "creds_profile_delete",
    "creds_bind",
    "creds_unbind",
)

_Handler = Callable[..., Awaitable[types.CallToolResult]]


@dataclass(frozen=True)
class CredsDeps:
    """Store, config, and the running instance manager for one box."""

    config: Config
    store: Store
    manager: InstanceManager
    catalog: Catalog


_EMPTY: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": {}}


def _object(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


_SUBJECT = {"type": "string", "pattern": r"^(user:.+|role:[A-Za-z0-9_.-]+|default)$"}
_PROFILE = {"type": "string", "minLength": 1}
_FIELDS = {"type": "object", "additionalProperties": {"type": "string"}}

TOOLS: list[types.Tool] = [
    types.Tool(
        name="creds_providers",
        description="List the provider installed in this box.",
        inputSchema=_EMPTY,
    ),
    types.Tool(
        name="creds_profiles",
        description="List credential profiles.",
        inputSchema=_object({"provider": {"type": "string", "minLength": 1}}),
    ),
    types.Tool(
        name="creds_bindings",
        description="List bindings in the order they are checked.",
        inputSchema=_EMPTY,
    ),
    types.Tool(
        name="creds_profile_create",
        description="Create a credential profile and, when secrets are required, a one-time form link.",
        inputSchema=_object(
            {
                "name": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,39}$"},
                "variant": {"type": "string", "minLength": 1},
                "fields": _FIELDS,
                "description": {"type": "string", "maxLength": 200},
            },
            ["name"],
        ),
    ),
    types.Tool(
        name="creds_profile_rotate",
        description="Rotate a profile's fields or its secrets.",
        inputSchema=_object(
            {
                "profile": _PROFILE,
                "fields": _FIELDS,
                "replace_secrets": {"type": "boolean", "default": True},
            },
            ["profile"],
        ),
    ),
    types.Tool(
        name="creds_profile_delete",
        description="Delete a credential profile that is not in use.",
        inputSchema=_object({"profile": _PROFILE}, ["profile"]),
    ),
    types.Tool(
        name="creds_bind",
        description="Bind a user, role, or the default subject to a profile.",
        inputSchema=_object(
            {
                "subject": _SUBJECT,
                "profile": _PROFILE,
                "replace": {"type": "boolean", "default": False},
                "position": {"type": "integer", "minimum": 1},
            },
            ["subject", "profile"],
        ),
    ),
    types.Tool(
        name="creds_unbind",
        description="Remove a binding.",
        inputSchema=_object({"subject": _SUBJECT}, ["subject"]),
    ),
    types.Tool(
        name="creds_whoami",
        description="Show which profile this caller would use, and why.",
        inputSchema=_EMPTY,
    ),
    types.Tool(
        name="creds_check",
        description="Check a profile's credentials.",
        inputSchema=_object({"profile": _PROFILE}, ["profile"]),
    ),
    types.Tool(
        name="creds_snapshot_tools",
        description=(
            "Start the provider server once under the caller's own profile, take its "
            "tools/list, and store that snapshot in the box."
        ),
        inputSchema=_EMPTY,
        annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    ),
]


def box_tool_names() -> list[str]:
    """Names of this build's own ``creds_*`` tools, in catalog order."""
    return [tool.name for tool in TOOLS]


def handler_for(name: str) -> _Handler | None:
    return _HANDLERS.get(name)


def render_policy_example(
    provider: Provider,
    catalog: Catalog,
    namespace: str,
    critical: list[str],
) -> str:
    """YAML sample from DESIGN 7.7. ``namespace`` replaces the example name.

    ``creds_snapshot_tools`` is granted to ``<ns>_box_admin`` by ``<ns>_creds_*``
    and is not in ``requires_human_identity``: it only reads, under the caller's
    own profile. The rendered text does not name it, so the committed
    ``policy.example.yaml`` files of released packages stay valid.
    """
    if _NAMESPACE.fullmatch(namespace) is None:
        raise ValueError("namespace is not a policy namespace")
    published = catalog.published_tools()
    by_child = {}
    for tool in published:
        child = catalog.child_name(tool.name)
        if child is not None:
            by_child[child] = tool.name
    read_only = [
        _prefixed(namespace, tool.name) for tool in published if _hint(tool, "readOnlyHint")
    ]
    destructive = [
        _prefixed(namespace, tool.name) for tool in published if _hint(tool, "destructiveHint")
    ]
    every = [_prefixed(namespace, tool.name) for tool in published]
    whoami = _prefixed(namespace, "creds_whoami")
    providers = _prefixed(namespace, "creds_providers")
    changing = [_prefixed(namespace, name) for name in _CHANGING]
    extra = []
    for child in critical:
        published_name = by_child.get(child, child)
        extra.append(_prefixed(namespace, published_name))
    reader = _dedupe([*read_only, whoami, providers])
    writer = _dedupe([*every, whoami, providers])
    critical_tools = _dedupe([*changing, *destructive, *extra])
    title = provider.title
    lines = [
        "# Example only. keystonectl prints this file and never merges it into your policy.",
        f"# The box does not check roles: whoever the policy lets call {namespace}_creds_* manages",
        f"# everyone's accounts in this box. Do not give a non-administrator role {namespace}_* :",
        f"# it also matches {namespace}_creds_*.",
        'version: "2.0.0"',
        "default_deny: true",
        "roles:",
        f"  {namespace}_reader:",
        f"    description: {_quote(f'Read {title} through the {namespace} box.')}",
        f"    allow: {_flow(reader)}",
        f"  {namespace}_writer:",
        f"    description: {_quote(f'Read and change {title} through the {namespace} box.')}",
        f"    allow: {_flow(writer)}",
        f"  {namespace}_box_admin:",
        f"    description: {_quote(f'Manage accounts and bindings in the {namespace} box.')}",
        f"    allow: {_flow([f'{namespace}_creds_*'])}",
        f"critical_tools: {_flow(critical_tools)}",
        "# Only a signed-in person may change accounts; API keys and service accounts may not.",
        f"requires_human_identity: {_flow(changing)}",
    ]
    return "\n".join(lines) + "\n"


async def _providers(
    provider: Provider,
    _identity: KeystoneIdentity,
    _arguments: dict[str, Any],
    _deps: CredsDeps | None = None,
) -> types.CallToolResult:
    fields = {field.name: field for field in provider.fields}
    variants = []
    for variant in provider.variants:
        variants.append(
            {
                "name": variant.name,
                "title": variant.title,
                "fields": [
                    {
                        "name": fields[name].name,
                        "secret": fields[name].secret,
                        "type": fields[name].type,
                        "hint": fields[name].hint,
                    }
                    for name in variant.fields
                ],
                "tools": None if variant.tools is None else list(variant.tools),
            }
        )
    install = get_install()
    installed_key = "unknown"
    if isinstance(install, dict) and isinstance(install.get("key"), str):
        installed_key = install["key"]
    payload = {
        "providers": [
            {
                "name": provider.name,
                "title": provider.title,
                "server": {
                    "command": provider.server.command,
                    "installed_key": installed_key,
                },
                "variants": variants,
                "isolation": get_isolation(),
            }
        ]
    }
    return _ok(payload)


async def _later(
    _provider: Provider,
    _identity: KeystoneIdentity,
    _arguments: dict[str, Any],
    _deps: CredsDeps | None = None,
) -> types.CallToolResult:
    return _not_ready()


async def _profiles(
    provider: Provider,
    _identity: KeystoneIdentity,
    arguments: dict[str, Any],
    deps: CredsDeps | None = None,
) -> types.CallToolResult:
    if deps is None:
        return _not_ready()
    arguments = arguments or {}
    refused = _only_keys(arguments, {"provider"})
    if refused is not None:
        return refused.as_result()
    requested = arguments.get("provider")
    if requested is not None and not isinstance(requested, str):
        return _invalid("provider must be a string.").as_result()
    if isinstance(requested, str) and requested != provider.name:
        return Refusal(
            UNKNOWN_PROVIDER,
            f"This box does not serve provider {requested}.",
            f"This box serves {provider.name}.",
        ).as_result()

    def _load() -> list[tuple[Profile, list[str]]]:
        rows = []
        for profile in deps.store.list_profiles(provider.name):
            subjects = [item.subject for item in deps.store.bindings_for(profile.id)]
            rows.append((profile, subjects))
        return rows

    rows = await anyio.to_thread.run_sync(_load)
    return _ok({"profiles": [_profile_view(deps, profile, subjects) for profile, subjects in rows]})


async def _bindings(
    provider: Provider,
    _identity: KeystoneIdentity,
    arguments: dict[str, Any],
    deps: CredsDeps | None = None,
) -> types.CallToolResult:
    if deps is None:
        return _not_ready()
    arguments = arguments or {}
    refused = _only_keys(arguments, set())
    if refused is not None:
        return refused.as_result()

    def _load() -> dict[str, Any]:
        found = deps.store.bindings(provider.name)
        users = [
            {
                "subject": item.subject,
                "profile": item.profile_id,
                "state": _profile_state(deps.store, item.profile_id),
            }
            for item in found.user
        ]
        roles = [
            {
                "position": item.position,
                "subject": item.subject,
                "profile": item.profile_id,
                "state": _profile_state(deps.store, item.profile_id),
            }
            for item in found.role
        ]
        default = None
        if found.default is not None:
            default = {
                "profile": found.default.profile_id,
                "state": _profile_state(deps.store, found.default.profile_id),
            }
        return {"user": users, "role": roles, "default": default}

    return _ok(await anyio.to_thread.run_sync(_load))


async def _create(
    provider: Provider,
    identity: KeystoneIdentity,
    arguments: dict[str, Any],
    deps: CredsDeps | None = None,
) -> types.CallToolResult:
    if deps is None:
        return _not_ready()
    arguments = arguments or {}
    refused = _only_keys(arguments, {"name", "variant", "fields", "description"})
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_create", None, refused)
    name = arguments.get("name")
    profile_id = None if not isinstance(name, str) else f"{provider.name}/{name}"
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        return await _fail(
            deps,
            identity,
            "creds_profile_create",
            None,
            _invalid("name must match ^[a-z0-9][a-z0-9-]{0,39}$."),
        )
    description, refused = _description(arguments.get("description", None))
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_create", profile_id, refused)
    parsed, refused = _parse_fields(provider, arguments.get("fields"))
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_create", profile_id, refused)
    variant, refused = _choose_variant(provider, arguments.get("variant"))
    if refused is not None or variant is None:
        assert refused is not None
        return await _fail(deps, identity, "creds_profile_create", profile_id, refused)
    refused = _fields_for_variant(variant, parsed)
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_create", profile_id, refused)
    missing = _missing_public(provider, variant, parsed)
    if missing:
        listed = ", ".join(missing)
        return await _fail(
            deps,
            identity,
            "creds_profile_create",
            profile_id,
            Refusal(
                MISSING_FIELDS,
                f"Missing non-secret fields: {listed}.",
                "Pass every non-secret field of the variant.",
            ),
        )
    refused = _check_urls(provider, variant, parsed, _url_policy(deps.config))
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_create", profile_id, refused)
    public = _public_values(provider, variant, parsed)
    try:
        profile = await anyio.to_thread.run_sync(
            lambda: deps.store.create_profile(
                profile_id, provider.name, variant.name, public, description, identity.sub
            )
        )
    except ProfileExists:
        return await _fail(
            deps,
            identity,
            "creds_profile_create",
            profile_id,
            Refusal(
                PROFILE_EXISTS,
                f"Profile {profile_id} already exists.",
                "Choose another name, or rotate the existing profile.",
            ),
        )
    secrets = _secret_names(provider, variant)
    if secrets:
        if _deferred(deps.config):
            return await _fail(
                deps,
                identity,
                "creds_profile_create",
                profile.id,
                Refusal(
                    FORM_UNAVAILABLE,
                    _FORM_UNAVAILABLE,
                    f"credsctl profile import {profile.id} --from-file -",
                ),
            )
        return await _linked(
            deps,
            identity,
            "creds_profile_create",
            provider,
            profile,
            variant,
            "create",
            None,
        )
    result = await run_checks(
        provider,
        variant,
        public,
        instances=deps.manager,
        config=_url_policy(deps.config),
    )
    if result.ok is False:
        await anyio.to_thread.run_sync(
            lambda: deps.store.set_check(
                profile.id, False, result.identity, datetime.now(timezone.utc)
            )
        )
        return await _fail(
            deps, identity, "creds_profile_create", profile.id, _check_failed(result.detail)
        )
    await anyio.to_thread.run_sync(
        lambda: _store_check(
            deps.store, profile, {}, public, identity.sub, result.ok, result.identity
        )
    )
    fresh = await anyio.to_thread.run_sync(lambda: deps.store.get_profile(profile.id))
    shown = profile if fresh is None else fresh
    return await _succeed(
        deps,
        identity,
        "creds_profile_create",
        shown.id,
        {"profile": shown.id, "state": shown.state, "last_check": _last_check(shown)},
    )


async def _rotate(
    provider: Provider,
    identity: KeystoneIdentity,
    arguments: dict[str, Any],
    deps: CredsDeps | None = None,
) -> types.CallToolResult:
    if deps is None:
        return _not_ready()
    arguments = arguments or {}
    refused = _only_keys(arguments, {"profile", "fields", "replace_secrets"})
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_rotate", None, refused)
    profile_id = arguments.get("profile")
    if not isinstance(profile_id, str) or not profile_id:
        return await _fail(
            deps,
            identity,
            "creds_profile_rotate",
            None,
            _invalid("profile is required."),
        )
    replace = _optional_bool(arguments.get("replace_secrets", True))
    if replace is None:
        return await _fail(
            deps,
            identity,
            "creds_profile_rotate",
            profile_id,
            _invalid("replace_secrets must be a boolean."),
        )
    parsed, refused = _parse_fields(provider, arguments.get("fields"))
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_rotate", profile_id, refused)
    profile = await anyio.to_thread.run_sync(lambda: deps.store.get_profile(profile_id))
    if profile is None or profile.provider != provider.name:
        return await _fail(
            deps, identity, "creds_profile_rotate", profile_id, _missing_profile(profile_id)
        )
    variant = _variant(provider, profile.variant)
    if variant is None:
        return await _fail(
            deps,
            identity,
            "creds_profile_rotate",
            profile_id,
            _invalid("The profile variant is not on this provider."),
        )
    refused = _fields_for_variant(variant, parsed)
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_rotate", profile_id, refused)
    changed = _changed_sensitive(provider, variant, profile, parsed)
    if changed and not replace:
        shown = changed[0] if len(changed) == 1 else " and ".join(changed)
        return await _fail(
            deps,
            identity,
            "creds_profile_rotate",
            profile_id,
            Refusal(
                SECRET_REENTRY_REQUIRED,
                (
                    f"Changing {shown} needs the secrets entered again on the form: "
                    "stored secrets are never sent to a new address."
                ),
                "Call creds_profile_rotate with replace_secrets true and send the new link.",
            ),
        )
    refused = _check_urls(provider, variant, parsed, _url_policy(deps.config))
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_rotate", profile_id, refused)
    public = dict(profile.public_fields)
    public.update(_public_values(provider, variant, parsed))
    secrets_needed = bool(_secret_names(provider, variant))
    if replace and secrets_needed:
        if not parsed and arguments.get("fields") is None:
            pending = None
        else:
            pending = public
        if _deferred(deps.config):
            return await _fail(
                deps,
                identity,
                "creds_profile_rotate",
                profile.id,
                Refusal(
                    FORM_UNAVAILABLE,
                    _FORM_UNAVAILABLE,
                    f"credsctl profile import {profile.id} --from-file -",
                ),
            )
        return await _linked(
            deps,
            identity,
            "creds_profile_rotate",
            provider,
            profile,
            variant,
            "rotate",
            pending,
        )
    if not parsed:
        return await _fail(
            deps,
            identity,
            "creds_profile_rotate",
            profile_id,
            _invalid("Pass fields to change, or replace_secrets true to rotate secrets."),
        )
    try:
        stored_secrets = await anyio.to_thread.run_sync(lambda: deps.store.read_secrets(profile.id))
    except SecretUnreadable:
        return await _fail(
            deps, identity, "creds_profile_rotate", profile.id, reentry_refusal(profile)
        )
    values = {**public, **stored_secrets}
    result = await run_checks(
        provider, variant, values, instances=deps.manager, config=_url_policy(deps.config)
    )
    if result.ok is False:
        return await _fail(
            deps, identity, "creds_profile_rotate", profile.id, _check_failed(result.detail)
        )
    await anyio.to_thread.run_sync(
        lambda: _store_check(
            deps.store,
            profile,
            stored_secrets,
            public,
            identity.sub,
            result.ok,
            result.identity,
        )
    )
    deps.manager.reset_failed(profile.id)
    await deps.manager.restart_profile(profile.id)
    fresh = await anyio.to_thread.run_sync(lambda: deps.store.get_profile(profile.id))
    shown = profile if fresh is None else fresh
    return await _succeed(
        deps,
        identity,
        "creds_profile_rotate",
        shown.id,
        {"profile": shown.id, "state": shown.state, "last_check": _last_check(shown)},
    )


async def _delete(
    provider: Provider,
    identity: KeystoneIdentity,
    arguments: dict[str, Any],
    deps: CredsDeps | None = None,
) -> types.CallToolResult:
    if deps is None:
        return _not_ready()
    arguments = arguments or {}
    refused = _only_keys(arguments, {"profile"})
    if refused is not None:
        return await _fail(deps, identity, "creds_profile_delete", None, refused)
    profile_id = arguments.get("profile")
    if not isinstance(profile_id, str) or not profile_id:
        return await _fail(
            deps, identity, "creds_profile_delete", None, _invalid("profile is required.")
        )
    try:
        await anyio.to_thread.run_sync(lambda: deps.store.delete_profile(profile_id))
    except ProfileNotFound:
        return await _fail(
            deps, identity, "creds_profile_delete", profile_id, _missing_profile(profile_id)
        )
    except ProfileInUse as exc:
        listed = ", ".join(exc.subjects)
        return await _fail(
            deps,
            identity,
            "creds_profile_delete",
            profile_id,
            Refusal(
                PROFILE_IN_USE,
                f"{profile_id} is used by {listed}. Unbind them first.",
                "Unbind every subject that uses this profile, then delete it.",
            ),
        )
    await deps.manager.stop_profile(profile_id)
    return await _succeed(
        deps,
        identity,
        "creds_profile_delete",
        profile_id,
        {"profile": profile_id, "deleted": True},
    )


async def _bind(
    provider: Provider,
    identity: KeystoneIdentity,
    arguments: dict[str, Any],
    deps: CredsDeps | None = None,
) -> types.CallToolResult:
    if deps is None:
        return _not_ready()
    arguments = arguments or {}
    refused = _only_keys(arguments, {"subject", "profile", "replace", "position"})
    if refused is not None:
        return await _fail(deps, identity, "creds_bind", None, refused, subject=_subject(arguments))
    raw_subject = arguments.get("subject")
    profile_id = arguments.get("profile")
    if not isinstance(raw_subject, str) or not isinstance(profile_id, str) or not profile_id:
        return await _fail(
            deps,
            identity,
            "creds_bind",
            profile_id if isinstance(profile_id, str) else None,
            _invalid("subject and profile are required."),
            subject=raw_subject if isinstance(raw_subject, str) else None,
        )
    try:
        subject = normalize_subject(raw_subject)
    except ValueError as exc:
        return await _fail(
            deps,
            identity,
            "creds_bind",
            profile_id,
            _invalid(_bind_message(str(exc))),
            subject=raw_subject,
        )
    replace = _optional_bool(arguments.get("replace", False))
    if replace is None:
        return await _fail(
            deps,
            identity,
            "creds_bind",
            profile_id,
            _invalid("replace must be a boolean."),
            subject=subject,
        )
    if "position" in arguments:
        position = _position(arguments.get("position"))
        if position is None:
            return await _fail(
                deps,
                identity,
                "creds_bind",
                profile_id,
                _invalid("position must be an integer of 1 or greater."),
                subject=subject,
            )
    else:
        position = None
    try:
        result = await anyio.to_thread.run_sync(
            lambda: deps.store.bind(
                provider.name,
                subject,
                profile_id,
                identity.sub,
                replace=replace,
                position=position,
            )
        )
    except ProfileNotFound:
        return await _fail(
            deps,
            identity,
            "creds_bind",
            profile_id,
            _missing_profile(profile_id),
            subject=subject,
            position=position,
        )
    except BindingExists as exc:
        return await _fail(
            deps,
            identity,
            "creds_bind",
            profile_id,
            Refusal(
                BINDING_EXISTS,
                f"{subject} already uses {exc.current}. Pass replace: true to change it.",
                "Pass replace true to point the subject at another profile.",
            ),
            subject=subject,
            position=position,
        )
    except ValueError as exc:
        return await _fail(
            deps,
            identity,
            "creds_bind",
            profile_id,
            _invalid(_bind_message(str(exc))),
            subject=subject,
            position=position,
        )
    found = await anyio.to_thread.run_sync(lambda: deps.store.bindings(provider.name))
    payload: dict[str, Any] = {
        "subject": subject,
        "profile": profile_id,
        "replaced": result.replaced,
        "position": result.position,
    }
    if subject.startswith("role:"):
        payload["order"] = [
            {"position": item.position, "subject": item.subject, "profile": item.profile_id}
            for item in found.role
        ]
    return await _succeed(
        deps,
        identity,
        "creds_bind",
        profile_id,
        payload,
        subject=subject,
        position=result.position,
    )


async def _unbind(
    provider: Provider,
    identity: KeystoneIdentity,
    arguments: dict[str, Any],
    deps: CredsDeps | None = None,
) -> types.CallToolResult:
    if deps is None:
        return _not_ready()
    arguments = arguments or {}
    refused = _only_keys(arguments, {"subject"})
    if refused is not None:
        return await _fail(
            deps, identity, "creds_unbind", None, refused, subject=_subject(arguments)
        )
    raw_subject = arguments.get("subject")
    if not isinstance(raw_subject, str):
        return await _fail(deps, identity, "creds_unbind", None, _invalid("subject is required."))
    try:
        subject = normalize_subject(raw_subject)
    except ValueError as exc:
        return await _fail(
            deps,
            identity,
            "creds_unbind",
            None,
            _invalid(_bind_message(str(exc))),
            subject=raw_subject,
        )
    try:
        removed = await anyio.to_thread.run_sync(lambda: deps.store.unbind(provider.name, subject))
    except BindingNotFound as exc:
        return await _fail(
            deps,
            identity,
            "creds_unbind",
            None,
            Refusal(
                BINDING_NOT_FOUND,
                f"There is no binding for {exc.subject}.",
                "List bindings with creds_bindings.",
            ),
            subject=exc.subject,
        )
    return await _succeed(
        deps,
        identity,
        "creds_unbind",
        removed,
        {"subject": subject, "removed_profile": removed},
        subject=subject,
    )


async def _check(
    provider: Provider,
    _identity: KeystoneIdentity,
    arguments: dict[str, Any],
    deps: CredsDeps | None = None,
) -> types.CallToolResult:
    if deps is None:
        return _not_ready()
    arguments = arguments or {}
    refused = _only_keys(arguments, {"profile"})
    if refused is not None:
        return refused.as_result()
    profile_id = arguments.get("profile")
    if not isinstance(profile_id, str) or not profile_id:
        return _invalid("profile is required.").as_result()
    profile = await anyio.to_thread.run_sync(lambda: deps.store.get_profile(profile_id))
    if profile is None or profile.provider != provider.name:
        return _missing_profile(profile_id).as_result()
    if profile.state in {"needs_reentry", "awaiting_secrets"}:
        return reentry_refusal(profile).as_result()
    variant = _variant(provider, profile.variant)
    if variant is None:
        return _invalid("The profile variant is not on this provider.").as_result()
    try:
        secrets = await anyio.to_thread.run_sync(lambda: deps.store.read_secrets(profile.id))
    except SecretUnreadable:
        return reentry_refusal(profile).as_result()
    values = {**profile.public_fields, **secrets}
    result = await run_checks(
        provider, variant, values, instances=deps.manager, config=_url_policy(deps.config)
    )
    await anyio.to_thread.run_sync(
        lambda: deps.store.set_check(
            profile.id, result.ok, result.identity, datetime.now(timezone.utc)
        )
    )
    instance: dict[str, Any] | None = None
    instance_ok = False
    if result.ok is True:
        try:
            inst = await deps.manager.ensure_running(profile)
        except Exception:
            inst = deps.manager.current(profile.id)
            instance_ok = False
        else:
            instance_ok = inst.state == "running"
        if inst is not None:
            instance = {
                "state": inst.state,
                "slot": inst.slot,
                "generation": inst.generation,
                "tools": _listed_tools(inst.tool_names, variant),
            }
    ok: bool | None
    if result.ok is True and instance_ok:
        ok = True
        deps.manager.reset_failed(profile.id)
    elif result.ok is None:
        ok = None
    else:
        ok = False
    return _ok(
        {
            "profile": profile.id,
            "ok": ok,
            "identity": result.identity,
            "checks": [
                {"ok": item.get("ok"), "http_status": item.get("http_status")}
                for item in result.results
            ],
            "instance": instance,
        }
    )


class _SnapshotStopped(Exception):
    def __init__(self, refusal: Refusal) -> None:
        self.refusal = refusal
        super().__init__(refusal.code)


async def _snapshot_tools(
    provider: Provider,
    identity: KeystoneIdentity,
    arguments: dict[str, Any],
    deps: CredsDeps | None = None,
) -> types.CallToolResult:
    """Take ``tools/list`` under the profile a provider call of this caller would use.

    The profile is chosen by the same resolver as a provider call; no argument
    can name another one. The stored JSON is the raw listing, the same document
    ``snapshot-tools`` writes. ``tools`` in the result are the names after the
    provider's ``tools`` rules.
    """
    if deps is None:
        return _not_ready()
    tool = "creds_snapshot_tools"
    arguments = arguments or {}
    refused = _only_keys(arguments, set())
    if refused is not None:
        return refused.as_result()
    resolution = await anyio.to_thread.run_sync(lambda: resolve(deps.store, provider, identity))
    profile = resolution.profile
    if resolution.refusal is not None or profile is None or resolution.binding is None:
        refusal = resolution.refusal or no_credentials_refusal(provider)
        return await _snapshot_audit(deps, identity, tool, resolution, refusal=refusal)
    variant = _variant(provider, profile.variant)
    if variant is None:
        refusal = _invalid("The profile variant is not on this provider.")
        return await _snapshot_audit(deps, identity, tool, resolution, refusal=refusal)
    try:
        secrets = await anyio.to_thread.run_sync(lambda: deps.store.read_secrets(profile.id))
    except SecretUnreadable:
        return await _snapshot_audit(
            deps, identity, tool, resolution, refusal=reentry_refusal(profile)
        )
    values = {**profile.public_fields, **secrets}
    try:
        tools = await _list_under(deps, provider, variant, values, profile.id)
    except _SnapshotStopped as exc:
        return await _snapshot_audit(deps, identity, tool, resolution, refusal=exc.refusal)
    snapshot = build_snapshot(provider, tools)
    try:
        published = [item.name for item in build_catalog(snapshot, provider).published_tools()]
    except CatalogError as exc:
        refusal = Refusal(
            SNAPSHOT_INVALID,
            f"The server's tools cannot be published with this provider: {exc}",
            "Fix the tools rules in provider.yaml, then take the snapshot again.",
        )
        return await _snapshot_audit(deps, identity, tool, resolution, refusal=refusal)
    body = render_snapshot(snapshot)
    digest = snapshot_sha256(snapshot)
    subject = resolution.binding.subject
    snapshot_id = await anyio.to_thread.run_sync(
        lambda: deps.store.put_tool_snapshot(
            body,
            sha256=digest,
            taken_at=snapshot["taken_at"],
            profile=profile.id,
            variant=variant.name,
            subject=subject,
        )
    )
    payload = {
        "id": snapshot_id,
        "sha256": digest,
        "tools": published,
        "profile": profile.id,
        "variant": variant.name,
        "variant_filters_tools": variant.tools is not None,
    }
    return await _snapshot_audit(
        deps, identity, tool, resolution, payload=payload, snapshot_id=snapshot_id
    )


async def _list_under(
    deps: CredsDeps,
    provider: Provider,
    variant: Variant,
    values: Mapping[str, str],
    profile_id: str,
) -> list[dict]:
    if not deps.manager.has_free_slot():
        raise _SnapshotStopped(exhausted_refusal())
    failed = Refusal(
        INSTANCE_FAILED,
        f"The server for account {profile_id} could not list its tools.",
        "Run creds_check on the account, then take the snapshot again.",
    )
    try:
        async with deps.manager.start_ephemeral(provider, variant, values) as session:
            if session is None:
                raise _SnapshotStopped(failed)
            with anyio.fail_after(deps.config.call_timeout_seconds):
                listed = await session.list_tools()
    except _SnapshotStopped:
        raise
    except TimeoutError:
        raise _SnapshotStopped(
            Refusal(
                UPSTREAM_TIMEOUT,
                "The server did not list its tools in time.",
                "Retry, or raise BOX_CALL_TIMEOUT_SECONDS.",
                retryable=True,
            )
        ) from None
    except Exception as exc:
        _LOG.warning("snapshot tools/list failed for %s (%s)", profile_id, type(exc).__name__)
        raise _SnapshotStopped(failed) from None
    return listed_tools(listed)


async def _snapshot_audit(
    deps: CredsDeps,
    identity: KeystoneIdentity,
    tool: str,
    resolution: Resolution,
    *,
    refusal: Refusal | None = None,
    payload: dict[str, Any] | None = None,
    snapshot_id: int | None = None,
) -> types.CallToolResult:
    binding = None
    if resolution.binding is not None:
        binding = {"subject": resolution.binding.subject, "position": resolution.binding.position}
    detail: dict[str, Any] = {"binding": binding, "snapshot": snapshot_id}

    def _write() -> None:
        deps.store.record(
            "call" if refusal is None else "refusal",
            sub=identity.sub,
            jti=identity.jti,
            tool=tool,
            profile_id=resolution.profile_id,
            tier=resolution.tier,
            outcome="ok" if refusal is None else "refused",
            code=None if refusal is None else refusal.code,
            detail=detail,
        )

    try:
        await anyio.to_thread.run_sync(_write)
    except Exception:
        _LOG.warning("snapshot audit failed")
    if refusal is not None:
        return refusal.as_result()
    assert payload is not None
    return _ok(payload)


def _ok(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload,
        isError=False,
    )


def _not_ready() -> types.CallToolResult:
    return Refusal(
        NOT_AVAILABLE,
        "creds tools arrive in a later build",
        "This credentials tool is not in this build.",
    ).as_result()


def _invalid(message: str) -> Refusal:
    return Refusal(INVALID_ARGUMENT, message, "Check the arguments and try again.")


def _missing_profile(profile_id: str) -> Refusal:
    return Refusal(
        PROFILE_NOT_FOUND,
        f"Profile {profile_id} was not found.",
        "Check the profile id with creds_profiles.",
    )


def _check_failed(detail: str) -> Refusal:
    message = detail or "The provider did not accept these values."
    return Refusal(CHECK_FAILED, message, "Correct the values and try again.")


def _only_keys(arguments: Mapping[str, Any], allowed: set[str]) -> Refusal | None:
    if not isinstance(arguments, dict):
        return _invalid("arguments must be an object.")
    for key in arguments:
        if key not in allowed:
            return _invalid(f"Unknown argument {key}.")
    return None


def _description(raw: object) -> tuple[str | None, Refusal | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, str) or len(raw) > 200:
        return None, _invalid("description must be a string of at most 200 characters.")
    return raw, None


def _parse_fields(provider: Provider, raw: object) -> tuple[dict[str, str], Refusal | None]:
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, _invalid("fields must be an object of strings.")
    by_lower = {field.name.lower(): field for field in provider.fields}
    parsed: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            return {}, _invalid("fields must be an object of strings.")
        field = by_lower.get(key.lower())
        if field is not None and field.secret:
            return {}, Refusal(
                SECRET_FIELD_IN_CHAT,
                f"{field.name} is secret. It is entered on the form, never in chat.",
                "Send the person a form link. Do not pass the secret in the tool call.",
            )
        if field is None:
            return {}, _invalid(f"Unknown field {key}.")
        if field.name in parsed:
            return {}, _invalid(f"Duplicate field {field.name}.")
        if not isinstance(value, str):
            return {}, _invalid("fields values must be strings.")
        if "\r" in value or "\n" in value or len(value) > field.max_length:
            return {}, _invalid(f"{field.name} is not an allowed value.")
        parsed[field.name] = value
    return parsed, None


def _choose_variant(provider: Provider, raw: object) -> tuple[Variant | None, Refusal | None]:
    if raw is None:
        if len(provider.variants) == 1:
            return provider.variants[0], None
        return None, _invalid(f"variant is required. Variants: {_variant_list(provider)}.")
    if not isinstance(raw, str):
        return None, _invalid("variant must be a string.")
    found = _variant(provider, raw)
    if found is None:
        return None, _invalid(f"Unknown variant {raw}. Variants: {_variant_list(provider)}.")
    return found, None


def _variant_list(provider: Provider) -> str:
    return ", ".join(
        f"{variant.name} ({variant.title or variant.name})" for variant in provider.variants
    )


def _variant(provider: Provider, name: str) -> Variant | None:
    for variant in provider.variants:
        if variant.name == name:
            return variant
    return None


def _fields_for_variant(variant: Variant, fields: Mapping[str, str]) -> Refusal | None:
    allowed = set(variant.fields)
    for name in fields:
        if name not in allowed:
            return _invalid(f"Unknown field {name}.")
    return None


def _missing_public(provider: Provider, variant: Variant, fields: Mapping[str, str]) -> list[str]:
    known = {field.name: field for field in provider.fields}
    missing = []
    for name in variant.fields:
        field = known[name]
        if field.secret:
            continue
        if name not in fields or fields[name] == "":
            missing.append(name)
    return missing


def _public_values(
    provider: Provider, variant: Variant, fields: Mapping[str, str]
) -> dict[str, str]:
    known = {field.name: field for field in provider.fields}
    return {
        name: value
        for name, value in fields.items()
        if name in variant.fields and not known[name].secret
    }


def _secret_names(provider: Provider, variant: Variant) -> list[str]:
    known = {field.name: field for field in provider.fields}
    return [name for name in variant.fields if known[name].secret]


def _check_urls(
    provider: Provider,
    variant: Variant,
    fields: Mapping[str, str],
    policy: UrlPolicy,
) -> Refusal | None:
    known = {field.name: field for field in provider.fields}
    for name, value in fields.items():
        field = known.get(name)
        if field is None or field.type != "url" or name not in variant.fields:
            continue
        refused = check_url(value, variant, policy)
        if refused is not None:
            return _invalid(f"{name} is not an allowed address ({refused.reason}).")
    return None


def _changed_sensitive(
    provider: Provider,
    variant: Variant,
    profile: Profile,
    fields: Mapping[str, str],
) -> list[str]:
    sensitive = set()
    known = {field.name: field for field in provider.fields}
    for name in variant.fields:
        field = known.get(name)
        if field is not None and field.type == "url":
            sensitive.add(name)
    for check in variant.checks:
        http = check.http
        if http is None:
            continue
        templates = [http.url, *(value for _name, value in http.headers)]
        for template in templates:
            sensitive.update(_PLACEHOLDER.findall(template))
    changed = []
    for name in variant.fields:
        if name not in sensitive or name not in fields:
            continue
        current = profile.public_fields.get(name, "")
        if fields[name] != current:
            changed.append(name)
    return changed


def _url_policy(config: Config) -> UrlPolicy:
    return UrlPolicy(
        allow_http_urls=config.allow_http_urls,
        allow_private_urls=config.allow_private_urls,
        allowed_url_hosts=config.allowed_url_hosts,
    )


def _deferred(config: Config) -> bool:
    return config.public_base_url.rstrip("/") == _PENDING_BASE


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _position(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _subject(arguments: Mapping[str, Any]) -> str | None:
    subject = arguments.get("subject")
    if isinstance(subject, str):
        return subject
    return None


def _bind_message(text: str) -> str:
    if text == "position is only valid for a role binding":
        return "position is only valid for a role binding."
    if text == "position must be 1 or greater":
        return "position must be an integer of 1 or greater."
    if text == "invalid subject":
        return "subject must be user:<email>, user:svc:<name>, role:<role>, or default."
    if text == "profile belongs to another provider":
        return "profile belongs to another provider."
    return "The binding could not be saved."


def _profile_state(store: Store, profile_id: str) -> str | None:
    profile = store.get_profile(profile_id)
    if profile is None:
        return None
    return profile.state


def _last_check(profile: Profile) -> dict[str, Any]:
    return {
        "at": profile.last_check_at,
        "ok": profile.last_check_ok,
        "identity": profile.last_check_identity,
    }


def _profile_view(deps: CredsDeps, profile: Profile, subjects: list[str]) -> dict[str, Any]:
    inst = deps.manager.current(profile.id)
    instance = None
    if inst is not None:
        instance = {
            "state": inst.state,
            "slot": inst.slot,
            "started_at": inst.started_at_iso,
            "last_call_at": inst.last_call_at_iso,
            "restarts": max(0, inst.generation - 1),
        }
    return {
        "id": profile.id,
        "provider": profile.provider,
        "variant": profile.variant,
        "description": profile.description,
        "state": profile.state,
        "public_fields": profile.public_fields,
        "created_at": profile.created_at,
        "created_by": profile.created_by,
        "rotated_at": profile.rotated_at,
        "rotated_by": profile.rotated_by,
        "last_check": _last_check(profile),
        "bindings": subjects,
        "instance": instance,
    }


def _listed_tools(names: tuple[str, ...], variant: Variant) -> int:
    if variant.tools is None:
        return len(names)
    return sum(
        1 for name in names if any(fnmatch.fnmatchcase(name, pattern) for pattern in variant.tools)
    )


def _store_check(
    store: Store,
    profile: Profile,
    secrets: dict[str, str],
    public: dict[str, str],
    by: str,
    ok: bool | None,
    identity: str | None,
) -> None:
    store.put_secrets(
        profile.id,
        secrets,
        by,
        version=profile.version + 1,
        public_fields=public,
    )
    store.set_check(profile.id, ok, identity, datetime.now(timezone.utc))


async def _linked(
    deps: CredsDeps,
    identity: KeystoneIdentity,
    tool: str,
    provider: Provider,
    profile: Profile,
    variant: Variant,
    purpose: str,
    pending: dict[str, str] | None,
) -> types.CallToolResult:
    token = await anyio.to_thread.run_sync(
        lambda: deps.store.create_link(
            profile.id,
            purpose,
            deps.config.form_ttl_seconds,
            identity.sub,
            pending,
        )
    )
    link = await anyio.to_thread.run_sync(lambda: deps.store.peek_link(token))
    payload = {
        "profile": profile.id,
        "state": profile.state,
        "form_url": f"{deps.config.public_base_url.rstrip('/')}/creds/{token}",
        "expires_at": None if link is None else link.expires_at,
        "secret_fields": _secret_names(provider, variant),
        "next": _NEXT,
    }
    return await _succeed(deps, identity, tool, profile.id, payload, token=token)


async def _fail(
    deps: CredsDeps,
    identity: KeystoneIdentity,
    tool: str,
    profile_id: str | None,
    refusal: Refusal,
    *,
    subject: str | None = None,
    position: int | None = None,
) -> types.CallToolResult:
    await _audit(
        deps, identity, tool, profile_id, code=refusal.code, subject=subject, position=position
    )
    return refusal.as_result()


async def _succeed(
    deps: CredsDeps,
    identity: KeystoneIdentity,
    tool: str,
    profile_id: str | None,
    payload: dict[str, Any],
    *,
    subject: str | None = None,
    position: int | None = None,
    token: str | None = None,
) -> types.CallToolResult:
    await _audit(
        deps,
        identity,
        tool,
        profile_id,
        code=None,
        subject=subject,
        position=position,
        token=token,
    )
    return _ok(payload)


async def _audit(
    deps: CredsDeps,
    identity: KeystoneIdentity,
    tool: str,
    profile_id: str | None,
    *,
    code: str | None,
    subject: str | None,
    position: int | None,
    token: str | None = None,
) -> None:
    detail: dict[str, Any] = {"subject": subject, "position": position}
    if token is not None:
        detail["link"] = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]

    def _write() -> None:
        deps.store.record(
            "admin",
            sub=identity.sub,
            jti=identity.jti,
            tool=tool,
            profile_id=profile_id,
            outcome="ok" if code is None else "refused",
            code=code,
            detail=detail,
        )

    try:
        await anyio.to_thread.run_sync(_write)
    except Exception:
        _LOG.warning("admin audit failed")


def _hint(tool: types.Tool, name: str) -> bool:
    annotations = tool.annotations
    if annotations is None:
        return False
    return getattr(annotations, name, None) is True


def _prefixed(namespace: str, name: str) -> str:
    return f"{namespace}_{name}"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    chosen: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        chosen.append(item)
    return chosen


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _flow(items: list[str]) -> str:
    return "[" + ", ".join(_yaml_token(item) for item in items) + "]"


def _yaml_token(item: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_]+", item):
        return item
    return _quote(item)


_HANDLERS: dict[str, _Handler] = {tool.name: _later for tool in TOOLS}
_HANDLERS["creds_providers"] = _providers
_HANDLERS["creds_profiles"] = _profiles
_HANDLERS["creds_bindings"] = _bindings
_HANDLERS["creds_profile_create"] = _create
_HANDLERS["creds_profile_rotate"] = _rotate
_HANDLERS["creds_profile_delete"] = _delete
_HANDLERS["creds_bind"] = _bind
_HANDLERS["creds_unbind"] = _unbind
_HANDLERS["creds_check"] = _check
_HANDLERS["creds_snapshot_tools"] = _snapshot_tools
