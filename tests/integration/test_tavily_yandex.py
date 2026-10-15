"""Tavily, Yandex Tracker, and Yandex Wiki: install, two profiles, two instances.

Fake tokens only. Profile import uses ``--no-check`` so the suite does not call
the provider HTTP checks. A tool call still reaches the child, which rejects
the token; that error is returned as the child sent it.
"""

import json
import socket
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
BEARER = "b" * 32
SECRET = "s" * 32
STORE_KEY = "k" * 32
TOKEN_A = "integration-test-token-profile-a-0123456789"
TOKEN_B = "integration-test-token-profile-b-0123456789"

CASES = (
    {
        "provider": "tavily",
        "namespace": "tavily",
        "command": "npx -y tavily-mcp@0.2.22",
        "tool": "tavily_search",
        "arguments": {"query": "integration"},
        "env": lambda token: f"TAVILY_API_KEY={token}\n",
    },
    {
        "provider": "yandex-tracker",
        "namespace": "yandex_tracker",
        "command": "npx -y @fractalizer/mcp-server-yandex-tracker@5.1.0",
        "tool": "fr_yandex_tracker_ping",
        "arguments": {},
        "env": lambda token: f"YANDEX_TRACKER_TOKEN={token}\nYANDEX_ORG_ID=100\n",
    },
    {
        "provider": "yandex-wiki",
        "namespace": "yandex_wiki",
        "command": "uvx ya-wiki-mcp==0.3.1",
        "tool": "get_page",
        "arguments": {"slug": "home"},
        "env": lambda token: f"YA_WIKI_TOKEN={token}\nYA_WIKI_ORG_ID=100\n",
    },
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["provider"])
def test_two_profiles_install_and_forward_child_error(case: dict) -> None:
    provider = case["provider"]
    volume = f"stdio-box-{provider}-cache"
    subprocess.run(["docker", "volume", "rm", "-f", volume], check=False, capture_output=True)
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    first = ""
    second = ""
    stores: list[str] = []
    port = _free_port()
    try:
        first = _start(case, port, volume, stores)
        _wait_healthy(first, port, case["namespace"])
        _seed_profiles(first, case)
        _call_tool_retry(port, case, "a@example.test", jti_prefix="call-a")
        _call_tool_retry(port, case, "b@example.test", jti_prefix="call-b")
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

        second = _start(case, port, volume, stores)
        _wait_healthy(second, port, case["namespace"], timeout=120)
        install = _install_from_logs(second)
        assert install.get("from_cache") is True
    finally:
        if first:
            _remove(first)
        if second:
            _remove(second)
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume, *stores], check=False, capture_output=True
        )


def _start(case: dict, port: int, volume: str, stores: list[str]) -> str:
    name = f"stdio-box-{case['provider']}-{uuid.uuid4().hex[:8]}"
    store = f"stdio-box-{case['provider']}-store-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "volume", "create", store], check=True, capture_output=True)
    stores.append(store)
    env = [
        "-p",
        f"{port}:8081",
        "-v",
        f"{store}:/data",
        "-v",
        f"{volume}:/cache",
        "-e",
        f"BOX_PROVIDER={case['provider']}",
        "-e",
        f"BOX_COMMAND={case['command']}",
        "-e",
        f"BOX_BEARER_TOKEN={BEARER}",
        "-e",
        f"BOX_IDENTITY_SECRET={SECRET}",
        "-e",
        f"BOX_IDENTITY_AUDIENCE={case['namespace']}",
        "-e",
        f"BOX_STORE_KEY={STORE_KEY}",
        "-e",
        f"BOX_PUBLIC_BASE_URL=https://{case['namespace']}.example",
    ]
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


def _wait_healthy(name: str, port: int, namespace: str, timeout: int = 360) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            req = request.Request(
                f"http://127.0.0.1:{port}/healthz",
                headers={"Host": namespace},
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


def _seed_profiles(name: str, case: dict) -> None:
    provider = case["provider"]
    for profile_id, token, subject in (
        (f"{provider}/a", TOKEN_A, "user:a@example.test"),
        (f"{provider}/b", TOKEN_B, "user:b@example.test"),
    ):
        imported = _exec(
            name,
            ["credsctl", "profile", "import", profile_id, "--no-check", "--from-file", "-"],
            input=case["env"](token),
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


def _call_tool_retry(port: int, case: dict, sub: str, *, jti_prefix: str) -> dict:
    deadline = time.monotonic() + 120
    attempt = 0
    last: dict = {}
    while time.monotonic() < deadline:
        attempt += 1
        last = _call_tool(port, case, sub, jti=f"{jti_prefix}-{attempt}")
        text = _result_text(last)
        if "instance_unavailable" not in text:
            assert _forwarded_child_error(last), last
            return last
        time.sleep(2)
    raise AssertionError(f"tool call never left backoff: {last}")


def _result_text(result: dict) -> str:
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return ""
    first = content[0]
    if not isinstance(first, dict):
        return ""
    text = first.get("text", "")
    return text if isinstance(text, str) else ""


def _forwarded_child_error(result: dict) -> bool:
    """The box returned the child's answer, and that answer is a rejection.

    Some servers set ``isError``. Others return the upstream 401/403 as ordinary
    tool text. Either shape is the child, unchanged.
    """
    text = _result_text(result)
    for marker in ("instance_unavailable", "tool_not_in_profile", "profile_not_found"):
        if marker in text:
            return False
    if result.get("isError") is True:
        return True
    lowered = text.lower()
    return "401" in text or "403" in text or "unauthorized" in lowered or "ошибка" in lowered


def _call_tool(port: int, case: dict, sub: str, *, jti: str) -> dict:
    tool = case["tool"]
    identity = _identity_token(namespace=case["namespace"], sub=sub, tool=tool, jti=jti)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": case["arguments"]},
        }
    ).encode()
    req = request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=body,
        headers={
            "Host": case["namespace"],
            "Authorization": f"Bearer {BEARER}",
            "X-Keystone-Identity": identity,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
    return payload["result"]


def _identity_token(*, namespace: str, sub: str, tool: str, jti: str) -> str:
    now = int(time.time())
    claims = {
        "iss": "http://localhost:8080",
        "aud": namespace,
        "sub": sub,
        "sub_type": "human",
        "email": sub,
        "roles": ["member"],
        "rbac": "enforce",
        "auth_method": "google",
        "interactive": True,
        "task_id": "t_provider",
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
    return subprocess.run(
        ["docker", "exec", "-i", name, *args],
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
