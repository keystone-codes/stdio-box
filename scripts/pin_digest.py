#!/usr/bin/env python3
"""Write a released stdio-box image reference into IMAGE and package manifests.

The manifest edit replaces the reference on the single ``image:`` line.
Every other byte of that file stays as it was. A package that is not
selected keeps the image it already has.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

IMAGE_NAME = "ghcr.io/keystone-codes/stdio-box"
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_REF = re.compile(
    rf"^{re.escape(IMAGE_NAME)}:"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)@sha256:(?P<digest>[0-9a-f]{64})$"
)
_VERSION = re.compile(r"^version:[ \t]+[\"']?(?P<version>[0-9]+\.[0-9]+\.[0-9]+)[\"']?[ \t]*$")
_MANIFEST_NAME = re.compile(r"^name:[ \t]+[\"']?(?P<name>[a-z][a-z0-9_-]{0,30})[\"']?[ \t]*$")
_IMAGE_PREFIX = re.compile(r"^[ \t]*image:[ \t]+")


class PinError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _ImageLine:
    index: int
    prefix: str
    quote: str
    value: str
    suffix: str
    newline: str


@dataclass(frozen=True)
class _Manifest:
    name: str
    version: str
    path: Path
    text: str
    image: _ImageLine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pin_digest.py")
    parser.add_argument("image_ref", help=f"{IMAGE_NAME}:<version>@sha256:<64 hex>")
    parser.add_argument(
        "--packages",
        help="comma-separated package names; default is every stdio-box image",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        pinned = pin(args.image_ref, args.packages)
    except PinError as exc:
        print(f"pin_digest: {exc}", file=sys.stderr)
        return exc.code
    for name, version in pinned:
        print(f"{name} {version}")
    return 0


def pin(image_ref: str, packages: str | None, root: Path | None = None) -> list[tuple[str, str]]:
    """Write ``image_ref`` into IMAGE and the selected manifests.

    Returns ``(name, version)`` for each manifest, sorted by name.
    """
    repo = Path.cwd() if root is None else root
    ref = _parse_ref(image_ref)
    project_version = _project_version(repo)
    if ref.group("version") != project_version:
        raise PinError(
            f"image ref version {ref.group('version')} does not match "
            f"pyproject.toml version {project_version}"
        )
    selected_names = None if packages is None else _parse_package_names(packages)
    manifests = _load_manifests(repo / "packages")
    chosen = _choose(manifests, selected_names)
    for manifest in chosen:
        _replace_image(manifest, image_ref)
    _write_text(repo / "IMAGE", image_ref + "\n")
    return [(manifest.name, manifest.version) for manifest in chosen]


def _parse_ref(image_ref: str) -> re.Match[str]:
    match = _REF.fullmatch(image_ref)
    if match is None:
        raise PinError(
            f"image ref must look like {IMAGE_NAME}:<version>@sha256:<64 hex>",
            code=2,
        )
    return match


def _parse_package_names(value: str) -> list[str]:
    names = [part.strip() for part in value.split(",")]
    if any(name == "" for name in names):
        raise PinError("--packages needs a comma-separated list of names", code=2)
    if len(names) != len(set(names)):
        raise PinError("--packages lists a name more than once", code=2)
    for name in names:
        if _NAME.fullmatch(name) is None:
            raise PinError(f"package name {name!r} is not allowed", code=2)
    return names


def _project_version(repo: Path) -> str:
    path = repo / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PinError(f"{path}: {exc.strerror or exc}") from exc
    try:
        version = tomllib.loads(text)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise PinError(f"{path}: project.version is missing") from exc
    if not isinstance(version, str):
        raise PinError(f"{path}: project.version is missing")
    return version


def _load_manifests(packages: Path) -> dict[str, _Manifest]:
    if not packages.is_dir():
        raise PinError(f"{packages} is not a directory")
    found: dict[str, _Manifest] = {}
    for path in sorted(packages.iterdir()):
        if not path.is_dir():
            continue
        manifest = path / "keystone-service.yaml"
        if not manifest.is_file():
            continue
        found[path.name] = _read_manifest(manifest)
    return found


def _read_text(path: Path) -> str:
    """Read bytes as text without translating newlines."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_text(path: Path, text: str) -> None:
    """Write text without translating newlines."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _read_manifest(path: Path) -> _Manifest:
    try:
        text = _read_text(path)
    except OSError as exc:
        raise PinError(f"{path}: {exc.strerror or exc}") from exc
    lines = text.splitlines(keepends=True)
    image_lines = [
        index for index, line in enumerate(lines) if re.match(r"^[ \t]*image:[ \t]", line)
    ]
    if len(image_lines) != 1:
        raise PinError(f"{path}: expected exactly one image: line, found {len(image_lines)}")
    image = _parse_image_line(lines[image_lines[0]], image_lines[0], path)
    name = path.parent.name
    manifest_name = _field(lines, _MANIFEST_NAME, "name", path)
    if manifest_name != name:
        raise PinError(f"{path}: name {manifest_name} does not match directory {name}")
    version = _field(lines, _VERSION, "version", path)
    return _Manifest(name=name, version=version, path=path, text=text, image=image)


def _parse_image_line(line: str, index: int, path: Path) -> _ImageLine:
    newline = ""
    body = line
    if body.endswith("\r\n"):
        newline = "\r\n"
        body = body[:-2]
    elif body.endswith("\n"):
        newline = "\n"
        body = body[:-1]
    matched = _IMAGE_PREFIX.match(body)
    if matched is None:
        raise PinError(f"{path}: image line is not a single reference")
    prefix = matched.group(0)
    rest = body[matched.end() :]
    quote = ""
    if rest.startswith(("'", '"')):
        quote = rest[0]
        end = rest.find(quote, 1)
        if end < 0:
            raise PinError(f"{path}: image line is not a single reference")
        value = rest[1:end]
        suffix = rest[end + 1 :]
    else:
        token = re.match(r"\S+", rest)
        if token is None:
            raise PinError(f"{path}: image line is not a single reference")
        value = token.group(0)
        suffix = rest[token.end() :]
    if value == "" or any(char.isspace() for char in value):
        raise PinError(f"{path}: image line is not a single reference")
    return _ImageLine(
        index=index,
        prefix=prefix,
        quote=quote,
        value=value,
        suffix=suffix,
        newline=newline,
    )


def _field(lines: list[str], pattern: re.Pattern[str], label: str, path: Path) -> str:
    found = []
    for line in lines:
        match = pattern.fullmatch(line.rstrip("\r\n"))
        if match is not None:
            found.append(match.group(label))
    if len(found) != 1:
        raise PinError(f"{path}: expected exactly one {label}: line, found {len(found)}")
    return found[0]


def _choose(
    manifests: dict[str, _Manifest],
    selected_names: list[str] | None,
) -> list[_Manifest]:
    if selected_names is None:
        chosen = [
            manifest for manifest in manifests.values() if _REF.fullmatch(manifest.image.value)
        ]
        return sorted(chosen, key=lambda manifest: manifest.name)
    missing = [name for name in selected_names if name not in manifests]
    if missing:
        raise PinError(f"unknown package {missing[0]}")
    return [manifests[name] for name in sorted(selected_names)]


def _replace_image(manifest: _Manifest, image_ref: str) -> None:
    lines = manifest.text.splitlines(keepends=True)
    image = manifest.image
    lines[image.index] = (
        f"{image.prefix}{image.quote}{image_ref}{image.quote}{image.suffix}{image.newline}"
    )
    _write_text(manifest.path, "".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
