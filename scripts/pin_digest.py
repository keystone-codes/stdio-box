"""Write the released image reference into IMAGE."""

import sys
import tomllib
from pathlib import Path

IMAGE_NAME = "ghcr.io/keystone-codes/stdio-box"


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: pin_digest.py sha256:<digest>", file=sys.stderr)
        raise SystemExit(2)
    digest = args[0]
    hex_part = digest.removeprefix("sha256:")
    if (
        not digest.startswith("sha256:")
        or len(hex_part) != 64
        or any(c not in "0123456789abcdef" for c in hex_part)
    ):
        print("digest must look like sha256:<64 hex chars>", file=sys.stderr)
        raise SystemExit(2)
    version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    Path("IMAGE").write_text(f"{IMAGE_NAME}:{version}@{digest}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
