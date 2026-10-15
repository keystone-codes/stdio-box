"""A credential change reaches the next call without restarting the process."""

from pathlib import Path

import anyio
import pytest
from tests.support.routing import (
    ECHO_DIR,
    STORE_KEY,
    body,
    make_runtime,
    open_store,
    person,
    seed,
    sha8,
)

import stdio_box.instances as instances
from stdio_box.cli import credsctl_main

OLD = "old-token-value-aaaa"
NEW = "new-token-value-bbbb"


def _invoke(args: list[str]) -> int:
    with pytest.raises(SystemExit) as raised:
        credsctl_main(args)
    code = raised.value.code
    assert isinstance(code, int)
    return code


@pytest.mark.asyncio
async def test_import_and_unbind_apply_to_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "REVISION_POLL_SECONDS", 0.2)
    data = tmp_path / "data"
    store = open_store(tmp_path)
    seed(store, "ann", OLD, "user:ann@example.com")
    monkeypatch.setenv("BOX_DATA_DIR", str(data))
    monkeypatch.setenv("BOX_STORE_KEY", STORE_KEY)
    monkeypatch.setenv("BOX_PROVIDER", "echo")
    monkeypatch.setenv("BOX_PROVIDER_DIR", str(ECHO_DIR))
    env_file = tmp_path / "ann.env"
    env_file.write_text(
        f"ECHO_URL=https://echo.invalid\nECHO_TOKEN={NEW}\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    _provider, _catalog, manager, forwarder = make_runtime(tmp_path, store)
    identity = person("ann@example.com", tool="who", jti="watch")
    async with manager.run():
        first = await forwarder.call(identity, "who", "who", {})
        assert not first.isError
        assert body(first)["token_sha8"] == sha8(OLD)
        assert (
            _invoke(
                [
                    "--json",
                    "profile",
                    "import",
                    "echo/ann",
                    "--from-file",
                    str(env_file),
                    "--no-check",
                ]
            )
            == 0
        )
        deadline = _until(5)
        seen = None
        while deadline():
            seen = await forwarder.call(identity, "who", "who", {})
            if not seen.isError and body(seen).get("token_sha8") == sha8(NEW):
                break
            await anyio.sleep(0.2)
        assert seen is not None
        assert not seen.isError
        assert body(seen)["token_sha8"] == sha8(NEW)
        assert _invoke(["unbind", "user:ann@example.com"]) == 0
        refused = await forwarder.call(identity, "who", "who", {})
        assert body(refused)["code"] == "no_credentials_profile"
    store.close()


def _until(seconds: float) -> object:
    import time

    end = time.monotonic() + seconds

    def _open() -> bool:
        return time.monotonic() < end

    return _open
