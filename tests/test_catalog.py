"""Published names from a provider snapshot."""

import json
from pathlib import Path

import pytest
from tests.support.facade import ECHO_DIR

from stdio_box.catalog import CatalogError, build_catalog
from stdio_box.provider import (
    Discovery,
    Provider,
    Server,
    ToolRules,
    Variant,
    load_provider,
)

ECHO_COMMAND = "uvx echo-server==0.0.0"


def _tool(name: str, description: str = "tool") -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    }


def _snapshot(
    names: list[str], *, command: str = ECHO_COMMAND, descriptions: dict[str, str] | None = None
) -> dict:
    descriptions = descriptions or {}
    return {
        "schema_version": 1,
        "server": {"command": command},
        "taken_at": "2026-10-15T00:00:00Z",
        "tools": [_tool(name, descriptions.get(name, name)) for name in names],
    }


def _provider(
    *,
    command: str = ECHO_COMMAND,
    rules: ToolRules | None = None,
    variants: tuple[Variant, ...] | None = None,
) -> Provider:
    if variants is None:
        variants = (
            Variant(
                name="all",
                title=None,
                fields=(),
                tools=None,
                allowed_hosts=None,
                checks=(),
            ),
        )
    return Provider(
        schema_version=1,
        name="echo",
        title="Echo",
        server=Server(command=command, bin="echo-server", lock="requirements.txt", static_env=()),
        fields=(),
        variants=variants,
        tools=rules,
        discovery=Discovery(mode="manual", env=()),
        identity_to_child="none",
        directory=Path("."),
    )


def test_strip_prefix_and_snake_normalize() -> None:
    rules = ToolRules(strip_prefix="API-", normalize="snake", rename=(), hide=())
    catalog = build_catalog(
        _snapshot(["API-post-search"], descriptions={"API-post-search": "search posts"}),
        _provider(rules=rules),
    )
    published = catalog.published_tools()
    assert [tool.name for tool in published] == ["post_search"]
    assert published[0].description == "search posts"
    assert catalog.child_name("post_search") == "API-post-search"


def test_normalize_snake() -> None:
    rules = ToolRules(strip_prefix=None, normalize="snake", rename=(), hide=())
    catalog = build_catalog(_snapshot(["Post-Search"]), _provider(rules=rules))
    assert [tool.name for tool in catalog.published_tools()] == ["post_search"]


def test_hide_and_rename() -> None:
    rules = ToolRules(
        strip_prefix=None,
        normalize=None,
        rename=(("old_name", "new_name"),),
        hide=("spam",),
    )
    catalog = build_catalog(_snapshot(["old_name", "spam", "who"]), _provider(rules=rules))
    assert [tool.name for tool in catalog.published_tools()] == ["new_name", "who"]
    assert catalog.child_name("new_name") == "old_name"
    assert catalog.child_name("spam") is None


def test_duplicate_published_names() -> None:
    rules = ToolRules(strip_prefix="API-", normalize="snake", rename=(), hide=())
    with pytest.raises(CatalogError, match="duplicate published name 'post_search'"):
        build_catalog(_snapshot(["API-post-search", "post_search"]), _provider(rules=rules))


def test_child_name_creds_prefix_is_rejected() -> None:
    with pytest.raises(CatalogError, match="published name 'creds_x' is not allowed"):
        build_catalog(_snapshot(["creds_x"]), _provider())


def test_snapshot_from_another_server_version() -> None:
    snapshot = _snapshot(["who"], command="uvx echo-server==9.9.9")
    with pytest.raises(CatalogError, match="snapshot was taken from another server version"):
        build_catalog(snapshot, _provider())


def test_echo_variant_membership() -> None:
    provider = load_provider(ECHO_DIR)
    snapshot = json.loads((ECHO_DIR / "tools.snapshot.json").read_text(encoding="utf-8"))
    catalog = build_catalog(snapshot, provider)
    assert catalog.in_variant("extra_tool", "token") is False
    assert catalog.in_variant("extra_tool", "full") is True
    assert catalog.in_variant("who", "token") is True
    open_variant = _provider()
    open_catalog = build_catalog(_snapshot(["who"]), open_variant)
    assert open_catalog.in_variant("extra_tool", "all") is True
