"""HTTP stand-in for Jira and Confluence.

The caller's name comes from ``Authorization``. ``Basic`` uses the login.
``Bearer`` uses ``pat-`` plus the first eight hex characters of SHA-256 of the
token. Jira paths are the Cloud and Server REST paths. Confluence is served
both under ``/wiki`` (Cloud) and at the server root.

This file has no third-party dependencies. The end-to-end run vendors it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_ANONYMOUS = "anonymous"


def identity_name(authorization: str | None) -> str:
    """Return the account name encoded in an Authorization header."""
    if not authorization:
        return _ANONYMOUS
    scheme, _, rest = authorization.partition(" ")
    token = rest.strip()
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            return _ANONYMOUS
        login, _separator, _password = decoded.partition(":")
        return login or _ANONYMOUS
    if scheme.lower() == "bearer" and token:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
        return f"pat-{digest}"
    return _ANONYMOUS


def response_for(path: str, authorization: str | None) -> dict[str, object]:
    """Return the JSON body for one request path."""
    name = identity_name(authorization)
    route = urlsplit(path).path
    if route.startswith("/wiki/"):
        return _confluence(route[len("/wiki") :], name)
    jira = _jira(route, name)
    if jira is not None:
        return jira
    return _confluence(route, name)


def _jira(path: str, name: str) -> dict[str, object] | None:
    if path in {"/rest/api/2/myself", "/rest/api/3/myself"}:
        return {"displayName": name, "accountId": "e2e", "emailAddress": name}
    if path == "/rest/api/2/serverInfo":
        return {
            "baseUrl": "http://atlassian-mock:8080",
            "version": "9.12.22",
            "versionNumbers": [9, 12, 22],
            "deploymentType": "Server",
            "buildNumber": 912022,
            "buildDate": "2024-01-15",
            "serverTime": "2026-10-15T12:00:00.000+0000",
            "scmInfo": "mock",
            "serverTitle": "Jira",
        }
    prefix, separator, issue_key = path.partition("/issue/")
    if separator and prefix.startswith("/rest/api/") and "/" not in prefix[len("/rest/api/") :]:
        if issue_key and "/" not in issue_key:
            return {"key": issue_key, "fields": {"summary": f"issue seen by {name}"}}
    return None


def _confluence(path: str, name: str) -> dict[str, object]:
    if path == "/rest/api/user/current":
        return {"displayName": name}
    if path.startswith("/rest/api/content/") and path.count("/") == 4:
        page_id = path.rsplit("/", 1)[-1]
        if page_id:
            return {"id": page_id, "type": "page", "title": f"page seen by {name}"}
    if path == "/rest/api/space":
        return {"results": [{"id": 1, "key": "E2E", "name": "E2E", "type": "global"}], "size": 1}
    return {}


class AtlassianMockHandler(BaseHTTPRequestHandler):
    """Answer Jira and Confluence reads. Everything else is ``{}``."""

    def do_GET(self) -> None:
        body = response_for(self.path, self.headers.get("Authorization"))
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve(port: int) -> None:
    """Listen on all interfaces until the process is stopped."""
    server = ThreadingHTTPServer(("0.0.0.0", port), AtlassianMockHandler)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or not args[0].isdigit() or not 1 <= int(args[0]) <= 65535:
        print("usage: atlassian_mock.py PORT", file=sys.stderr)
        return 2
    serve(int(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
