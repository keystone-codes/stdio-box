"""One-time browser form at ``/creds/<token>``.

The link is the secret. The page also requires a same-site cookie, a CSRF
field, and an Origin that matches ``BOX_PUBLIC_BASE_URL``.
"""

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import partial
from html import escape
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from stdio_box.checks import CheckResult, run_checks, variant_title
from stdio_box.config import Config
from stdio_box.form_page import render_form, render_message, render_page
from stdio_box.provider import Field, Provider, Variant
from stdio_box.store.errors import LinkNotFound
from stdio_box.store.repo import FormLink, Profile, Store
from stdio_box.url_policy import UrlPolicy

_MAX_BODY = 16 * 1024
_POSTS_PER_MINUTE = 60
_WINDOW_SECONDS = 60.0
_PORT = re.compile(r":\d+\Z")
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
_INVALID = "This link is not valid. Ask the administrator for a new one."
_REJECTED = "The form could not be accepted. Open the link again."
_TOO_MANY = "Too many attempts. Ask the administrator for a new link."
_TOO_FAST = "Too many submissions. Wait a minute and try again."


class _Limiter:
    """At most 60 accepted POST bodies per minute for this process."""

    def __init__(self) -> None:
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            cutoff = moment - _WINDOW_SECONDS
            while self._times and self._times[0] <= cutoff:
                self._times.popleft()
            if len(self._times) >= _POSTS_PER_MINUTE:
                return False
            self._times.append(moment)
            return True


class FormStats(Protocol):
    """Counters the instance manager already publishes in its status snapshot."""

    def note_form_issued(self) -> None: ...

    def note_form_submitted(self, outcome: str) -> None: ...

    def reset_failed(self, profile_id: str) -> None: ...


def creds_route(
    config: Config,
    provider: Provider,
    store: object | None,
    form_stats: FormStats | None = None,
) -> Route:
    """``/creds/{token}`` on the facade. No bearer check."""
    creds = _Creds(
        config,
        provider,
        store if isinstance(store, Store) else None,
        form_stats,
    )

    async def endpoint(request: Request) -> Response:
        return await creds(request)

    return Route(
        "/creds/{token}",
        endpoint=endpoint,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )


