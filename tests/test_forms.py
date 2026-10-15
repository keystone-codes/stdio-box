"""The /creds/<token> form: host, origin, cookie, CSRF, and saving."""

import json
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from tests.support.facade import TEST_STORE_KEY, make_config

from stdio_box.catalog import build_catalog
from stdio_box.http_app import UnavailableForwarder, build_app
from stdio_box.provider import load_provider
from stdio_box.store.errors import SecretUnreadable
from stdio_box.store.repo import Store

ECHO_HTTP = Path(__file__).resolve().parent / "data" / "packages" / "echo-http"
ORIGIN = "https://box.example"
TOKEN_VALUE = "entered-token-value"
OLD_VALUE = "previous-token-value"
_SECURITY = {
    "content-security-policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
}


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_host, _port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _provider():
    return load_provider(ECHO_HTTP)


def _open_store(tmp_path: Path, provider, clock: _Clock | None = None) -> Store:
    data = tmp_path / "data"
    data.mkdir()
    return Store.open(
        data,
        TEST_STORE_KEY,
        field_names=[field.name for field in provider.fields],
        clock=clock,
    )


def _app(tmp_path: Path, store: Store, provider, form_stats: object | None = None):
    snapshot = json.loads((ECHO_HTTP / "tools.snapshot.json").read_text(encoding="utf-8"))
    catalog = build_catalog(snapshot, provider)
    config = make_config(
        ECHO_HTTP,
        provider="echo-http",
        public_base_url=ORIGIN,
        data_dir=str(tmp_path / "data"),
        command="uvx echo-server==0.0.0",
    )
    return build_app(
        config, provider, catalog, UnavailableForwarder(), store, form_stats=form_stats
    )


class _FormStats:
    def __init__(self) -> None:
        self.issued = 0
        self.submitted: dict[str, int] = {}
        self.cleared: list[str] = []

    def note_form_issued(self) -> None:
        self.issued += 1

    def note_form_submitted(self, outcome: str) -> None:
        self.submitted[outcome] = self.submitted.get(outcome, 0) + 1

    def reset_failed(self, profile_id: str) -> None:
        self.cleared.append(profile_id)


def _profile(store: Store, provider, *, variant: str = "one", public: dict | None = None) -> str:
    profile_id = f"{provider.name}/main"
    fields = public or {"ECHO_URL": "https://echo.example", "ECHO_ORG": "acme"}
    store.create_profile(profile_id, provider.name, variant, fields, None, "test")
    return profile_id


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=ORIGIN,
    )


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    assert match is not None
    return match.group(1)


def _nonce(response: httpx.Response) -> str:
    header = response.headers["set-cookie"]
    pair = header.split(";", 1)[0]
    name, _, value = pair.partition("=")
    assert name == "__Host-box_form"
    return value


async def _form(client: httpx.AsyncClient, token: str) -> tuple[str, str]:
    response = await client.get(f"/creds/{token}")
    assert response.status_code == 200
    nonce, csrf = _nonce(response), _csrf(response.text)
    client.cookies.clear()
    return nonce, csrf


