"""Credential tools. Handlers other than ``creds_providers`` are not connected yet."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types as types

from stdio_box._vendor.keystone_identity import KeystoneIdentity
from stdio_box.provider import Provider
from stdio_box.refusals import NOT_AVAILABLE, Refusal

_Handler = Callable[[Provider, KeystoneIdentity, dict[str, Any]], Awaitable[types.CallToolResult]]

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
]


def handler_for(name: str) -> _Handler | None:
    return _HANDLERS.get(name)


async def _providers(
    provider: Provider,
    _identity: KeystoneIdentity,
    _arguments: dict[str, Any],
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
    payload = {
        "providers": [
            {
                "name": provider.name,
                "title": provider.title,
                "server": {
                    "command": provider.server.command,
                    "installed_key": "unknown",
                },
                "variants": variants,
                "isolation": "unknown",
            }
        ]
    }
    return _ok(payload)


async def _later(
    _provider: Provider,
    _identity: KeystoneIdentity,
    _arguments: dict[str, Any],
) -> types.CallToolResult:
    return Refusal(
        NOT_AVAILABLE,
        "creds tools arrive in a later build",
        "This credentials tool is not in this build.",
    ).as_result()


def _ok(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload,
        isError=False,
    )


_HANDLERS: dict[str, _Handler] = {tool.name: _later for tool in TOOLS}
_HANDLERS["creds_providers"] = _providers
