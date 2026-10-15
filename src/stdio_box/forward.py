"""Forward one published tool call to the profile's server and record it."""

import json
import logging
import time
from typing import Any

import anyio
import mcp.types as types

from stdio_box._vendor.keystone_identity import KeystoneIdentity
from stdio_box.catalog import Catalog, CatalogError
from stdio_box.instances import InstanceError, InstanceManager
from stdio_box.provider import Provider, Variant
from stdio_box.refusals import TOOL_NOT_IN_PROFILE, Refusal
from stdio_box.resolver import Resolution, resolve
from stdio_box.store.repo import Store

_LOG = logging.getLogger("stdio_box.forward")


class StoreForwarder:
    """Resolve a profile, run the tool on its instance, and write the audit row."""

    def __init__(
        self,
        store: Store,
        provider: Provider,
        catalog: Catalog,
        manager: InstanceManager,
        *,
        call_timeout_seconds: float,
    ) -> None:
        self._store = store
        self._provider = provider
        self._catalog = catalog
        self._manager = manager
        self._call_timeout_seconds = call_timeout_seconds

    async def call(
        self,
        identity: KeystoneIdentity,
        name: str,
        child_name: str,
        arguments: dict[str, Any],
        *,
        identity_token: str | None = None,
    ) -> types.CallToolResult:
        started = time.monotonic()
        resolution = await anyio.to_thread.run_sync(
            lambda: resolve(self._store, self._provider, identity)
        )
        if resolution.refusal is not None or resolution.profile is None:
            refusal = resolution.refusal
            if refusal is None:
                refusal = Refusal(
                    TOOL_NOT_IN_PROFILE,
                    "There is no account for this call.",
                    "Ask an administrator to bind a profile.",
                )
            await self._audit(
                identity,
                name,
                resolution,
                None,
                started,
                outcome="refused",
                code=refusal.code,
                event="refusal",
            )
            return refusal.as_result()
        try:
            allowed = self._catalog.in_variant(child_name, resolution.profile.variant)
        except CatalogError:
            allowed = False
        if not allowed:
            refusal = _tool_refusal(self._provider, resolution, name)
            await self._audit(
                identity,
                name,
                resolution,
                None,
                started,
                outcome="refused",
                code=refusal.code,
                event="refusal",
            )
            return refusal.as_result()
        try:
            instance = await self._manager.ensure_running(resolution.profile)
        except InstanceError as exc:
            await self._audit(
                identity,
                name,
                resolution,
                None,
                started,
                outcome="refused",
                code=exc.refusal.code,
                event="refusal",
            )
            return exc.refusal.as_result()
        meta = None
        if self._provider.identity_to_child == "meta" and identity_token:
            meta = {"keystone-mcp/identity": identity_token}
        try:
            result = await self._manager.invoke(
                instance,
                child_name,
                arguments,
                self._call_timeout_seconds,
                meta,
            )
        except InstanceError as exc:
            await self._audit(
                identity,
                name,
                resolution,
                instance,
                started,
                outcome="refused",
                code=exc.refusal.code,
                event="refusal",
            )
            return exc.refusal.as_result()
        except Exception as exc:
            lost = await self._manager.observe_call_error(instance, exc)
            _LOG.warning("child call failed for %s (%s)", resolution.profile_id, type(exc).__name__)
            await self._audit(
                identity,
                name,
                resolution,
                instance,
                started,
                outcome="error",
                code=None,
                event="call",
            )
            text = "The server process ended the call." if lost else "The server rejected the call."
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=text)],
                isError=True,
            )
        outcome = "error" if result.isError else "ok"
        await self._audit(
            identity,
            name,
            resolution,
            instance,
            started,
            outcome=outcome,
            code=None,
            event="call",
        )
        return result

    async def _audit(
        self,
        identity: KeystoneIdentity,
        tool: str,
        resolution: Resolution,
        instance: object,
        started: float,
        *,
        outcome: str,
        code: str | None,
        event: str,
    ) -> None:
        if outcome == "refused" and code is not None:
            self._manager.note_refusal(code)
        else:
            self._manager.note_call(outcome)
        duration_ms = int((time.monotonic() - started) * 1000)
        slot = getattr(instance, "slot", None)
        generation = getattr(instance, "generation", None)
        binding = None
        if resolution.binding is not None:
            binding = {
                "subject": resolution.binding.subject,
                "position": resolution.binding.position,
            }
        detail = {"binding": binding, "slot": slot, "generation": generation}

        def _write() -> None:
            self._store.record(
                event,
                sub=identity.sub,
                jti=identity.jti,
                tool=tool,
                profile_id=resolution.profile_id,
                tier=resolution.tier,
                outcome=outcome,
                code=code,
                duration_ms=duration_ms,
                detail=detail,
            )

        try:
            await anyio.to_thread.run_sync(_write)
        except Exception:
            _LOG.warning("audit record failed")


def render_whoami(
    provider: Provider,
    identity: KeystoneIdentity,
    resolution: Resolution,
) -> types.CallToolResult:
    """``creds_whoami`` never refuses. ``would_refuse`` carries the code instead."""
    binding = None
    if resolution.binding is not None:
        binding = {
            "subject": resolution.binding.subject,
            "position": resolution.binding.position,
        }
    not_used = [
        {"subject": item.subject, "position": item.position, "profile": item.profile_id}
        for item in resolution.not_used
    ]
    tools = None
    if resolution.profile is not None:
        variant = _variant(provider, resolution.profile.variant)
        if variant is not None and variant.tools is not None:
            tools = list(variant.tools)
    payload: dict[str, Any] = {
        "sub": identity.sub,
        "sub_type": identity.sub_type,
        "roles": list(identity.roles),
        "rbac": identity.rbac,
        "profile": resolution.profile_id,
        "tier": resolution.tier,
        "binding": binding,
        "not_used": not_used,
        "tools": tools,
        "would_refuse": None if resolution.refusal is None else resolution.refusal.code,
        "hint": _hint(resolution),
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload,
        isError=False,
    )


def _hint(resolution: Resolution) -> str | None:
    if not resolution.not_used or resolution.binding is None:
        return None
    skipped = ", ".join(
        f"{item.subject} at position {item.position} ({item.profile_id})"
        for item in resolution.not_used
    )
    first = resolution.not_used[0]
    return (
        f"The binding {resolution.binding.subject} at position {resolution.binding.position} "
        f"was used. {skipped} also matches this caller and was not used because it is lower "
        f"in the order. Move a binding up with creds_bind {first.subject} {first.profile_id} "
        "--replace --position 1."
    )


def _tool_refusal(provider: Provider, resolution: Resolution, published_name: str) -> Refusal:
    variant_name = "" if resolution.profile is None else resolution.profile.variant
    variant = _variant(provider, variant_name)
    title = variant_name if variant is None or not variant.title else variant.title
    profile_id = resolution.profile_id or ""
    return Refusal(
        TOOL_NOT_IN_PROFILE,
        (
            f"The account {profile_id} ({title}) does not include {published_name}. "
            "Ask an administrator for an account that covers it."
        ),
        "Ask an administrator for an account whose variant includes this tool.",
    )


def _variant(provider: Provider, name: str) -> Variant | None:
    for variant in provider.variants:
        if variant.name == name:
            return variant
    return None
