"""The Atlassian stand-in names the caller and serves both products."""

import base64
import hashlib
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from tests.support.atlassian_mock import AtlassianMockHandler, identity_name


def test_identity_name_from_basic_and_bearer() -> None:
    basic = base64.b64encode(b"ada@example.test:secret").decode("ascii")
    assert identity_name(f"Basic {basic}") == "ada@example.test"
    token = "server-token"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    assert identity_name(f"Bearer {token}") == f"pat-{digest}"
    assert identity_name(None) == "anonymous"


def test_jira_and_confluence_routes() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), AtlassianMockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    token = "confluence-token"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    name = f"pat-{digest}"
    try:
        myself = _get(port, "/rest/api/3/myself", f"Bearer {token}")
        assert myself["displayName"] == name
        assert myself["accountId"] == "e2e"
        assert myself["emailAddress"] == name
        info = _get(port, "/rest/api/2/serverInfo", f"Bearer {token}")
        assert info["deploymentType"] == "Server"
        assert info["versionNumbers"] == [9, 12, 22]
        issue = _get(port, "/rest/api/2/issue/E2E-1", f"Bearer {token}")
        assert issue["fields"]["summary"] == f"issue seen by {name}"
        page = _get(port, "/wiki/rest/api/content/99", f"Bearer {token}")
        assert page["title"] == f"page seen by {name}"
        server_page = _get(port, "/rest/api/content/99", f"Bearer {token}")
        assert server_page["title"] == f"page seen by {name}"
        current = _get(port, "/wiki/rest/api/user/current", f"Bearer {token}")
        assert current == {"displayName": name}
        space = _get(port, "/rest/api/space", f"Bearer {token}")
        assert len(space["results"]) == 1
        assert _get(port, "/rest/api/2/unknown", f"Bearer {token}") == {}
    finally:
        server.shutdown()


def _get(port: int, path: str, authorization: str) -> dict:
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": authorization},
    )
    with urlopen(request) as response:
        assert response.status == 200
        body = json.loads(response.read().decode("utf-8"))
    assert isinstance(body, dict)
    return body
