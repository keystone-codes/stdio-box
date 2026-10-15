"""Atlassian profiles through the box, against the local Jira and Confluence stand-in.

Requires Docker, the ``stdio-box:dev`` image, and a network that can install
mcp-atlassian. The stand-in is ``tests/support/atlassian_mock.py`` on the same
Docker network.
"""

import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tests.support.facade import TEST_BEARER, TEST_SECRET, TEST_STORE_KEY, make_token

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "stdio-box:dev"
MOCK = ROOT / "tests" / "support" / "atlassian_mock.py"
PACKAGE = ROOT / "packages" / "atlassian"
MOCK_URL = "http://atlassian-mock:8080"
PUBLIC = "https://atlassian.example"
AUDIENCE = "atlassian"


async def test_profiles_keep_accounts_apart() -> None:
    suffix = uuid.uuid4().hex[:8]
    network = f"atlassian-e2e-{suffix}"
    mock_name = f"atlassian-mock-{suffix}"
    box_name = f"atlassian-box-{suffix}"
    store = f"atlassian-store-{suffix}"
    cache = f"atlassian-cache-{suffix}"
    subprocess.run(["docker", "network", "create", network], check=True, capture_output=True)
    try:
        _start_mock(mock_name, network)
        _start_box(box_name, network, store, cache)
        port = _wait_ready(box_name)
        base = f"http://127.0.0.1:{port}"
        jira_token = uuid.uuid4().hex
        confluence_token = uuid.uuid4().hex
        jira_name = _pat(jira_token)
        confluence_name = _pat(confluence_token)

        created = await _call(
            base,
            "creds_profile_create",
            {
                "name": "dev",
                "variant": "server",
                "fields": {"JIRA_URL": MOCK_URL, "CONFLUENCE_URL": MOCK_URL},
            },
            sub="dev@example.test",
        )
        assert created.isError is False
        cloud = await _call(
            base,
            "creds_profile_create",
            {
                "name": "cloud",
                "variant": "cloud",
                "fields": {"JIRA_URL": MOCK_URL, "CONFLUENCE_URL": MOCK_URL},
            },
            sub="dev@example.test",
        )
        assert _code(cloud) == "invalid_argument"
        profile = _object(created)["profile"]
        saved = _submit_form(
            base,
            _object(created)["form_url"],
            {
                "JIRA_PERSONAL_TOKEN": jira_token,
                "CONFLUENCE_PERSONAL_TOKEN": confluence_token,
            },
        )
        assert saved.status_code == 200
        assert jira_name in saved.text

        bound = await _call(
            base,
            "creds_bind",
            {"subject": "user:dev@example.test", "profile": profile},
            sub="dev@example.test",
        )
        assert bound.isError is False
        issue = await _call(
            base,
            "jira_get_issue",
            {"issue_key": "E2E-1"},
            sub="dev@example.test",
        )
        assert issue.isError is False
        assert f"issue seen by {jira_name}" in _text(issue)
        page = await _call(
            base,
            "confluence_get_page",
            {"page_id": "99"},
            sub="dev@example.test",
        )
        assert page.isError is False
        assert f"page seen by {confluence_name}" in _text(page)

        jira_only_token = uuid.uuid4().hex
        jira_only = await _call(
            base,
            "creds_profile_create",
            {
                "name": "jira-only",
                "variant": "jira_server",
                "fields": {"JIRA_URL": MOCK_URL},
            },
            sub="jira@example.test",
        )
        assert jira_only.isError is False
        jira_profile = _object(jira_only)["profile"]
        jira_saved = _submit_form(
            base,
            _object(jira_only)["form_url"],
            {"JIRA_PERSONAL_TOKEN": jira_only_token},
        )
        assert jira_saved.status_code == 200
        bound_jira = await _call(
            base,
            "creds_bind",
            {"subject": "user:jira@example.test", "profile": jira_profile},
            sub="jira@example.test",
        )
        assert bound_jira.isError is False
        before = _atlassian_processes(box_name)
        refused = await _call(
            base,
            "confluence_get_page",
            {"page_id": "99"},
            sub="jira@example.test",
        )
        assert _code(refused) == "tool_not_in_profile"
        assert _atlassian_processes(box_name) == before
    finally:
        subprocess.run(
            ["docker", "rm", "-f", box_name, mock_name],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["docker", "volume", "rm", "-f", store, cache], check=False, capture_output=True
        )
        subprocess.run(["docker", "network", "rm", network], check=False, capture_output=True)


