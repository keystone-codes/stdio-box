"""Box tools of a pinned stdio-box image, for package manifest checks.

A package lists the ``creds_*`` tools of the image it pins, not of this
checkout. A tool added after 0.1.0 is listed here with the version that added it.
"""

import re

from stdio_box.creds_tools import TOOLS

_IMAGE_TAG = re.compile(r"^ghcr\.io/keystone-codes/stdio-box:(?P<tag>[0-9]+\.[0-9]+\.[0-9]+)@")
_ADDED_IN = {"creds_snapshot_tools": (0, 2, 0)}


def box_tools_for(image: str) -> list[str]:
    """``creds_*`` names the image ``image`` serves, in catalog order."""
    match = _IMAGE_TAG.match(image)
    assert match is not None, image
    version = tuple(int(part) for part in match.group("tag").split("."))
    return [tool.name for tool in TOOLS if _ADDED_IN.get(tool.name, (0, 0, 0)) <= version]
