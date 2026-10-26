"""Credential checks declared on a provider variant.

HTTP checks run in order. A tool check starts a one-shot child through
``instances.start_ephemeral``, calls the named tool, and stops that child.
"""

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote, urlsplit

import httpx

from stdio_box.provider import Check, Field, HttpAuth, HttpCheck, Provider, Variant
from stdio_box.url_policy import UrlPolicy, UrlRefused, classify_url, url_policy_from_mapping

_FIELD = re.compile(r"\{([^{}]+)\}")
_DEFAULT_TIMEOUT = 15.0
_MAX_TIMEOUT = 30.0
_IDENTITY_LIMIT = 120
_NO_SLOT = "no free instance slot"


@dataclass(frozen=True)
class CheckResult:
    ok: bool | None
    identity: str | None
    results: list[dict[str, object]]
    detail: str
    duration_ms: int


@dataclass(frozen=True)
class _One:
    kind: str
    ok: bool | None
    http_status: int | None
    identity: str | None
    detail: str
    target: str | None = None


class _TemplateError(Exception):
    """A placeholder could not be filled without putting a secret in the URL."""


def variant_title(variant: Variant) -> str:
    return variant.title or variant.name


async def run_checks(
    provider: Provider,
    variant: Variant,
    values: Mapping[str, str],
    *,
    instances: object = None,
    config: UrlPolicy | None = None,
) -> CheckResult:
    """Run ``variant.checks`` in order.

    ``instances`` is the instance manager. A tool check uses its
    ``start_ephemeral`` method. Without that method, or with no free slot,
    the check is ``ok=None``. ``config`` supplies the URL flags; when it is
    omitted they are read from the process environment.
    """
    policy = url_policy_from_mapping({}) if config is None else config
    started = time.perf_counter()
    title = variant_title(variant)
    fields = {field.name: field for field in provider.fields}
    identity: str | None = None
    saw_none = False
    none_detail = ""
    results: list[dict[str, object]] = []
    for index, check in enumerate(variant.checks):
        one = await _run_check(check, provider, variant, fields, values, policy, title, instances)
        results.append(
            {
                "index": index,
                "kind": one.kind,
                "ok": one.ok,
                "http_status": one.http_status,
                "target": one.target,
            }
        )
        if index == 0:
            identity = one.identity
        if one.ok is False:
            return _result(False, identity, results, one.detail, started)
        if one.ok is None:
            saw_none = True
            if not none_detail:
                none_detail = one.detail
    if saw_none:
        return _result(None, identity, results, none_detail, started)
    return _result(True, identity, results, "", started)


async def _run_check(
    check: Check,
    provider: Provider,
    variant: Variant,
    fields: Mapping[str, Field],
    values: Mapping[str, str],
    policy: UrlPolicy,
    title: str,
    instances: object,
) -> _One:
    if check.kind == "tool" or check.http is None:
        return await _run_tool(check, provider, variant, values, title, instances)
    return await _run_http(check.http, variant, fields, values, policy, title)


async def _run_tool(
    check: Check,
    provider: Provider,
    variant: Variant,
    values: Mapping[str, str],
    title: str,
    instances: object,
) -> _One:
    tool = check.tool
    start = getattr(instances, "start_ephemeral", None)
    if tool is None or start is None:
        return _One("tool", None, None, None, _NO_SLOT, target=None if tool is None else tool.name)
    target = tool.name
    arguments = {} if tool.arguments is None else dict(tool.arguments)
    timeout = _timeout(tool.timeout_seconds)
    try:
        async with start(provider, variant, values) as session:
            if session is None:
                return _One("tool", None, None, None, _NO_SLOT, target=target)
            result = await session.call_tool(
                tool.name,
                arguments,
                read_timeout_seconds=timedelta(seconds=timeout),
            )
    except Exception:
        return _One("tool", None, None, None, _unreachable(title), target=target)
    if result.isError:
        return _One(
            "tool", False, None, None, f"{title} did not accept these values.", target=target
        )
    return _One("tool", True, None, _tool_identity(result), "", target=target)


def _tool_identity(result: object) -> str | None:
    structured = getattr(result, "structuredContent", None)
    if not isinstance(structured, dict):
        return None
    value = structured.get("identity")
    if isinstance(value, str) and value and len(value) <= _IDENTITY_LIMIT:
        return value
    return None


