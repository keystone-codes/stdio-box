"""Shared fixtures for the HTTP facade tests."""

import json
import time
import uuid
from pathlib import Path
from typing import Any

import jwt

from stdio_box.catalog import build_catalog
from stdio_box.config import Config
from stdio_box.http_app import UnavailableForwarder, build_app
from stdio_box.provider import load_provider

ECHO_DIR = Path(__file__).resolve().parents[1] / "data" / "packages" / "echo"
TEST_BEARER = "box-test-bearer-token-aaaaaaaaaa"
TEST_SECRET = "box-test-identity-secret-bbbbbbbb"
TEST_STORE_KEY = "box-test-store-key-cccccccccccccc"
TEST_AUDIENCE = "echo"


def make_config(directory: Path = ECHO_DIR, **overrides: Any) -> Config:
    values: dict[str, Any] = {
        "bearer_token": TEST_BEARER,
        "identity_secret": TEST_SECRET,
        "identity_audience": TEST_AUDIENCE,
        "public_base_url": "https://echo.example",
        "store_key": TEST_STORE_KEY,
        "provider": "echo",
        "provider_dir": str(directory),
        "command": "uvx echo-server==0.0.0",
        "port": 8081,
        "data_dir": "/data",
        "log_level": "INFO",
        "allowed_hosts": (),
    }
    values.update(overrides)
    return Config(**values)


def build_echo_app(**config_overrides: Any):
    provider = load_provider(ECHO_DIR)
    snapshot = json.loads((ECHO_DIR / "tools.snapshot.json").read_text(encoding="utf-8"))
    catalog = build_catalog(snapshot, provider)
    config = make_config(**config_overrides)
    return build_app(config, provider, catalog, UnavailableForwarder())


def make_token(
    *,
    secret: str = TEST_SECRET,
    audience: str = TEST_AUDIENCE,
    tool: str,
    sub: str = "ann@example.com",
    sub_type: str = "human",
    roles: list[str] | None = None,
    exp: int | None = None,
    iat: int | None = None,
    jti: str | None = None,
    alg: str = "HS256",
    typ: str = "keystone-identity+jwt",
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "http://localhost:8080",
        "aud": audience,
        "sub": sub,
        "sub_type": sub_type,
        "roles": ["member"] if roles is None else list(roles),
        "rbac": "enforce",
        "auth_method": "google" if sub_type == "human" else "api_key",
        "interactive": sub_type == "human",
        "task_id": "t_test",
        "mcp_method": "tools/call",
        "mcp_target": tool,
        "iat": now if iat is None else iat,
        "exp": now + 300 if exp is None else exp,
        "jti": jti or uuid.uuid4().hex,
    }
    if sub_type == "human":
        claims["email"] = sub
    return jwt.encode(claims, secret, algorithm=alg, headers={"typ": typ})
