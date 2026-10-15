"""Bearer on /mcp, and /healthz without it."""

import json

from starlette.testclient import TestClient
from tests.support.facade import TEST_BEARER, build_echo_app

WRONG_BEARER = "wrong-bearer-token-zzzzzzzzzzzzzz"


def _post(client: TestClient, *, bearer: str | None, url: str = "/mcp") -> object:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post(
        url,
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )


def test_mcp_bearer_and_open_healthz() -> None:
    app = build_echo_app()
    with TestClient(app, base_url="http://echo") as client:
        missing = _post(client, bearer=None)
        assert missing.status_code == 401
        assert missing.json() == {"error": "unauthorized"}
        assert missing.headers["www-authenticate"] == "Bearer"

        wrong = _post(client, bearer=WRONG_BEARER)
        assert wrong.status_code == 401
        assert wrong.json() == {"error": "unauthorized"}

        ok = _post(client, bearer=TEST_BEARER)
        assert ok.status_code == 200
        body = json.loads(ok.text)
        assert "result" in body

        for method in ("GET", "DELETE"):
            denied = client.request(method, "/mcp")
            assert denied.status_code == 401
            assert denied.headers["www-authenticate"] == "Bearer"

        private = client.get("/healthz")
        assert private.status_code == 200
        assert private.json() == {
            "status": "ok",
            "store": "unknown",
            "isolation": "unknown",
            "install": "unknown",
        }

        public = client.get("/healthz", headers={"X-Forwarded-Host": "echo.example"})
        assert public.status_code == 200
        assert public.json() == {}