def _start_mock(name: str, network: str) -> None:
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "--network-alias",
            "atlassian-mock",
            "--entrypoint",
            "python3",
            "-v",
            f"{MOCK}:/opt/atlassian_mock.py:ro",
            IMAGE,
            "/opt/atlassian_mock.py",
            "8080",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _start_box(name: str, network: str, store: str, cache: str) -> None:
    env = [
        "-e",
        f"BOX_BEARER_TOKEN={TEST_BEARER}",
        "-e",
        f"BOX_IDENTITY_SECRET={TEST_SECRET}",
        "-e",
        f"BOX_IDENTITY_AUDIENCE={AUDIENCE}",
        "-e",
        f"BOX_PUBLIC_BASE_URL={PUBLIC}",
        "-e",
        f"BOX_STORE_KEY={TEST_STORE_KEY}",
        "-e",
        "BOX_PROVIDER=atlassian",
        "-e",
        "BOX_COMMAND=uvx mcp-atlassian==0.23.1",
        "-e",
        "BOX_ALLOW_HTTP_URLS=true",
        "-e",
        "BOX_ALLOW_PRIVATE_URLS=true",
    ]
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "-p",
            "127.0.0.1::8081",
            *env,
            "-v",
            f"{PACKAGE}:/opt/box/packages/atlassian:ro",
            "-v",
            f"{store}:/data",
            "-v",
            f"{cache}:/cache",
            IMAGE,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _wait_ready(name: str) -> str:
    deadline = time.monotonic() + 300
    last = ""
    while time.monotonic() < deadline:
        logs = subprocess.run(
            ["docker", "logs", name],
            check=False,
            capture_output=True,
            text=True,
        )
        last = logs.stdout + logs.stderr
        inspected = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name],
            check=False,
            capture_output=True,
            text=True,
        )
        if inspected.stdout.strip().startswith("exited"):
            raise AssertionError(f"box exited\n{last}")
        port = _host_port(name)
        if port:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2)
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code == 200:
                install = response.json().get("install")
                if isinstance(install, dict) and install.get("key"):
                    return port
        time.sleep(1)
    raise AssertionError(f"box did not become ready\n{last}")


def _host_port(name: str) -> str:
    result = subprocess.run(
        ["docker", "port", name, "8081"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    return result.stdout.strip().rsplit(":", 1)[-1]


async def _call(base: str, tool: str, arguments: dict, *, sub: str):
    headers = {
        "Authorization": f"Bearer {TEST_BEARER}",
        "X-Keystone-Identity": make_token(tool=tool, sub=sub, audience=AUDIENCE),
    }
    async with streamablehttp_client(f"{base}/mcp", headers=headers) as (read, write, _session):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, arguments)


def _submit_form(base: str, form_url: str, secrets: dict[str, str]) -> httpx.Response:
    path = form_url
    if "://" in form_url:
        path = "/" + form_url.split("://", 1)[1].split("/", 1)[1]
    opened = httpx.get(f"{base}{path}", timeout=30)
    assert opened.status_code == 200, opened.text
    match = re.search(r'name="csrf" value="([^"]+)"', opened.text)
    assert match is not None
    cookie = re.search(r"__Host-box_form=([^;]+)", opened.headers.get("set-cookie", ""))
    assert cookie is not None
    return httpx.post(
        f"{base}{path}",
        data={"csrf": match.group(1), **secrets},
        headers={
            "Origin": PUBLIC,
            "Cookie": f"__Host-box_form={cookie.group(1)}",
        },
        timeout=30,
    )


def _atlassian_processes(name: str) -> int:
    script = (
        "import os\n"
        "count=0\n"
        "for pid in os.listdir('/proc'):\n"
        "    if not pid.isdigit():\n"
        "        continue\n"
        "    try:\n"
        "        raw=open(f'/proc/{pid}/cmdline','rb').read()\n"
        "    except OSError:\n"
        "        continue\n"
        "    if b'mcp-atlassian' in raw:\n"
        "        count+=1\n"
        "print(count)\n"
    )
    result = subprocess.run(
        ["docker", "exec", name, "python3", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def _object(result) -> dict:
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    return json.loads(_text(result))


def _text(result) -> str:
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _code(result) -> str:
    assert result.isError is True
    return json.loads(_text(result))["code"]


def _pat(token: str) -> str:
    return "pat-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
