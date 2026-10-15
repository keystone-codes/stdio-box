"""tools/list returns the snapshot and the creds tools, and starts no child."""

import asyncio
import json
import os
import subprocess

import pytest
from starlette.testclient import TestClient
from tests.support.facade import ECHO_DIR, TEST_BEARER, build_echo_app, make_token

from stdio_box.creds_tools import TOOLS

SNAPSHOT_NAMES = [
    "who",
    "slow",
    "block",
    "crash",
    "spam",
    "meta",
    "escape",
    "forkbomb",
    "extra_tool",
]


def _forbid(name: str):
    def blocked(*_args, **_kwargs):
        raise AssertionError(f"{name} was called")

    return blocked


def test_tools_list_is_the_snapshot_plus_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", _forbid("Popen"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbid("create_subprocess_exec"))
    monkeypatch.setattr(os, "fork", _forbid("fork"))
    if hasattr(os, "posix_spawn"):
        monkeypatch.setattr(os, "posix_spawn", _forbid("posix_spawn"))

    app = build_echo_app()
    with TestClient(app, base_url="http://echo") as client:
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TEST_BEARER}",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 200
    tools = json.loads(response.text)["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == SNAPSHOT_NAMES + [tool.name for tool in TOOLS]
    assert (ECHO_DIR / "tools.snapshot.json").is_file()


def test_call_routes_creds_and_snapshot_tools() -> None:
    app = build_echo_app()
    with TestClient(app, base_url="http://echo") as client:
        providers = _call(client, "creds_providers", make_token(tool="creds_providers"))
        body = json.loads(providers["content"][0]["text"])
        assert providers.get("isError") is not True
        provider = body["providers"][0]
        assert provider["name"] == "echo"
        assert provider["isolation"] == "unknown"
        assert provider["server"]["installed_key"] == "unknown"
        assert provider["server"]["command"] == "uvx echo-server==0.0.0"

        whoami = _call(client, "creds_whoami", make_token(tool="creds_whoami"))
        assert whoami["isError"] is True
        assert json.loads(whoami["content"][0]["text"])["message"] == (
            "creds tools arrive in a later build"
        )

        who = _call(client, "who", make_token(tool="who"))
        assert who["isError"] is True
        assert json.loads(who["content"][0]["text"])["code"] == "not_available"

        unknown = _call(client, "missing_tool", make_token(tool="missing_tool"))
        assert json.loads(unknown["content"][0]["text"])["message"] == "unknown tool"


def _call(client: TestClient, name: str, token: str) -> dict:
    response = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TEST_BEARER}",
            "X-Keystone-Identity": token,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        },
    )
    assert response.status_code == 200, response.text
    return json.loads(response.text)["result"]
