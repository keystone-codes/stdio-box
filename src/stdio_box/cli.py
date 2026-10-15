"""Command entry points."""

import sys

from stdio_box.launch import run_install, run_launch
from stdio_box.provider import ProviderError, load_provider
from stdio_box.snapshot import snapshot_main

_USAGE = """usage: stdio-box validate-provider <provider-directory>
       stdio-box launch
       stdio-box install [--provider-dir DIR] [--cache DIR]
       stdio-box snapshot-tools [--provider-dir DIR] [--out PATH|-] [--check] [--env-file PATH]"""

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
        if len(args) != 1:
            print(_USAGE, file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(run_launch())
    if command == "install":
        raise SystemExit(run_install(args[1:]))
    if command == "snapshot-tools":
        raise SystemExit(snapshot_main(args[1:]))
    print(_USAGE, file=sys.stderr)
    raise SystemExit(2)


def credsctl_main(argv: list[str] | None = None) -> None:
    del argv
    print(_CREDSCTL_USAGE, file=sys.stderr)
    raise SystemExit(2)


def _validate_provider(directory: str) -> int:
    try:
        load_provider(directory)
    except ProviderError as exc:
        for message in exc.messages:
            print(message)
        return 1
    return 0
