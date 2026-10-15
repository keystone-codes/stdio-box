"""Shared setup for profile routing and instance tests."""

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from stdio_box._vendor.keystone_identity import KeystoneIdentity
from stdio_box.catalog import Catalog, build_catalog
from stdio_box.forward import StoreForwarder
from stdio_box.instances import InstanceManager
from stdio_box.provider import Provider, load_provider
from stdio_box.spawner import InlineSpawner, allowlist_for
from stdio_box.store.repo import Store

ECHO_DIR = Path(__file__).resolve().parents[1] / "data" / "packages" / "echo"
ECHO_SERVER = Path(__file__).resolve().parent / "echo_server.py"
STORE_KEY = "k" * 32


def load_echo() -> tuple[Provider, Catalog, frozenset[str]]:
    provider = load_provider(ECHO_DIR)
    snapshot = json.loads((ECHO_DIR / "tools.snapshot.json").read_text(encoding="utf-8"))
    catalog = build_catalog(snapshot, provider)
    names = frozenset(
        tool["name"] for tool in snapshot["tools"] if isinstance(tool.get("name"), str)
    )
    return provider, catalog, names


def open_store(tmp_path: Path) -> Store:
    return Store.open(
        tmp_path / "data",
        STORE_KEY,
        field_names=["ECHO_URL", "ECHO_TOKEN", "ECHO_EXTRA"],
    )


def seed(
    store: Store,
    name: str,
    token: str,
    subject: str | None,
    *,
    variant: str = "token",
    extra: str = "extra-value-aaaa",
) -> str:
    profile_id = f"echo/{name}"
    public = {"ECHO_URL": "https://echo.invalid"}
    secrets = {"ECHO_TOKEN": token}
    if variant == "full":
        secrets["ECHO_EXTRA"] = extra
    store.create_profile(profile_id, "echo", variant, public, None, "tester")
    store.put_secrets(profile_id, secrets, "tester", version=1)
    if subject is not None:
        store.bind("echo", subject, profile_id, "tester")
    return profile_id


def person(
    sub: str = "ann@example.com",
    *,
    roles: tuple[str, ...] = ("member",),
    rbac: str = "enforce",
    sub_type: str = "human",
    tool: str = "who",
    jti: str = "jti-1",
) -> KeystoneIdentity:
    return KeystoneIdentity(
        sub=sub,
        sub_type=sub_type,
        email=sub if sub_type == "human" else None,
        roles=roles,
        rbac=rbac,
        auth_method="google" if sub_type == "human" else "api_key",
        interactive=sub_type == "human",
        client_id=None,
        task_id="t",
        mcp_method="tools/call",
        mcp_target=tool,
        jti=jti,
    )


def make_runtime(
    tmp_path: Path,
    store: Store,
    *,
    max_instances: int = 8,
    idle_seconds: float = 600,
    call_timeout: float = 30,
    start_timeout: float = 10,
    argv: list[str] | None = None,
) -> tuple[Provider, Catalog, InstanceManager, StoreForwarder]:
    provider, catalog, names = load_echo()
    if argv is None:
        argv = [sys.executable, str(ECHO_SERVER)]
    spawner = InlineSpawner(allowlist_for(argv[0]), slot_root=str(tmp_path))
    manager = InstanceManager(
        spawner,
        store,
        provider,
        catalog,
        argv,
        snapshot_names=names,
        max_instances=max_instances,
        idle_seconds=idle_seconds,
        start_timeout_seconds=start_timeout,
        slot_root=str(tmp_path),
    )
    forwarder = StoreForwarder(
        store,
        provider,
        catalog,
        manager,
        call_timeout_seconds=call_timeout,
    )
    return provider, catalog, manager, forwarder


def body(result: Any) -> dict[str, Any]:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


def sha8(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]
