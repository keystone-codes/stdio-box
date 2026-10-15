"""Host allow-lists, private addresses, and the pinned request address."""

import socket
from pathlib import Path

import httpx
import pytest
import respx

from stdio_box.checks import run_checks
from stdio_box.provider import Variant, load_provider
from stdio_box.url_policy import UrlPolicy, check_url

ECHO_HTTP = Path(__file__).resolve().parent / "data" / "packages" / "echo-http"


def _variant(patterns: tuple[str, ...] | None) -> Variant:
    return Variant(
        name="cloud",
        title="Cloud",
        fields=("URL",),
        tools=None,
        allowed_hosts=patterns,
        checks=(),
    )


def _dns(address: str):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def fake(_host, _port, *_args, **_kwargs):
        return [(family, socket.SOCK_STREAM, 6, "", (address, 0))]

    return fake


def test_allowed_hosts_and_operator_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns("1.1.1.1"))
    variant = _variant(("*.atlassian.net",))
    policy = UrlPolicy()
    assert check_url("https://example.atlassian.net", variant, policy) is None
    assert check_url("https://example.atlassian.net/wiki", variant, policy) is None
    assert check_url("https://jira.example.com", variant, policy) is not None
    assert check_url("https://user:pw@example.atlassian.net/wiki", variant, policy) is not None
    assert check_url("https://example.atlassian.net/wiki#part", variant, policy) is not None

    narrow = UrlPolicy(allowed_url_hosts=("jira.example.com",))
    assert check_url("https://example.atlassian.net", variant, narrow) is not None
    both = UrlPolicy(allowed_url_hosts=("*.atlassian.net",))
    assert check_url("https://example.atlassian.net/wiki", variant, both) is None


def test_private_addresses_are_refused_unless_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    addresses = {
        "loop.example": "127.0.0.1",
        "lan.example": "10.0.0.5",
        "link.example": "169.254.169.254",
        "ula.example": "fd00::1",
        "cgnat.example": "100.64.0.1",
    }

    def fake(host, _port, *_args, **_kwargs):
        address = addresses[host]
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    variant = _variant(("*.example",))
    closed = UrlPolicy()
    opened = UrlPolicy(allow_private_urls=True)
    for host in addresses:
        url = f"https://{host}/path"
        assert check_url(url, variant, closed) is not None
        assert check_url(url, variant, opened) is None


@pytest.mark.asyncio
async def test_request_uses_the_address_from_the_first_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def flip(_host, _port, *_args, **_kwargs):
        calls["n"] += 1
        address = "1.1.1.1" if calls["n"] == 1 else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", flip)
    provider = load_provider(ECHO_HTTP)
    variant = next(item for item in provider.variants if item.name == "one")
    seen: dict[str, object] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={"name": "Ada"})

    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(side_effect=capture)
        result = await run_checks(
            provider,
            variant,
            {
                "ECHO_URL": "https://echo.example",
                "ECHO_TOKEN": "entered-token-value",
                "ECHO_ORG": "acme",
            },
            instances=None,
            config=UrlPolicy(),
        )
    assert result.ok is True
    assert seen["url"] == "https://1.1.1.1/who"
    assert seen["host"] == "echo.example"
    assert seen["sni"] == "echo.example"
    assert "8.8.8.8" not in str(seen["url"])
