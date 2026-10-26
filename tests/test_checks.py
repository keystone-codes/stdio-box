"""HTTP credential checks: auth, headers, order, redirects, and identity."""

import socket
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from stdio_box.checks import run_checks
from stdio_box.provider import Check, HttpAuth, HttpCheck, load_provider
from stdio_box.url_policy import UrlPolicy

ECHO_HTTP = Path(__file__).resolve().parent / "data" / "packages" / "echo-http"
TOKEN = "entered-token-value"
ALSO = "second-token-value"
VALUES = {
    "ECHO_URL": "https://echo.example",
    "ECHO_TOKEN": TOKEN,
    "ECHO_ORG": "acme",
    "ECHO_ALSO": ALSO,
}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_host, _port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _provider():
    return load_provider(ECHO_HTTP)


def _one(http: HttpCheck):
    provider = _provider()
    base = next(variant for variant in provider.variants if variant.name == "one")
    return provider, replace(base, checks=(Check(kind="http", http=http),))


def _http(**overrides) -> HttpCheck:
    values = {
        "method": "GET",
        "url": "{ECHO_URL}/who",
        "expect_status": (200,),
        "auth": HttpAuth(kind="bearer", bearer="ECHO_TOKEN"),
        "headers": (),
        "identity": "name",
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return HttpCheck(**values)


@pytest.mark.asyncio
async def test_basic_bearer_and_header_auth() -> None:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"name": "Ada"})

    provider, bearer = _one(_http())
    _, basic = _one(_http(auth=HttpAuth(kind="basic", basic=("ECHO_ORG", "ECHO_TOKEN"))))
    _, scheme = _one(
        _http(
            auth=HttpAuth(
                kind="header",
                header_name="Authorization",
                header_scheme="OAuth",
                header_field="ECHO_TOKEN",
            )
        )
    )
    _, plain = _one(
        _http(auth=HttpAuth(kind="header", header_name="X-Token", header_field="ECHO_TOKEN"))
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(side_effect=capture)
        bearer_result = await run_checks(
            provider, bearer, VALUES, instances=None, config=UrlPolicy()
        )
        basic_result = await run_checks(provider, basic, VALUES, instances=None, config=UrlPolicy())
        scheme_result = await run_checks(
            provider, scheme, VALUES, instances=None, config=UrlPolicy()
        )
        plain_result = await run_checks(provider, plain, VALUES, instances=None, config=UrlPolicy())

    assert bearer_result.ok is True
    assert basic_result.ok is True
    assert scheme_result.ok is True
    assert plain_result.ok is True
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert seen[1].headers["authorization"].startswith("Basic ")
    assert seen[2].headers["authorization"] == f"OAuth {TOKEN}"
    assert seen[3].headers["x-token"] == TOKEN


@pytest.mark.asyncio
async def test_nonsecret_field_is_substituted_into_a_header() -> None:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"name": "Ada"})

    provider = _provider()
    variant = next(item for item in provider.variants if item.name == "one")
    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(side_effect=capture)
        result = await run_checks(provider, variant, VALUES, instances=None, config=UrlPolicy())
    assert result.ok is True
    assert seen[0].headers["x-org-id"] == "acme"
    assert TOKEN not in str(seen[0].url)


@pytest.mark.asyncio
async def test_second_rejected_check_fails_the_variant() -> None:
    provider = _provider()
    variant = next(item for item in provider.variants if item.name == "two")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/also":
            return httpx.Response(403)
        return httpx.Response(200, json={"name": "Ada"})

    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(side_effect=respond)
        result = await run_checks(provider, variant, VALUES, instances=None, config=UrlPolicy())
    assert result.ok is False
    assert "second.example" in result.detail
    assert result.results[0]["ok"] is True
    assert result.results[0]["index"] == 0
    assert result.results[1]["ok"] is False
    assert result.results[1]["http_status"] == 403
    assert result.results[1]["index"] == 1
    assert result.results[1]["target"] == "https://second.example/also"
    assert ALSO not in str(result.results[1]["target"])
    assert TOKEN not in str(result.results[1]["target"])


@pytest.mark.asyncio
async def test_first_unreachable_check_is_inconclusive() -> None:
    provider = _provider()
    variant = next(item for item in provider.variants if item.name == "two")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/who":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"name": "Ada"})

    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(side_effect=respond)
        result = await run_checks(provider, variant, VALUES, instances=None, config=UrlPolicy())
    assert result.ok is None
    assert result.results[0]["ok"] is None
    assert result.results[1]["ok"] is True


@pytest.mark.asyncio
async def test_https_is_required_unless_allowed() -> None:
    provider, variant = _one(_http(url="http://echo.example/who"))
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(200, json={"name": "Ada"}))
        refused = await run_checks(provider, variant, VALUES, instances=None, config=UrlPolicy())
        allowed = await run_checks(
            provider,
            variant,
            VALUES,
            instances=None,
            config=UrlPolicy(allow_http_urls=True),
        )
        assert refused.ok is False
        assert len(mock.calls) == 1
        assert allowed.ok is True


@pytest.mark.asyncio
async def test_a_secret_field_in_the_url_template_never_reaches_the_result() -> None:
    provider, variant = _one(_http(url="{ECHO_URL}/who?token={ECHO_TOKEN}"))
    result = await run_checks(provider, variant, VALUES, instances=None, config=UrlPolicy())
    assert result.ok is False
    target = result.results[0]["target"]
    assert target is not None
    assert TOKEN not in str(target)
    assert "{ECHO_TOKEN}" in str(target)


@pytest.mark.asyncio
async def test_redirect_is_not_followed() -> None:
    provider, variant = _one(_http())
    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        )
        result = await run_checks(provider, variant, VALUES, instances=None, config=UrlPolicy())
        assert result.ok is False
        assert result.results[0]["http_status"] == 302
        assert len(mock.calls) == 1
        assert "evil.example" not in str(mock.calls[0].request.url)


@pytest.mark.asyncio
async def test_identity_comes_from_json() -> None:
    provider, variant = _one(_http())
    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(
            return_value=httpx.Response(
                200, json={"name": "Ada Lovelace", "nested": {"name": "no"}}
            )
        )
        result = await run_checks(provider, variant, VALUES, instances=None, config=UrlPolicy())
    assert result.ok is True
    assert result.identity == "Ada Lovelace"

    long_name = "n" * 121
    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(return_value=httpx.Response(200, json={"name": long_name}))
        too_long = await run_checks(provider, variant, VALUES, instances=None, config=UrlPolicy())
    assert too_long.ok is True
    assert too_long.identity is None
