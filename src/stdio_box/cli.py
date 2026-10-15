"""Command entry points. Later cards add the remaining subcommands."""

import json
import os
import sys
from pathlib import Path

import uvicorn

from stdio_box.catalog import CatalogError, build_catalog
from stdio_box.config import Config, load_config
from stdio_box.http_app import UnavailableForwarder, build_app
from stdio_box.logs import configure_logging
from stdio_box.provider import Provider, ProviderError, load_provider
from stdio_box.store.errors import StoreSchemaNewer
from stdio_box.store.repo import Store

_USAGE = """usage: stdio-box validate-provider <provider-directory>
       stdio-box launch
       stdio-box serve"""


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
        print("launch is not implemented yet", file=sys.stderr)
        raise SystemExit(2)
    if command == "serve":
        _serve(args[1:])
        return
    print(_USAGE, file=sys.stderr)
    raise SystemExit(2)


def credsctl_main(argv: list[str] | None = None) -> None:
    from stdio_box.credsctl import credsctl_main as run

    run(argv)


def _serve(args: list[str]) -> None:
    if args:
        print(_USAGE, file=sys.stderr)
        raise SystemExit(2)
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
    app = build_app(
        config,
        provider,
        catalog,
        UnavailableForwarder(),
        store,
        schema_newer=schema_newer,
    )
    try:
        uvicorn.run(app, host="0.0.0.0", port=config.port)
    finally:
        if store is not None:
            store.close()


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


def _validate_provider(directory: str) -> int:
    try:
        load_provider(directory)
    except ProviderError as exc:
        for message in exc.messages:
            print(message)
        return 1
    return 0