async def _submit(
    client: httpx.AsyncClient,
    token: str,
    nonce: str,
    csrf: str,
    *,
    fields: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    data = {"csrf": csrf, "ECHO_TOKEN": TOKEN_VALUE}
    if fields is not None:
        data = fields
    sent = {"Origin": ORIGIN, "Cookie": f"__Host-box_form={nonce}"}
    if headers is not None:
        sent = headers
    return await client.post(f"/creds/{token}", data=data, headers=sent)


def _assert_security(response: httpx.Response) -> None:
    for name, value in _SECURITY.items():
        assert response.headers[name] == value


@pytest.mark.asyncio
async def test_get_does_not_consume(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    app = _app(tmp_path, store, provider)
    async with _client(app) as client:
        first = await client.get(f"/creds/{token}")
        second = await client.get(f"/creds/{token}")
    assert first.status_code == 200
    assert second.status_code == 200
    cookie = first.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert "domain=" not in cookie
    assert store.peek_link(token) is not None
    store.close()


@pytest.mark.asyncio
async def test_invalid_links_look_the_same(tmp_path: Path) -> None:
    provider = _provider()
    clock = _Clock()
    store = _open_store(tmp_path, provider, clock)
    profile_id = _profile(store, provider)
    expired = store.create_link(profile_id, "create", 900, "test")
    clock.now += timedelta(seconds=901)
    consumed = store.create_link(profile_id, "create", 900, "test")
    store.consume_link(consumed)
    app = _app(tmp_path, store, provider)
    async with _client(app) as client:
        unknown = await client.get("/creds/not-a-real-token")
        expired_response = await client.get(f"/creds/{expired}")
        consumed_response = await client.get(f"/creds/{consumed}")
    assert unknown.status_code == 404
    assert unknown.text == expired_response.text == consumed_response.text
    assert "This link is not valid. Ask the administrator for a new one." in unknown.text
    store.close()


@pytest.mark.asyncio
async def test_post_requires_origin_cookie_csrf(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    app = _app(tmp_path, store, provider)
    async with _client(app) as client:
        nonce, csrf = await _form(client, token)
        missing_origin = await _submit(
            client,
            token,
            nonce,
            csrf,
            headers={"Cookie": f"__Host-box_form={nonce}"},
        )
        foreign = await _submit(
            client,
            token,
            nonce,
            csrf,
            headers={"Origin": "https://evil.example", "Cookie": f"__Host-box_form={nonce}"},
        )
        no_cookie = await _submit(
            client,
            token,
            nonce,
            csrf,
            headers={"Origin": ORIGIN},
        )
        bad_csrf = await _submit(
            client, token, nonce, csrf[:-1] + ("a" if csrf[-1] != "a" else "b")
        )
    for response in (missing_origin, foreign, no_cookie, bad_csrf):
        assert response.status_code == 400
        assert "The form could not be accepted. Open the link again." in response.text
    with pytest.raises(SecretUnreadable):
        store.read_secrets(profile_id)
    store.close()


@pytest.mark.asyncio
async def test_rejects_unknown_and_nonsecret_fields(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    app = _app(tmp_path, store, provider)
    async with _client(app) as client:
        nonce, csrf = await _form(client, token)
        unknown = await _submit(
            client,
            token,
            nonce,
            csrf,
            fields={"csrf": csrf, "ECHO_TOKEN": TOKEN_VALUE, "OTHER": "x"},
        )
        public = await _submit(
            client,
            token,
            nonce,
            csrf,
            fields={
                "csrf": csrf,
                "ECHO_TOKEN": TOKEN_VALUE,
                "ECHO_URL": "https://echo.example",
            },
        )
    assert unknown.status_code == 400
    assert public.status_code == 400
    with pytest.raises(SecretUnreadable):
        store.read_secrets(profile_id)
    store.close()


@pytest.mark.asyncio
async def test_successful_submit_saves_and_consumes(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    app = _app(tmp_path, store, provider)
    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(return_value=httpx.Response(200, json={"name": "Ada Lovelace"}))
        async with _client(app) as client:
            nonce, csrf = await _form(client, token)
            saved = await _submit(client, token, nonce, csrf)
            again = await client.get(f"/creds/{token}")
    assert saved.status_code == 200
    assert "Ada Lovelace" in saved.text
    assert "You can close this page." in saved.text
    assert again.status_code == 404
    assert store.read_secrets(profile_id) == {"ECHO_TOKEN": TOKEN_VALUE}
    rows = [row for row in store.list_audit() if row["event"] == "form"]
    assert rows[-1]["outcome"] == "saved"
    assert TOKEN_VALUE not in json.dumps(rows)
    store.close()


@pytest.mark.asyncio
async def test_form_counts_on_the_existing_status_counters(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    stats = _FormStats()
    app = _app(tmp_path, store, provider, stats)
    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(return_value=httpx.Response(200, json={"name": "Ada Lovelace"}))
        async with _client(app) as client:
            nonce, csrf = await _form(client, token)
            saved = await _submit(client, token, nonce, csrf)
    assert saved.status_code == 200
    assert stats.issued == 1
    assert stats.submitted == {"saved": 1}
    assert stats.cleared == [profile_id]
    store.close()


@pytest.mark.asyncio
async def test_failed_check_keeps_old_values(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    store.put_secrets(profile_id, {"ECHO_TOKEN": OLD_VALUE}, "test", version=1)
    token = store.create_link(profile_id, "rotate", 900, "test")
    app = _app(tmp_path, store, provider)
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(401))
        async with _client(app) as client:
            nonce, csrf = await _form(client, token)
            statuses = []
            for _ in range(4):
                response = await _submit(client, token, nonce, csrf)
                statuses.append(response.status_code)
                assert "did not accept these values (HTTP 401)." in response.text
                assert store.read_secrets(profile_id) == {"ECHO_TOKEN": OLD_VALUE}
            last = await _submit(client, token, nonce, csrf)
    assert statuses == [422, 422, 422, 422]
    assert last.status_code == 410
    assert "Too many attempts. Ask the administrator for a new link." in last.text
    assert store.read_secrets(profile_id) == {"ECHO_TOKEN": OLD_VALUE}
    assert store.get_profile(profile_id).version == 1
    store.close()


@pytest.mark.asyncio
async def test_unreachable_check_saves(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    app = _app(tmp_path, store, provider)

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(side_effect=down)
        async with _client(app) as client:
            nonce, csrf = await _form(client, token)
            saved = await _submit(client, token, nonce, csrf)
    assert saved.status_code == 200
    assert "could not be reached" in saved.text
    profile = store.get_profile(profile_id)
    assert profile is not None
    assert profile.last_check_ok is None
    assert profile.state == "ready"
    assert store.read_secrets(profile_id) == {"ECHO_TOKEN": TOKEN_VALUE}
    store.close()


@pytest.mark.asyncio
async def test_headers(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    app = _app(tmp_path, store, provider)
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(401))
        async with _client(app) as client:
            ok = await client.get(f"/creds/{token}")
            missing = await client.get("/creds/not-a-real-token")
            nonce, csrf = _nonce(ok), _csrf(ok.text)
            rejected = await _submit(client, token, nonce, csrf)
    assert ok.status_code == 200
    assert missing.status_code == 404
    assert rejected.status_code == 422
    for response in (ok, missing, rejected):
        _assert_security(response)
    store.close()


@pytest.mark.asyncio
async def test_request_as_nginx_sends_it(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    app = _app(tmp_path, store, provider)
    proxy = {
        "Host": "127.0.0.1",
        "X-Forwarded-Host": "box.example",
        "X-Forwarded-Proto": "https",
        "X-Real-IP": "203.0.113.9",
        "X-Forwarded-For": "203.0.113.9",
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.route().mock(return_value=httpx.Response(200, json={"name": "Ada Lovelace"}))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            opened = await client.get(f"/creds/{token}", headers=proxy)
            assert opened.status_code == 200
            saved = await client.post(
                f"/creds/{token}",
                data={"csrf": _csrf(opened.text), "ECHO_TOKEN": TOKEN_VALUE},
                headers={
                    **proxy,
                    "Origin": ORIGIN,
                    "Cookie": f"__Host-box_form={_nonce(opened)}",
                },
            )
    assert saved.status_code == 200
    assert store.read_secrets(profile_id) == {"ECHO_TOKEN": TOKEN_VALUE}
    store.close()


@pytest.mark.asyncio
async def test_host_guard_creds(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    app = _app(tmp_path, store, provider)
    async with _client(app) as client:
        attacker = await client.get(f"/creds/{token}", headers={"Host": "attacker.example"})
        forwarded = await client.get(
            f"/creds/{token}",
            headers={"Host": "127.0.0.1", "X-Forwarded-Host": "attacker.example"},
        )
    assert attacker.status_code == 421
    assert forwarded.status_code == 421
    store.close()


@pytest.mark.asyncio
async def test_origin_null_rejected_and_policy_is_same_origin(tmp_path: Path) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    profile_id = _profile(store, provider)
    token = store.create_link(profile_id, "create", 900, "test")
    app = _app(tmp_path, store, provider)
    async with _client(app) as client:
        opened = await client.get(f"/creds/{token}")
        rejected = await client.post(
            f"/creds/{token}",
            data={"csrf": _csrf(opened.text), "ECHO_TOKEN": TOKEN_VALUE},
            headers={
                "Origin": "null",
                "Cookie": f"__Host-box_form={_nonce(opened)}",
            },
        )
    assert opened.headers["referrer-policy"] == "same-origin"
    assert rejected.status_code == 400
    with pytest.raises(SecretUnreadable):
        store.read_secrets(profile_id)
    store.close()


@pytest.mark.asyncio
async def test_no_secret_in_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    provider = _provider()
    store = _open_store(tmp_path, provider)
    saved_id = _profile(store, provider)
    saved_token = store.create_link(saved_id, "create", 900, "test")
    rejected_id = f"{provider.name}/other"
    store.create_profile(
        rejected_id,
        provider.name,
        "one",
        {"ECHO_URL": "https://echo.example", "ECHO_ORG": "acme"},
        None,
        "test",
    )
    store.put_secrets(rejected_id, {"ECHO_TOKEN": OLD_VALUE}, "test", version=1)
    rejected_token = store.create_link(rejected_id, "rotate", 900, "test")
    app = _app(tmp_path, store, provider)
    caplog.set_level(logging.INFO)

    rejected_value = "rejected-token-value"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == f"Bearer {TOKEN_VALUE}":
            return httpx.Response(200, json={"name": "Ada"})
        return httpx.Response(401)

    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=respond)
        async with _client(app) as client:
            nonce, csrf = await _form(client, saved_token)
            ok = await _submit(client, saved_token, nonce, csrf)
            bad_nonce, bad_csrf = await _form(client, rejected_token)
            bad = await _submit(
                client,
                rejected_token,
                bad_nonce,
                bad_csrf,
                fields={"csrf": bad_csrf, "ECHO_TOKEN": rejected_value},
            )
    assert ok.status_code == 200
    assert bad.status_code == 422
    text = caplog.text + json.dumps(store.list_audit())
    assert TOKEN_VALUE not in text
    assert rejected_value not in text
    assert OLD_VALUE not in text
    paths = [getattr(record, "path", "") for record in caplog.records]
    assert "/creds/***" in paths
    assert all(
        not str(path).startswith("/creds/") or path == "/creds/***" for path in paths if path
    )
    store.close()
