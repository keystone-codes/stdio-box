import secrets
from pathlib import Path

from stdio_box.store.health import healthz_body
from stdio_box.store.repo import Store


def test_key_mismatch_is_degraded_for_internal_callers_and_empty_in_public(
    tmp_path: Path, store_key: str
) -> None:
    data = tmp_path / "data"
    original = Store.open(data, store_key)
    original.close()
    store = Store.open(data, secrets.token_urlsafe(32))
    assert store.key_state == "key_mismatch"

    code, body = healthz_body(store)
    assert code == 200
    assert body["status"] == "degraded"
    assert body["store"] == "key_mismatch"
    assert body["isolation"] == "unknown"
    assert body["install"] == "unknown"

    public_code, public_body = healthz_body(store, forwarded_host="box.example")
    assert public_code == 200
    assert public_body == {}
    store.close()


def test_schema_newer_is_unavailable() -> None:
    code, body = healthz_body(None, schema_newer=True)
    assert code == 503
    assert body["status"] == "unavailable"
    assert body["store"] == "schema_newer"
    public_code, public_body = healthz_body(None, schema_newer=True, forwarded_host="box.example")
    assert public_code == 503
    assert public_body == {}