async def _run_http(
    http: HttpCheck,
    variant: Variant,
    fields: Mapping[str, Field],
    values: Mapping[str, str],
    policy: UrlPolicy,
    title: str,
) -> _One:
    try:
        url = _substitute(http.url, fields, values, encode=True)
        headers = {
            name: _substitute(template, fields, values, encode=False)
            for name, template in http.headers
        }
        auth, auth_headers = _auth(http.auth, values)
    except _TemplateError:
        return _One(
            "http", False, None, None, _refused(title, http.url), target=_display_url(http.url)
        )
    display = _display_url(url)
    pinned = classify_url(url, variant, policy)
    if isinstance(pinned, UrlRefused):
        return _One("http", False, None, None, _refused(title, url), target=display)
    headers["Host"] = pinned.host_header
    headers.update(auth_headers)
    timeout = _timeout(http.timeout_seconds)
    request_args: dict[str, object] = {
        "headers": headers,
        "auth": auth,
    }
    if urlsplit(url).scheme == "https":
        request_args["extensions"] = {"sni_hostname": pinned.host}
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout),
            trust_env=False,
        ) as client:
            response = await client.request(http.method, pinned.request_url, **request_args)
    except httpx.HTTPError:
        return _One("http", None, None, None, _unreachable(title), target=display)
    status = response.status_code
    identity = _identity(response.content, http.identity)
    if status not in http.expect_status:
        return _One("http", False, status, identity, _answered(title, url, status), target=display)
    return _One("http", True, status, identity, "", target=display)


def _auth(
    auth: HttpAuth | None, values: Mapping[str, str]
) -> tuple[httpx.Auth | None, dict[str, str]]:
    if auth is None:
        return None, {}
    if auth.kind == "basic":
        if auth.basic is None:
            raise _TemplateError
        user_name, secret_name = auth.basic
        username = _secret_value(values, user_name)
        password = _secret_value(values, secret_name)
        return httpx.BasicAuth(username, password), {}
    if auth.kind == "bearer":
        if auth.bearer is None:
            raise _TemplateError
        token = _secret_value(values, auth.bearer)
        return None, {"Authorization": f"Bearer {token}"}
    if auth.header_name is None or auth.header_field is None:
        raise _TemplateError
    token = _secret_value(values, auth.header_field)
    rendered = token if not auth.header_scheme else f"{auth.header_scheme} {token}"
    return None, {auth.header_name: rendered}


def _secret_value(values: Mapping[str, str], name: str) -> str:
    if name not in values:
        raise _TemplateError
    value = values[name]
    if "\r" in value or "\n" in value:
        raise _TemplateError
    return value


def _substitute(
    template: str,
    fields: Mapping[str, Field],
    values: Mapping[str, str],
    *,
    encode: bool,
) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        field = fields.get(name)
        if field is None or field.secret or name not in values:
            raise _TemplateError
        value = values[name]
        if "\r" in value or "\n" in value:
            raise _TemplateError
        if encode:
            return quote(value, safe=":/")
        return value

    return _FIELD.sub(replace, template)


def _identity(body: bytes, key: str | None) -> str | None:
    if not key:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if not isinstance(value, str) or len(value) > _IDENTITY_LIMIT:
        return None
    return value


def _timeout(seconds: int | None) -> float:
    if seconds is None:
        return _DEFAULT_TIMEOUT
    return float(min(max(seconds, 1), int(_MAX_TIMEOUT)))


def _host_and_path(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{host}{parsed.path}"


def _display_url(url: str) -> str:
    """The URL with only public fields filled in, no query string, no secret can appear here.

    ``url`` is either a filled template (``_substitute`` already refuses to put a secret
    field into a URL) or the raw template string (unfilled placeholders, also no secret
    value). Dropping the query string and fragment is an extra guard in case a public
    field ever carried one.
    """
    parsed = urlsplit(url)
    if not parsed.scheme:
        return url
    return f"{parsed.scheme}://{_host_and_path(url)}"


def _answered(title: str, url: str, status: int) -> str:
    return f"{title} did not accept these values: {_host_and_path(url)} answered HTTP {status}"


def _refused(title: str, url: str) -> str:
    return f"{title} did not accept these values: {_host_and_path(url)} was refused"


def _unreachable(title: str) -> str:
    return f"{title} could not be reached to check them."


def _result(
    ok: bool | None,
    identity: str | None,
    results: list[dict[str, object]],
    detail: str,
    started: float,
) -> CheckResult:
    return CheckResult(
        ok=ok,
        identity=identity,
        results=results,
        detail=detail,
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
    )