class _Creds:
    def __init__(
        self,
        config: Config,
        provider: Provider,
        store: Store | None,
        form_stats: FormStats | None,
    ) -> None:
        self._config = config
        self._provider = provider
        self._store = store
        self._form_stats = form_stats
        self._limiter = _Limiter()
        self._fields = {field.name: field for field in provider.fields}

    async def __call__(self, request: Request) -> Response:
        if not _host_allowed(request, self._config):
            return _page("Misdirected request.", 421)
        token = str(request.path_params.get("token", ""))
        if request.method == "GET":
            return await self._get(token)
        if request.method == "POST":
            return await self._post(request, token)
        return _page("Method not allowed.", 405)

    async def _get(self, token: str) -> Response:
        link = await self._peek(token)
        if link is None:
            return _page(_INVALID, 404)
        profile = await self._profile(link.profile_id)
        variant = None if profile is None else _variant(self._provider, profile.variant)
        if profile is None or variant is None or self._store is None:
            return _page(_INVALID, 404)
        nonce = secrets.token_urlsafe(16)
        csrf = await self._csrf(token, nonce)
        body = _form_body(
            self._provider,
            variant,
            profile,
            link,
            token,
            csrf,
            self._fields,
        )
        self._issued()
        return _page(body, 200, nonce=nonce, title=self._provider.title)

    async def _post(self, request: Request, token: str) -> Response:
        link = await self._peek(token)
        if link is None:
            return _page(_INVALID, 404)
        profile = await self._profile(link.profile_id)
        variant = None if profile is None else _variant(self._provider, profile.variant)
        if profile is None or variant is None or self._store is None:
            return _page(_INVALID, 404)
        if not _origin_ok(request, self._config):
            return _page(_REJECTED, 400)
        nonce = request.cookies.get("__Host-box_form", "")
        if not nonce:
            return _page(_REJECTED, 400)
        submitted = await _read_form(request)
        if submitted is None:
            return _page(_REJECTED, 400)
        csrf = submitted.get("csrf", "")
        expected = await self._csrf(token, nonce)
        if not _same(expected, csrf):
            return _page(_REJECTED, 400)
        secrets = _secret_values(submitted, variant, self._fields)
        if secrets is None:
            return _page(_REJECTED, 400)
        if not self._limiter.allow():
            return _page(_TOO_FAST, 429)
        values = _check_values(profile, link, variant, self._fields, secrets)
        policy = UrlPolicy(
            allow_http_urls=self._config.allow_http_urls,
            allow_private_urls=self._config.allow_private_urls,
            allowed_url_hosts=self._config.allowed_url_hosts,
        )
        result = await run_checks(
            self._provider,
            variant,
            values,
            instances=self._form_stats,
            config=policy,
        )
        if result.ok is False:
            return await self._reject(token, link, profile, variant, nonce, result)
        return await self._save(token, link, profile, variant, secrets, result)

    async def _reject(
        self,
        token: str,
        link: FormLink,
        profile: Profile,
        variant: Variant,
        nonce: str,
        result: CheckResult,
    ) -> Response:
        store = self._store
        assert store is not None
        try:
            attempts = await anyio.to_thread.run_sync(store.fail_link, token)
        except LinkNotFound:
            return _page(_INVALID, 404)
        await self._audit(token, link, profile.id, "rejected", attempts)
        self._submitted("rejected")
        if attempts >= 5:
            return _page(_TOO_MANY, 410)
        csrf = await self._csrf(token, nonce)
        notice = _failure_notice(variant, result)
        body = _form_body(
            self._provider,
            variant,
            profile,
            link,
            token,
            csrf,
            self._fields,
            notice=notice,
        )
        return _page(body, 422, title=self._provider.title)

    async def _save(
        self,
        token: str,
        link: FormLink,
        profile: Profile,
        variant: Variant,
        secrets: dict[str, str],
        result: CheckResult,
    ) -> Response:
        store = self._store
        assert store is not None
        public = link.pending_public_fields
        await anyio.to_thread.run_sync(
            partial(
                store.put_secrets,
                profile.id,
                secrets,
                link.created_by,
                version=profile.version + 1,
                public_fields=public,
            )
        )
        await anyio.to_thread.run_sync(
            partial(
                store.set_check,
                profile.id,
                result.ok,
                result.identity,
                datetime.now(timezone.utc),
            )
        )
        await anyio.to_thread.run_sync(store.consume_link, token)
        await self._audit(token, link, profile.id, "saved", link.attempts)
        self._submitted("saved")
        self._cleared(profile.id)
        title = escape(variant_title(variant))
        if result.ok is None:
            text = f"Saved, but {title} could not be reached to check them."
        elif result.identity:
            text = (
                f"Saved. {title} says this account is {escape(result.identity)}. "
                "You can close this page."
            )
        else:
            text = "Saved. You can close this page."
        return _page(text, 200)

    async def _peek(self, token: str) -> FormLink | None:
        if self._store is None:
            return None
        return await anyio.to_thread.run_sync(self._store.peek_link, token)

    async def _profile(self, profile_id: str) -> Profile | None:
        if self._store is None:
            return None
        return await anyio.to_thread.run_sync(self._store.get_profile, profile_id)

    async def _csrf(self, token: str, nonce: str) -> str:
        store = self._store
        assert store is not None
        return await anyio.to_thread.run_sync(partial(store.form_csrf, token, nonce))

    def _issued(self) -> None:
        if self._form_stats is not None:
            self._form_stats.note_form_issued()

    def _submitted(self, outcome: str) -> None:
        if self._form_stats is not None:
            self._form_stats.note_form_submitted(outcome)

    def _cleared(self, profile_id: str) -> None:
        if self._form_stats is not None:
            self._form_stats.reset_failed(profile_id)

    async def _audit(
        self, token: str, link: FormLink, profile_id: str, outcome: str, attempts: int
    ) -> None:
        store = self._store
        assert store is not None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        await anyio.to_thread.run_sync(
            partial(
                store.record,
                "form",
                profile_id=profile_id,
                outcome=outcome,
                detail={"link": digest[:8], "purpose": link.purpose, "attempts": attempts},
            )
        )


