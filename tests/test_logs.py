"""Access-log redaction for credential links."""

import json
import logging

from stdio_box.logs import JsonFormatter, redact_path


def test_creds_paths_are_masked() -> None:
    assert redact_path("/creds/token") == "/creds/***"
    assert redact_path("/CREDS/Token") == "/creds/***"
    assert redact_path("/foo/../creds/secret") == "/creds/***"
    assert redact_path("/./creds/secret") == "/creds/***"
    assert redact_path("/x%2F..%2Fcreds%2Fsecret") == "/creds/***"
    assert redact_path("/mcp") == "/mcp"


def test_json_formatter_is_one_object() -> None:
    record = logging.LogRecord(
        name="stdio_box.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request",
        args=(),
        exc_info=None,
    )
    record.event = "http_request"
    record.path = "/creds/***"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "request"
    assert payload["event"] == "http_request"
    assert payload["path"] == "/creds/***"
    assert payload["level"] == "INFO"
    assert "\n" not in JsonFormatter().format(record)
