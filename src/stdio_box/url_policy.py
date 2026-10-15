"""Which URLs a credential check or a url field may use.

The check resolves the name once and the request is sent to that address.
A later lookup cannot move the connection.
"""

import ipaddress
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from urllib.parse import urlsplit, urlunsplit

from stdio_box.provider import Variant

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class UrlRefused(Exception):
    """The URL is not allowed. ``reason`` is a short label, not a secret."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class UrlPolicy:
    allow_http_urls: bool = False
    allow_private_urls: bool = False
    allowed_url_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class PinnedUrl:
    """A URL that passed policy, plus the address the request must use."""

    original: str
    request_url: str
    host: str
    host_header: str


def url_policy_from_mapping(environ: Mapping[str, str]) -> UrlPolicy:
    raw_hosts = environ.get("BOX_ALLOWED_URL_HOSTS", "")
    hosts = tuple(part.strip() for part in raw_hosts.split(",") if part.strip())
    return UrlPolicy(
        allow_http_urls=_truthy(environ.get("BOX_ALLOW_HTTP_URLS", "")),
        allow_private_urls=_truthy(environ.get("BOX_ALLOW_PRIVATE_URLS", "")),
        allowed_url_hosts=hosts,
    )


def check_url(value: str, variant: Variant, config: UrlPolicy) -> UrlRefused | None:
    """Return ``None`` when ``value`` may be used, otherwise why it was refused."""
    pinned = classify_url(value, variant, config)
    if isinstance(pinned, UrlRefused):
        return pinned
    return None


def classify_url(value: str, variant: Variant, config: UrlPolicy) -> PinnedUrl | UrlRefused:
    """Apply the URL rules and pin the first resolved address."""
    if not value or any(character.isspace() for character in value) or "\\" in value:
        return UrlRefused("host")
    if "#" in value:
        return UrlRefused("fragment")
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        return UrlRefused("userinfo")
    scheme = parsed.scheme.lower()
    if scheme == "http":
        if not config.allow_http_urls:
            return UrlRefused("scheme")
    elif scheme != "https":
        return UrlRefused("scheme")
    host = parsed.hostname
    if not host:
        return UrlRefused("host")
    host = host.rstrip(".").lower()
    if not host:
        return UrlRefused("host")
    if not _host_matches(host, variant.allowed_hosts):
        return UrlRefused("host")
    if not _host_matches(host, config.allowed_url_hosts):
        return UrlRefused("host")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return UrlRefused("unresolved")
    addresses: list[str] = []
    for *_, sockaddr in infos:
        address = str(sockaddr[0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        return UrlRefused("unresolved")
    if not config.allow_private_urls and any(not _is_public(address) for address in addresses):
        return UrlRefused("private")
    return PinnedUrl(
        original=value,
        request_url=_request_url(parsed, addresses[0]),
        host=host,
        host_header=_host_header(host, parsed.port, scheme),
    )


def _truthy(raw: str) -> bool:
    return raw.strip().lower() == "true"


def _host_matches(host: str, patterns: tuple[str, ...] | None) -> bool:
    if not patterns:
        return True
    return any(fnmatchcase(host, pattern.lower()) for pattern in patterns)


def _is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if parsed.version == 4 and parsed in _CGNAT:
        return False
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_multicast
        or parsed.is_unspecified
    )


def _request_url(parsed, address: str) -> str:
    literal = f"[{address}]" if ":" in address else address
    netloc = literal if parsed.port is None else f"{literal}:{parsed.port}"
    path = parsed.path
    return urlunsplit((parsed.scheme, netloc, path, parsed.query, ""))


def _host_header(host: str, port: int | None, scheme: str) -> str:
    if port is None:
        return host
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return host
    return f"{host}:{port}"
