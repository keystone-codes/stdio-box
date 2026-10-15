"""Tool errors returned to the client as one JSON text block."""

import json
from dataclasses import dataclass
from typing import Any

import mcp.types as types

IDENTITY_REJECTED = "identity_rejected"
NO_CREDENTIALS_PROFILE = "no_credentials_profile"
TOOL_NOT_IN_PROFILE = "tool_not_in_profile"
PROFILE_NEEDS_REENTRY = "profile_needs_reentry"
INSTANCE_UNAVAILABLE = "instance_unavailable"
INSTANCE_FAILED = "instance_failed"
INSTANCES_EXHAUSTED = "instances_exhausted"
INSTANCE_BUSY = "instance_busy"
UPSTREAM_TIMEOUT = "upstream_timeout"
NOT_AVAILABLE = "not_available"
INVALID_ARGUMENT = "invalid_argument"
UNKNOWN_PROVIDER = "unknown_provider"
PROFILE_EXISTS = "profile_exists"
PROFILE_NOT_FOUND = "profile_not_found"
PROFILE_IN_USE = "profile_in_use"
BINDING_EXISTS = "binding_exists"
BINDING_NOT_FOUND = "binding_not_found"
SECRET_FIELD_IN_CHAT = "secret_field_in_chat"
SECRET_REENTRY_REQUIRED = "secret_reentry_required"
MISSING_FIELDS = "missing_fields"
FORM_UNAVAILABLE = "form_unavailable"
CHECK_FAILED = "check_failed"


@dataclass(frozen=True)
class Refusal:
    """A failed tool call. ``retryable`` stays on the object and is not serialized."""

    code: str
    message: str
    hint: str
    retryable: bool = False
    data: dict[str, Any] | None = None

    def as_result(self) -> types.CallToolResult:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
        }
        if self.data:
            payload.update(self.data)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            isError=True,
        )
