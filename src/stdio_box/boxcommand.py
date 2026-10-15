"""Parse ``BOX_COMMAND`` without invoking a shell or running the command."""

import re
import shlex
from dataclasses import dataclass
from typing import Literal

from packaging.version import InvalidVersion, Version

_NPM_VERSION = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
_PY_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_NPM_NAME = re.compile(r"^(?:@[A-Za-z0-9._-]+/[A-Za-z0-9._-]+|[A-Za-z0-9._-]+)$")
_UNSAFE = (";", "$(")
_NPM_YES = {"-y", "--yes"}


class BoxCommandError(Exception):
    """``BOX_COMMAND`` is not an exact locked invocation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class BoxCommand:
    runner: Literal["uvx", "npx"]
    package: str
    version: str
    args: tuple[str, ...]


def parse_box_command(text: str) -> BoxCommand:
    """Split ``text`` with ``shlex`` and return the runner, package, version, and args."""
    if any(mark in text for mark in _UNSAFE):
        raise BoxCommandError(
            "box_command_invalid",
            "box command contains a shell metacharacter",
        )
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise BoxCommandError("box_command_invalid", "box command could not be parsed") from exc
    if not tokens:
        raise BoxCommandError("box_command_invalid", "box command is empty")
    runner = tokens[0]
    if runner == "uvx":
        return _parse_uvx(tokens[1:])
    if runner == "npx":
        return _parse_npx(tokens[1:])
    raise BoxCommandError(
        "box_command_invalid",
        "box command must start with uvx or npx",
    )


def _parse_uvx(tokens: list[str]) -> BoxCommand:
    if not tokens:
        raise BoxCommandError("box_command_invalid", "uvx package must be name==version")
    if tokens[0].startswith("-"):
        raise BoxCommandError(
            "box_command_invalid",
            f"uvx flag {tokens[0]} is not allowed before the package",
        )
    package, version = _split_python_spec(tokens[0])
    _require_python_version(version)
    if not _PY_NAME.fullmatch(package):
        raise BoxCommandError("box_command_invalid", "uvx package name is not valid")
    return BoxCommand(runner="uvx", package=package, version=version, args=tuple(tokens[1:]))


def _parse_npx(tokens: list[str]) -> BoxCommand:
    index = 0
    while index < len(tokens) and tokens[index] in _NPM_YES:
        index += 1
    if index >= len(tokens):
        raise BoxCommandError("box_command_invalid", "npx package must be name@version")
    if tokens[index].startswith("-"):
        raise BoxCommandError(
            "box_command_invalid",
            f"npx flag {tokens[index]} is not allowed before the package",
        )
    package, version = _split_npm_spec(tokens[index])
    if not _NPM_NAME.fullmatch(package):
        raise BoxCommandError("box_command_invalid", "npx package name is not valid")
    if _NPM_VERSION.fullmatch(version) is None:
        raise BoxCommandError(
            "box_command_invalid",
            f"version {version!r} is not an exact X.Y.Z version",
        )
    return BoxCommand(
        runner="npx", package=package, version=version, args=tuple(tokens[index + 1 :])
    )


def _split_python_spec(token: str) -> tuple[str, str]:
    name, separator, version = token.partition("==")
    if separator != "==" or not name or not version or "==" in version:
        raise BoxCommandError("box_command_invalid", "uvx package must be name==version")
    return name, version


def _split_npm_spec(token: str) -> tuple[str, str]:
    if token.startswith("@"):
        slash = token.find("/")
        at = token.find("@", slash + 1) if slash > 0 else -1
    else:
        at = token.rfind("@")
    if at <= 0 or at == len(token) - 1:
        raise BoxCommandError("box_command_invalid", "npx package must be name@version")
    return token[:at], token[at + 1 :]


def _require_python_version(version: str) -> None:
    if any(mark in version for mark in ("*", "x", "X", "^", "~", ">", "<", ",")):
        raise BoxCommandError(
            "box_command_invalid",
            f"version {version!r} is not an exact version",
        )
    try:
        Version(version)
    except InvalidVersion as exc:
        raise BoxCommandError(
            "box_command_invalid",
            f"version {version!r} is not an exact version",
        ) from exc
