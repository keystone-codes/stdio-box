"""Command entry points."""

import json
import os
import sys
from pathlib import Path

import uvicorn

from stdio_box.catalog import CatalogError, build_catalog
from stdio_box.config import Config, load_config
from stdio_box.http_app import Forwarder, UnavailableForwarder, build_app
from stdio_box.installed import InstalledServer
from stdio_box.logs import configure_logging
from stdio_box.provider import Provider, ProviderError, load_provider
from stdio_box.snapshot import snapshot_main
from stdio_box.store.errors import StoreSchemaNewer
from stdio_box.store.repo import Store

_USAGE = """usage: stdio-box validate-provider <provider-directory>
       stdio-box launch
       stdio-box serve
       stdio-box serve (--spawner inline | --spawner-fd N) --installed JSON
       stdio-box debug-spawn --slot N
       stdio-box install [--provider-dir DIR] [--cache DIR]
       stdio-box snapshot-tools [--provider-dir DIR] [--out PATH|-] [--check] [--env-file PATH]
       stdio-box policy-example --namespace <ns> [--provider-dir DIR] [--critical <child>...]
       stdio-box box-tools"""


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_USAGE, file=sys.stderr)
        raise SystemExit(2)
    command = args[0]
    if command == "validate-provider":
        if len(args) != 2:
            print(_USAGE, file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(_validate_provider(args[1]))
    if command == "launch":
        if len(args) != 1:
            print(_USAGE, file=sys.stderr)
            raise SystemExit(2)
        from stdio_box.launch import main as launch_main

        raise SystemExit(launch_main())
    if command == "serve":
        if args[1:]:
            from stdio_box.serve import main as serve_main

            raise SystemExit(serve_main(args[1:]))
        serve_http()
        return
    if command == "debug-spawn":
        from stdio_box.serve import debug_spawn_main

        raise SystemExit(debug_spawn_main(args[1:]))
    if command == "install":
        from stdio_box.launch import run_install

        raise SystemExit(run_install(args[1:]))
    if command == "snapshot-tools":
        raise SystemExit(snapshot_main(args[1:]))
    if command == "policy-example":
        raise SystemExit(_policy_example(args[1:]))
    if command == "box-tools":
        if len(args) != 1:
            print(_USAGE, file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(_box_tools())
    print(_USAGE, file=sys.stderr)
    raise SystemExit(2)


def credsctl_main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["status"] and os.environ.get("BOX_DEBUG") == "1":
        from stdio_box.serve import credsctl_status

        raise SystemExit(credsctl_status())
    from stdio_box.credsctl import credsctl_main as run

    run(argv)


def serve_http(spawner: object | None = None, installed: InstalledServer | None = None) -> None:
    """Load config, open the store, and serve ``/mcp`` and ``/healthz``."""
    config = load_config(os.environ)
    configure_logging(config.log_level)
    try:
        provider = load_provider(config.provider_dir)
    except ProviderError as exc:
        for message in exc.messages:
            print(message, file=sys.stderr)
        raise SystemExit(1) from exc
    if provider.name != config.provider:
        print(
            f"provider name '{provider.name}' does not match BOX_PROVIDER",
            file=sys.stderr,
        )
        raise SystemExit(1)
    snapshot_path = Path(config.provider_dir) / "tools.snapshot.json"
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"tools.snapshot.json: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        catalog = build_catalog(snapshot, provider)
    except CatalogError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    store, schema_newer = _open_store(config, provider)
    forwarder, whoami, background, form_stats = _routing(
        config, provider, catalog, snapshot, store, spawner, installed
    )
    creds = None
    if store is not None and form_stats is not None:
        from stdio_box.catalog import Catalog
        from stdio_box.creds_tools import CredsDeps
        from stdio_box.instances import InstanceManager

        if isinstance(form_stats, InstanceManager) and isinstance(catalog, Catalog):
            creds = CredsDeps(config, store, form_stats, catalog)
    app = build_app(
        config,
        provider,
        catalog,
        forwarder,
        store,
        schema_newer=schema_newer,
        whoami=whoami,
        background=background,
        form_stats=form_stats,
        creds=creds,
    )
    try:
        uvicorn.run(app, host="0.0.0.0", port=config.port)
    finally:
        if store is not None:
            store.close()


def _routing(
    config: Config,
    provider: Provider,
    catalog: object,
    snapshot: object,
    store: Store | None,
    spawner: object | None,
    installed: InstalledServer | None,
) -> tuple[Forwarder, object, object, object | None]:
    """Build the call forwarder. A store that did not open keeps the placeholder."""
    if store is None:
        return UnavailableForwarder(), None, None, None
    argv = _child_argv(config, installed)
    child = spawner
    if child is None and argv:
        from stdio_box.spawner import InlineSpawner, allowlist_for

        child = InlineSpawner(allowlist_for(argv[0]))
    if child is None or not argv:
        return UnavailableForwarder(), None, None, None
    from stdio_box.catalog import Catalog
    from stdio_box.forward import StoreForwarder, render_whoami
    from stdio_box.instances import InstanceManager
    from stdio_box.resolver import resolve

    if not isinstance(catalog, Catalog):
        return UnavailableForwarder(), None, None, None
    names = _snapshot_names(snapshot)
    slot_root = getattr(child, "slot_root", "/tmp/box")
    manager = InstanceManager(
        child,
        store,
        provider,
        catalog,
        argv,
        snapshot_names=names,
        max_instances=config.max_instances,
        idle_seconds=config.idle_seconds,
        start_timeout_seconds=config.start_timeout_seconds,
        slot_root=str(slot_root),
    )
    forwarder = StoreForwarder(
        store,
        provider,
        catalog,
        manager,
        call_timeout_seconds=config.call_timeout_seconds,
    )

    async def _whoami(identity: object) -> object:
        import anyio

        from stdio_box._vendor.keystone_identity import KeystoneIdentity

        if not isinstance(identity, KeystoneIdentity):
            raise RuntimeError("whoami requires an identity")

        def _resolve() -> object:
            return render_whoami(provider, identity, resolve(store, provider, identity))

        return await anyio.to_thread.run_sync(_resolve)

    return forwarder, _whoami, manager.run, manager


def _child_argv(config: Config, installed: InstalledServer | None) -> list[str]:
    if installed is not None:
        return [installed.argv0, *installed.args]
    if config.debug and config.child_argv_override:
        import shlex

        return shlex.split(config.child_argv_override)
    return []


def _snapshot_names(snapshot: object) -> frozenset[str]:
    if not isinstance(snapshot, dict):
        return frozenset()
    tools = snapshot.get("tools")
    if not isinstance(tools, list):
        return frozenset()
    names = []
    for item in tools:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return frozenset(names)


def _open_store(config: Config, provider: Provider) -> tuple[Store | None, bool]:
    """Open the profile store once, before the HTTP server accepts requests.

    A database newer than this process logs ``store_schema_newer`` inside the
    migration and is not modified. The server still starts so ``/healthz`` can
    answer 503. A key that does not match is not an error: the store opens with
    ``key_state`` ``key_mismatch``.
    """
    try:
        store = Store.open(
            config.data_dir,
            config.store_key,
            field_names=[field.name for field in provider.fields],
        )
    except StoreSchemaNewer:
        return None, True
    return store, False


def _policy_example(argv: list[str]) -> int:
    """Print the sample policy for one namespace. Exit 2 on usage errors."""
    namespace: str | None = None
    provider_dir: str | None = None
    critical: list[str] = []
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in {"--namespace", "--provider-dir", "--critical"} and index + 1 >= len(argv):
            print(_POLICY_USAGE, file=sys.stderr)
            return 2
        if flag == "--namespace":
            namespace = argv[index + 1]
            index += 2
            continue
        if flag == "--provider-dir":
            provider_dir = argv[index + 1]
            index += 2
            continue
        if flag == "--critical":
            critical.append(argv[index + 1])
            index += 2
            continue
        print(_POLICY_USAGE, file=sys.stderr)
        return 2
    if namespace is None:
        print(_POLICY_USAGE, file=sys.stderr)
        return 2
    directory = provider_dir
    if directory is None:
        from stdio_box.config import ConfigError, load_settings

        try:
            settings = load_settings()
        except ConfigError as exc:
            for message in exc.messages:
                print(f"config error: {message}", file=sys.stderr)
            return 2
        if settings.provider_dir is None:
            print("config error: provider directory is required", file=sys.stderr)
            return 2
        directory = str(settings.provider_dir)
    from stdio_box.creds_tools import render_policy_example

    snapshot_path = Path(directory) / "tools.snapshot.json"
    try:
        provider = load_provider(directory)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        catalog = build_catalog(snapshot, provider)
        text = render_policy_example(provider, catalog, namespace, critical)
    except (
        ProviderError,
        CatalogError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(text, end="")
    return 0


_POLICY_USAGE = (
    "usage: stdio-box policy-example --namespace <ns> [--provider-dir DIR] [--critical <child>...]"
)


def _box_tools() -> int:
    """Print the names of this build's own ``creds_*`` tools as one JSON array."""
    from stdio_box.creds_tools import box_tool_names

    print(json.dumps(box_tool_names()))
    return 0


def _validate_provider(directory: str) -> int:
    try:
        load_provider(directory)
    except ProviderError as exc:
        for message in exc.messages:
            print(message)
        return 1
    return 0
