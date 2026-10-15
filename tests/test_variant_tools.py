"""A variant that omits a tool does not start a child, and does not look like drift."""

from pathlib import Path

import pytest
from tests.support.routing import body, make_runtime, open_store, person, seed


@pytest.mark.asyncio
async def test_token_variant_refuses_extra_tool_without_a_child(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING")
    store = open_store(tmp_path)
    seed(store, "ann", "token-ann-aaaaaa", "user:ann@example.com", variant="token")
    seed(
        store,
        "full",
        "token-full-aaaaa",
        "user:full@example.com",
        variant="full",
    )
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store)
    async with manager.run():
        refused = await forwarder.call(
            person("ann@example.com", tool="extra_tool", jti="extra"),
            "extra_tool",
            "extra_tool",
            {},
        )
        assert manager.snapshot()["instances"]["running"] == 0
        assert body(refused)["code"] == "tool_not_in_profile"
        assert "URL and token" in body(refused)["message"]
        assert "extra_tool" in body(refused)["message"]
        assert "tools_snapshot_drift" not in caplog.text

        listed = await forwarder.call(
            person("ann@example.com", tool="who", jti="who"), "who", "who", {}
        )
        assert not listed.isError
        assert "tools_snapshot_drift" not in caplog.text

        extra = await forwarder.call(
            person("full@example.com", tool="extra_tool", jti="full"),
            "extra_tool",
            "extra_tool",
            {},
        )
        assert not extra.isError
        assert body(extra)["name"] == "extra_tool"
    store.close()
