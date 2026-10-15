"""HTTP facade: host check, bearer, MCP, and the open health route."""

import hmac
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from stdio_box import __version__
from stdio_box._vendor.keystone_identity import KeystoneIdentity
from stdio_box.catalog import Catalog
from stdio_box.config import Config
from stdio_box.creds_tools import TOOLS, handler_for
from stdio_box.identity import CallRejected, ReplayCache, verify_call
from stdio_box.logs import RequestLogMiddleware
from stdio_box.provider import Provider
from stdio_box.refusals import INVALID_ARGUMENT, NOT_AVAILABLE, Refusal

_PORT = re.compile(r":\d+\Z")


class Forwarder(Protocol):
    """Runs one published tool on the provider server."""

    async def call(
        self,
        identity: KeystoneIdentity,
        name: str,
        child_name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult: ...


class UnavailableForwarder:
    """Used until a child process can take the call."""

    async def call(
        self,
        _identity: KeystoneIdentity,
        _name: str,
        _child_name: str,
        _arguments: dict[str, Any],
    ) -> types.CallToolResult:
        return Refusal(
            NOT_AVAILABLE,
            "server tool calls are not available yet",
            "The box accepted the call but cannot run the server tool in this build.",
        ).as_result()


def health_details() -> dict[str, str]:
    """Fields shown when the request did not come through the public proxy.

    ``store``, ``isolation``, and ``install`` stay ``unknown`` until those
    checks report. ``unknown`` is not a failure, so ``status`` is ``ok``.
    """
    return {
        "status": "ok",
        "store": "unknown",
        "isolation": "unknown",
        "install": "unknown",
    }


def build_app(
    config: Config,
    provider: Provider,
    catalog: Catalog,
    forwarder: Forwarder,
) -> Starlette:
    """Starlette app for ``/mcp`` and ``/healthz``. ``/creds/`` is not mounted."""
    replay_cache = ReplayCache()
    server = _build_server(config, provider, catalog, forwarder, replay_cache)
    manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)

    class _McpEndpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(
        routes=[
            Route(
                "/mcp",
                endpoint=_McpGate(_McpEndpoint(), config),
                methods=["GET", "POST", "DELETE"],
            ),
            Route("/healthz", endpoint=_healthz, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(RequestLogMiddleware)
    return app


def _build_server(
    config: Config,
    provider: Provider,
    catalog: Catalog,
    forwarder: Forwarder,
    replay_cache: ReplayCache,
) -> Server:
    server: Server = Server("stdio-box", version=__version__)
    if not hasattr(server, "list_tools"):
        # mcp 1.28 registers tools through these decorators. An older constructor
        # that took on_list_tools is not available on this pin.
        raise RuntimeError("MCP server has no list_tools decorator")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return catalog.published_tools() + list(TOOLS)

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            request = server.request_context.request
        except LookupError:
            request = None
        headers = _headers_from_request(request)
        try:
            identity = verify_call(headers, name, config, replay_cache)
        except CallRejected as exc:
            return exc.refusal.as_result()
        handler = handler_for(name)
        if handler is not None:
            return await handler(provider, identity, arguments)
        child = catalog.child_name(name)
        if child is not None:
            return await forwarder.call(identity, name, child, arguments)
        return Refusal(
            INVALID_ARGUMENT,
            "unknown tool",
            "The tool name is not published by this box.",
        ).as_result()

    return server


def _headers_from_request(request: Any) -> dict[str, str]:
    if request is None:
        return {}
    return {key.lower(): value for key, value in request.headers.items()}


class _McpGate:
    """Host check, then bearer, then the MCP session. Both run before MCP parsing."""

    def __init__(self, inner: Any, config: Config) -> None:
        self._inner = inner
        self._config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            host = _header(scope, b"host")
            if not _host_allowed(host, self._config):
                response = JSONResponse({"error": "misdirected_request"}, status_code=421)
                await response(scope, receive, send)
                return
            if not _bearer_ok(_header(scope, b"authorization"), self._config.bearer_token):
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self._inner(scope, receive, send)


def _host_allowed(host_header: str | None, config: Config) -> bool:
    if host_header is None:
        return False
    name = _hostname(host_header)
    if name is None:
        return False
    audience = config.identity_audience.lower()
    allowed = {
        audience,
        f"{audience}-mcp",
        "localhost",
        "127.0.0.1",
        *(item.lower() for item in config.allowed_hosts),
    }
    return name in allowed


def _hostname(host_header: str) -> str | None:
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
        name, _port = value.rsplit(":", 1)
        if not name or not _port.isdigit():
            return None
        return name
    return value


def _bearer_ok(authorization: str | None, expected: str) -> bool:
    expected_b = expected.encode("utf-8")
    if authorization is None:
        hmac.compare_digest(expected_b, expected_b)
        return False
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or token == "" or token != token.strip():
        hmac.compare_digest(expected_b, expected_b)
        return False
    got = token.encode("utf-8")
    if len(got) != len(expected_b):
        hmac.compare_digest(expected_b, expected_b)
        return False
    return hmac.compare_digest(got, expected_b)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _healthz(request: Request) -> JSONResponse:
    # The public proxy sets X-Forwarded-Host. That path learns nothing about
    # the store, isolation, or install state: status code and an empty object.
    if "x-forwarded-host" in request.headers:
        return JSONResponse({})
    return JSONResponse(health_details())
