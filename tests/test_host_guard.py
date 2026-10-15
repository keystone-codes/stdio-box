"""Host allow-list on /mcp."""

import json

from starlette.testclient import TestClient
from tests.support.facade import TEST_BEARER, build_echo_app


def _list(client: TestClient, url: str) -> object:
    return client.post(
        url,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TEST_BEARER}",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )


def test_foreign_host_is_rejected_and_allowed_hosts_pass() -> None:
    app = build_echo_app(allowed_hosts=("extra.example",))
    with TestClient(app, base_url="http://echo") as client:
        foreign = _list(client, "http://evil.example/mcp")
        assert foreign.status_code == 421
        assert foreign.json() == {"error": "misdirected_request"}

        allowed = [
            "http://echo/mcp",
            "http://echo:8081/mcp",
            "http://echo-mcp/mcp",
            "http://echo-mcp:9/mcp",
            "http://localhost/mcp",
            "http://localhost:8081/mcp",
            "http://127.0.0.1/mcp",
            "http://127.0.0.1:8081/mcp",
            "http://extra.example/mcp",
            "http://extra.example:443/mcp",
        ]
        for url in allowed:
            response = _list(client, url)
            assert response.status_code == 200, url
            assert "result" in json.loads(response.text)
