"""Published tool names from a provider snapshot."""

import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import mcp.types as types
from pydantic import ValidationError

from stdio_box.provider import Provider, ToolRules, Variant

_PUBLISHED_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SNAKE = re.compile(r"[^A-Za-z0-9]+")


class CatalogError(Exception):
    """The snapshot cannot be published with this provider."""


@dataclass(frozen=True)
class Catalog:
    _provider: Provider
    _tools: tuple[types.Tool, ...]
    _child_by_published: dict[str, str]

    def published_tools(self) -> list[types.Tool]:
        return list(self._tools)

    def child_name(self, published: str) -> str | None:
        return self._child_by_published.get(published)

    def in_variant(self, child_name: str, variant: str) -> bool:
        """True when ``child_name`` is allowed by that variant's tool patterns.

        Patterns are matched with :func:`fnmatch.fnmatchcase` against the child
        name, not the published name. A variant that omits ``tools`` allows every
        name. Matching is case-sensitive on every platform.
        """
        found = _variant(self._provider, variant)
        if found.tools is None:
            return True
        return any(fnmatch.fnmatchcase(child_name, pattern) for pattern in found.tools)


def build_catalog(snapshot: dict[str, Any], provider: Provider) -> Catalog:
    """Turn a ``tools.snapshot.json`` document into published tools."""
    _check_snapshot_shape(snapshot, provider)
    tools: list[types.Tool] = []
    child_by_published: dict[str, str] = {}
    for index, raw in enumerate(snapshot["tools"]):
        child = _child_tool(index, raw)
        published = _published_name(child.name, provider.tools)
        if published is None:
            continue
        if published in child_by_published:
            raise CatalogError(f"duplicate published name '{published}'")
        child_by_published[published] = child.name
        tools.append(child.model_copy(update={"name": published}))
    return Catalog(
        _provider=provider,
        _tools=tuple(tools),
        _child_by_published=child_by_published,
    )


def _check_snapshot_shape(snapshot: dict[str, Any], provider: Provider) -> None:
    if not isinstance(snapshot, dict):
        raise CatalogError("tools.snapshot.json: document must be an object")
    if snapshot.get("schema_version") != 1:
        raise CatalogError("tools.snapshot.json: schema_version must be 1")
    server = snapshot.get("server")
    if not isinstance(server, dict) or not isinstance(server.get("command"), str):
        raise CatalogError("tools.snapshot.json: server.command is required")
    if server["command"] != provider.server.command:
        raise CatalogError("snapshot was taken from another server version")
    taken_at = snapshot.get("taken_at")
    if not isinstance(taken_at, str):
        raise CatalogError("tools.snapshot.json: taken_at must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(taken_at)
    except ValueError as exc:
        raise CatalogError("tools.snapshot.json: taken_at must be an ISO-8601 timestamp") from exc
    if not isinstance(snapshot.get("tools"), list):
        raise CatalogError("tools.snapshot.json: tools must be a list")


def _child_tool(index: int, raw: Any) -> types.Tool:
    if not isinstance(raw, dict):
        raise CatalogError(f"tools.snapshot.json: tools[{index}] must be an object")
    try:
        return types.Tool.model_validate(raw)
    except ValidationError as exc:
        raise CatalogError(f"tools.snapshot.json: tools[{index}] is not a tool object") from exc


def _published_name(child: str, rules: ToolRules | None) -> str | None:
    """Return the published name, or None when the tool is hidden."""
    hidden: set[str] = set()
    prefix: str | None = None
    normalize: str | None = None
    rename: dict[str, str] = {}
    if rules is not None:
        hidden = set(rules.hide)
        prefix = rules.strip_prefix
        normalize = rules.normalize
        rename = dict(rules.rename)
    if child in hidden:
        return None
    name = child
    if prefix and name.startswith(prefix):
        name = name[len(prefix) :]
    if normalize == "snake":
        name = _SNAKE.sub("_", name).strip("_").lower()
    name = rename.get(name, name)
    if name in hidden:
        return None
    if name.startswith("creds_") or _PUBLISHED_NAME.fullmatch(name) is None:
        raise CatalogError(f"published name '{name}' is not allowed")
    return name


def _variant(provider: Provider, name: str) -> Variant:
    for variant in provider.variants:
        if variant.name == name:
            return variant
    raise CatalogError(f"unknown variant '{name}'")
