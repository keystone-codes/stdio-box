# Changelog

## [Unreleased]

### Changed

- The one-time secret page at `/creds/<token>` is set in type, with the account and its already-known fields set apart from the secret inputs. The page still has no script and no external resources.

### Added

- Package version 0.2.0.
- `creds_snapshot_tools`: takes the server's `tools/list` under the caller's own profile, stores the raw snapshot (newest five, table `tool_snapshots`), and returns its id, `sha256`, the published names, the profile, the variant, and `variant_filters_tools`. Refusal code `snapshot_invalid`.
- `credsctl snapshot list` and `credsctl snapshot export <id>`.
- `stdio-box box-tools` prints the names of the image's own `creds_*` tools as JSON. There are eleven.

## [0.1.0] - 2026-10-15

### Added

- Packages: atlassian 1.0.0, notion 1.0.0, tavily 1.0.0, yandex-tracker 1.0.0, yandex-wiki 1.0.0.
- Package `tavily` 1.0.0 (tavily-mcp 0.2.22).
- Package `yandex-tracker` 1.0.0 (@fractalizer/mcp-server-yandex-tracker 5.1.0).
- Package `yandex-wiki` 1.0.0 (ya-wiki-mcp 0.3.1).
- Package `atlassian` 1.0.0: Jira and Confluence (mcp-atlassian 0.23.1).
- Ten `creds_*` tools, ordered role bindings with `position`, `creds_bindings`; `stdio-box policy-example`.
- Package `notion` 1.0.0 (@notionhq/notion-mcp-server 2.5.2).
- Per-profile instances with lazy start, idle stop, LRU eviction, ping-based restart, backoff; user, ordered role and default bindings; per-variant tool sets; call audit.
- One image for every provider; provider directories under `packages/`; descriptor format `provider-v1`; `stdio-box validate-provider`.
- HTTP facade: bearer, signed identity with `mcp_target` and replay check, tool catalog from the provider snapshot, `stdio-box serve`.
- Encrypted profile store (SQLite, AES-256-GCM, HKDF from `BOX_STORE_KEY`), ordered role bindings, migrations with backup, key-mismatch mode, `credsctl`.
- The provider's server is installed at container start from its hash-pinned lock file into the `cache` volume, owned by a separate user; `stdio-box install`, `stdio-box snapshot-tools`.
- One-time secret entry form under `/creds/`, provider checks over HTTP with host allow-lists and no private addresses by default, rotation that keeps old values until new ones pass the check.

### Fixed

- The slot `tmp` directory is created by the spawner, so a tool call can start the child after a credential check.

### Security

- Children run under per-slot uids started by a root-only spawner; the wrapper is non-dumpable; child stderr is rate-limited and masked.
- The form needs a matching Origin, a same-site cookie and a CSRF field; links expire in 15 minutes and work once.
