"""Notion box: two profiles, two instances, install cache reuse."""

import json
import subprocess
import time
import uuid
from pathlib import Path
from urllib import error, request

import jwt
import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "stdio-box:dev"
VOLUME = "stdio-box-notion-cache"
BEARER = "b" * 32
SECRET = "s" * 32
STORE_KEY = "k" * 32
AUDIENCE = "notion"
TOKEN_A = "ntn_integration_test_profile_a_01234567890123456789012"
TOKEN_B = "ntn_integration_test_profile_b_01234567890123456789012"
READ_TOOL = "get_self"


def test_two_profiles_and_cache_reuse() -> None:
    subprocess.run(["docker", "volume", "rm", "-f", VOLUME], check=False, capture_output=True)
    subprocess.run(["docker", "volume", "create", VOLUME], check=True, capture_output=True)
    first = ""
    second = ""
    port = _free_port()
    try:
        first = _start(port, fresh_cache=True)
        _wait_healthy(first, port)
        _seed_profiles(first)
        _call_tool_retry(port, "a@example.test", READ_TOOL, jti_prefix="call-a")
        _call_tool_retry(port, "b@example.test", READ_TOOL, jti_prefix="call-b")
        status = _creds_status(first)
        assert status["instances"]["running"] == 2
        logs = subprocess.run(
            ["docker", "logs", first], check=False, capture_output=True, text=True
        )
        combined = logs.stdout + logs.stderr
        assert TOKEN_A not in combined
        assert TOKEN_B not in combined
        _remove(first)
        first = ""

        second = _start(port, fresh_cache=False)
        _wait_healthy(second, port, timeout=120)
        install = _install_from_logs(second)
        assert install.get("from_cache") is True
    finally:
        if first:
            _remove(first)
        if second:
            _remove(second)
        subprocess.run(["docker", "volume", "rm", "-f", VOLUME], check=False, capture_output=True)


def _env(port: int, *, store_volume: str) -> list[str]:
    return [
        "-p",
        f"{port}:8081",
        "-v",
        f"{store_volume}:/data",
        "-v",
        f"{VOLUME}:/cache",
        "-e",
        "BOX_PROVIDER=notion",
        "-e",
        "BOX_COMMAND=npx -y @notionhq/notion-mcp-server@2.5.2",
        "-e",
        f"BOX_BEARER_TOKEN={BEARER}",
        "-e",
        f"BOX_IDENTITY_SECRET={SECRET}",
        "-e",
        f"BOX_IDENTITY_AUDIENCE={AUDIENCE}",
        "-e",
        f"BOX_STORE_KEY={STORE_KEY}",
        "-e",
        "BOX_PUBLIC_BASE_URL=https://notion.example",
    ]


def _start(port: int, *, fresh_cache: bool) -> str:
    name = f"stdio-box-notion-{uuid.uuid4().hex[:8]}"
    store = f"stdio-box-notion-store-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "volume", "create", store], check=True, capture_output=True)
    env = _env(port, store_volume=store)
    result = subprocess.run(
        ["docker", "run", "-d", "--name", name, *env, IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return name


def _remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, text=True)


def _wait_healthy(name: str, port: int, timeout: int = 360) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            req = request.Request(
                f"http://127.0.0.1:{port}/healthz",
                headers={"Host": AUDIENCE},
                method="GET",
            )
            with request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (error.URLError, TimeoutError, ConnectionResetError) as exc:
            last = str(exc)
        inspected = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", name],
            check=False,
            capture_output=True,
            text=True,
        )
        if inspected.stdout.strip() != "running":
            logs = subprocess.run(
                ["docker", "logs", name], check=False, capture_output=True, text=True
            )
            raise AssertionError(f"container stopped: {logs.stdout}\n{logs.stderr}")
        time.sleep(1)
    raise AssertionError(f"healthz never returned 200: {last}")


def _seed_profiles(name: str) -> None:
    for profile_id, token, subject in (
        ("notion/a", TOKEN_A, "user:a@example.test"),
        ("notion/b", TOKEN_B, "user:b@example.test"),
    ):
        env_text = f"NOTION_TOKEN={token}\n"
        imported = _exec(
            name,
            [
                "credsctl",
                "profile",
                "import",
                profile_id,
                "--no-check",
                "--from-file",
                "-",
            ],
            input=env_text,
        )
        assert imported.returncode == 0, imported.stderr
        bound = _exec(name, ["credsctl", "bind", subject, profile_id])
        assert bound.returncode == 0, bound.stderr


def _creds_status(name: str) -> dict:
    deadline = time.monotonic() + 120
    last: dict = {}
    while time.monotonic() < deadline:
        result = _exec(name, ["credsctl", "status", "--json"])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert isinstance(payload, dict)
        body = payload.get("result") if payload.get("ok") else payload
        if not isinstance(body, dict):
            body = payload
        last = body
        if body.get("instances", {}).get("running", 0) >= 2:
            return body
        time.sleep(1)
    return last


def _call_tool_retry(port: int, sub: str, tool: str, *, jti_prefix: str) -> dict:
    deadline = time.monotonic() + 120
    attempt = 0
    last: dict = {}
    while time.monotonic() < deadline:
        attempt += 1
        last = _call_tool(port, sub, tool, jti=f"{jti_prefix}-{attempt}")
        text = last.get("content", [{}])[0].get("text", "")
        if "instance_unavailable" not in text:
            assert last.get("isError") is True
            return last
        time.sleep(2)
    raise AssertionError(f"tool call never left backoff: {last}")


def _call_tool(port: int, sub: str, tool: str, *, jti: str) -> dict:
    identity = _identity_token(sub=sub, tool=tool, jti=jti)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        }
    ).encode()
    req = request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=body,
        headers={
            "Host": AUDIENCE,
            "Authorization": f"Bearer {BEARER}",
            "X-Keystone-Identity": identity,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["result"]
    return result


def _identity_token(*, sub: str, tool: str, jti: str) -> str:
    now = int(time.time())
    claims = {
        "iss": "http://localhost:8080",
        "aud": AUDIENCE,
        "sub": sub,
        "sub_type": "human",
        "email": sub,
        "roles": ["member"],
        "rbac": "enforce",
        "auth_method": "google",
        "interactive": True,
        "task_id": "t_notion",
        "mcp_method": "tools/call",
        "mcp_target": tool,
        "iat": now,
        "exp": now + 300,
        "jti": jti,
    }
    return jwt.encode(
        claims,
        SECRET,
        algorithm="HS256",
        headers={"typ": "keystone-identity+jwt"},
    )


def _exec(
    name: str, args: list[str], *, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "exec", "-i", name, *args]
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        input=input,
        timeout=120,
    )


def _install_from_logs(name: str) -> dict:
    logs = subprocess.run(["docker", "logs", name], check=False, capture_output=True, text=True)
    text = logs.stdout + logs.stderr
    found: dict | None = None
    for line in text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            payload = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == "install":
            found = payload
    assert found is not None, text[-2000:]
    return found


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