def _page(
    text: str, status: int, *, nonce: str | None = None, title: str | None = None
) -> HTMLResponse:
    if text.startswith("<"):
        body = text
        tone = "tone-entry"
        page_title = title or "Credential entry"
    else:
        body = render_message(text)
        tone = "tone-done" if status == 200 else "tone-stop"
        page_title = title or ("Saved" if status == 200 else "Credential entry")
    response = HTMLResponse(
        render_page(body, title=page_title, tone=tone),
        status_code=status,
        headers=dict(_SECURITY_HEADERS),
    )
    if nonce is not None:
        response.set_cookie(
            "__Host-box_form",
            nonce,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
    return response


def _form_body(
    provider: Provider,
    variant: Variant,
    profile: Profile,
    link: FormLink,
    token: str,
    csrf: str,
    fields: Mapping[str, Field],
    *,
    notice: str | None = None,
) -> str:
    shown = _public_values(profile, link, variant, fields)
    readonly: list[tuple[str, str, str]] = []
    secrets: list[tuple[str, str, int]] = []
    for name in variant.fields:
        field = fields[name]
        if field.secret:
            secrets.append((name, field.hint, field.max_length))
        else:
            readonly.append((name, shown.get(name, ""), field.hint))
    return render_form(
        provider_title=provider.title,
        variant_title=variant_title(variant),
        profile_id=profile.id,
        purpose=link.purpose,
        token=token,
        csrf=csrf,
        readonly=readonly,
        secrets=secrets,
        notice=notice,
    )


def _failure_notice(variant: Variant, result: CheckResult) -> str:
    title = variant_title(variant)
    status = next(
        (item["http_status"] for item in result.results if item["ok"] is False),
        None,
    )
    if isinstance(status, int):
        return f"{title} did not accept these values (HTTP {status})."
    return f"{title} did not accept these values."


def _public_values(
    profile: Profile,
    link: FormLink,
    variant: Variant,
    fields: Mapping[str, Field],
) -> dict[str, str]:
    pending = link.pending_public_fields or {}
    shown: dict[str, str] = {}
    for name in variant.fields:
        field = fields[name]
        if field.secret:
            continue
        if name in pending and isinstance(pending[name], str):
            shown[name] = pending[name]
        else:
            current = profile.public_fields.get(name, "")
            shown[name] = current if isinstance(current, str) else ""
    return shown


def _check_values(
    profile: Profile,
    link: FormLink,
    variant: Variant,
    fields: Mapping[str, Field],
    secrets: Mapping[str, str],
) -> dict[str, str]:
    values = _public_values(profile, link, variant, fields)
    values.update(secrets)
    return values


def _secret_values(
    submitted: Mapping[str, str],
    variant: Variant,
    fields: Mapping[str, Field],
) -> dict[str, str] | None:
    secret_names = [name for name in variant.fields if fields[name].secret]
    allowed = set(secret_names) | {"csrf"}
    if set(submitted) != allowed:
        return None
    values: dict[str, str] = {}
    for name in secret_names:
        value = submitted[name]
        field = fields[name]
        if value == "" or len(value) > field.max_length or "\r" in value or "\n" in value:
            return None
        values[name] = value
    return values


async def _read_form(request: Request) -> dict[str, str] | None:
    media = request.headers.get("content-type", "")
    if media.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        return None
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_BODY:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY:
            return None
        chunks.append(chunk)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        return None
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=False)
    seen: dict[str, str] = {}
    for key, value in pairs:
        if key in seen:
            return None
        seen[key] = value
    return seen


def _origin_ok(request: Request, config: Config) -> bool:
    raw = request.headers.get("origin")
    if raw is None or raw.strip().lower() == "null":
        return False
    return _origin(raw) == _origin(config.public_base_url)


def _origin(value: str) -> str | None:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        return None
    host = parsed.hostname.lower()
    port = parsed.port
    if (
        port is None
        or (parsed.scheme == "https" and port == 443)
        or (parsed.scheme == "http" and port == 80)
    ):
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{port}"


def _host_allowed(request: Request, config: Config) -> bool:
    host = _hostname(request.headers.get("host"))
    public = _hostname(urlsplit(config.public_base_url).hostname)
    if host is None or host not in {"127.0.0.1", "localhost", public}:
        return False
    if "x-forwarded-host" not in request.headers:
        return True
    forwarded = _hostname(request.headers.get("x-forwarded-host", "").split(",")[0])
    return forwarded is not None and public is not None and forwarded == public


def _hostname(host_header: str | None) -> str | None:
    if host_header is None:
        return None
    value = host_header.strip().lower()
    if not value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end <= 1:
            return None
        rest = value[end + 1 :]
        if rest and _PORT.fullmatch(rest) is None:
            return None
        return value[1:end]
    if value.count(":") > 1:
        return None
    if ":" in value:
        name, port = value.rsplit(":", 1)
        if not name or not port.isdigit():
            return None
        return name
    return value


def _same(expected: str, got: str) -> bool:
    if len(got) != len(expected):
        hmac.compare_digest(expected, expected)
        return False
    return hmac.compare_digest(expected, got)


def _variant(provider: Provider, name: str) -> Variant | None:
    for variant in provider.variants:
        if variant.name == name:
            return variant
    return None
