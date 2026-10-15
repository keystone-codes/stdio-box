"""Command entry points. Later cards add the remaining subcommands."""

import json
import os
import sys
from pathlib import Path

import uvicorn

from stdio_box.catalog import CatalogError, build_catalog
from stdio_box.config import load_config
from stdio_box.http_app import UnavailableForwarder, build_app
from stdio_box.logs import configure_logging
from stdio_box.provider import ProviderError, load_provider

_USAGE = """usage: stdio-box validate-provider <provider-directory>
       stdio-box launch
       stdio-box serve"""

_CREDSCTL_USAGE = "usage: credsctl <command> [<args>]"


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
    del argv
    print(_CREDSCTL_USAGE, file=sys.stderr)
    raise SystemExit(2)


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
    app = build_app(config, provider, catalog, UnavailableForwarder())
    uvicorn.run(app, host="0.0.0.0", port=config.port)


def _validate_provider(directory: str) -> int:
    try:
        load_provider(directory)
    except ProviderError as exc:
        for message in exc.messages:
            print(message)
        return 1
    return 0
