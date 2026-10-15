"""Command entry points. Later cards add the remaining subcommands."""

import sys

from stdio_box.provider import ProviderError, load_provider

_USAGE = """usage: stdio-box validate-provider <provider-directory>
       stdio-box launch
       stdio-box serve (--spawner inline | --spawner-fd N) --installed JSON
       stdio-box debug-spawn --slot N"""

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
        from stdio_box.launch import main as launch_main

        raise SystemExit(launch_main())
    if command == "serve":
        from stdio_box.serve import main as serve_main

        raise SystemExit(serve_main(args[1:]))
    if command == "debug-spawn":
        from stdio_box.serve import debug_spawn_main

        raise SystemExit(debug_spawn_main(args[1:]))
    print(_USAGE, file=sys.stderr)
    raise SystemExit(2)


def credsctl_main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["status"]:
        from stdio_box.serve import credsctl_status

        raise SystemExit(credsctl_status())
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
