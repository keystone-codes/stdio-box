# Changelog

## [Unreleased]

### Added

- Per-profile instances with lazy start, idle stop, LRU eviction, ping-based restart, backoff; user, ordered role and default bindings; per-variant tool sets; call audit.
- One image for every provider; provider directories under `packages/`; descriptor format `provider-v1`; `stdio-box validate-provider`.
- HTTP facade: bearer, signed identity with `mcp_target` and replay check, tool catalog from the provider snapshot, `stdio-box serve`.
- Encrypted profile store (SQLite, AES-256-GCM, HKDF from `BOX_STORE_KEY`), ordered role bindings, migrations with backup, key-mismatch mode, `credsctl`.
- The provider's server is installed at container start from its hash-pinned lock file into the `cache` volume, owned by a separate user; `stdio-box install`, `stdio-box snapshot-tools`.

### Security

- Children run under per-slot uids started by a root-only spawner; the wrapper is non-dumpable; child stderr is rate-limited and masked.
