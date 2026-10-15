"""Store fields for `/healthz`.

The HTTP route itself belongs to the facade. This module only decides the
status code and the `status` / `store` fields so that facade can merge them:

- no `X-Forwarded-Host`: keep the facade body and overwrite `status` and `store`
  (`isolation` and `install` stay with the facade);
- `X-Forwarded-Host` present (the request came through the public domain):
  same status code, body `{}`.

`StoreSchemaNewer` while opening the store is `schema_newer=True` (HTTP 503).
Opening in that case logs `store_schema_newer` and writes nothing.
A key that does not match is HTTP 200 with `status=degraded` and
`store=key_mismatch` for an internal caller.
"""


def store_health(store: object | None, *, schema_newer: bool = False) -> tuple[int, str, str]:
    """Return `(http_status, status_field, store_field)`."""
    if schema_newer:
        return 503, "unavailable", "schema_newer"
    if store is None:
        return 503, "unavailable", "unavailable"
    if getattr(store, "key_state", "ok") == "key_mismatch":
        return 200, "degraded", "key_mismatch"
    return 200, "ok", "ok"


def healthz_body(
    store: object | None,
    *,
    schema_newer: bool = False,
    forwarded_host: str | None = None,
    isolation: str = "unknown",
    install: str = "unknown",
) -> tuple[int, dict]:
    """Full `/healthz` payload, including the public-domain redaction.

    `isolation` and `install` default to `unknown` until the facade's neighbours
    fill them in. Pass the facade's current values when merging.
    """
    status_code, status_field, store_field = store_health(store, schema_newer=schema_newer)
    if forwarded_host:
        return status_code, {}
    return status_code, {
        "status": status_field,
        "store": store_field,
        "isolation": isolation,
        "install": install,
    }
