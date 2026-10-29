# Changelog

## [Unreleased]

Packages atlassian, notion, tavily, yandex-tracker and yandex-wiki 1.1.1 run on image 0.3.1. `yandex-tracker` 1.1.1 changes only the image.

### Security

- Package `atlassian` 1.1.1: `fastmcp` (vendored by `mcp-atlassian` 0.23.1) 3.4.8 → 3.4.11 (GHSA-7xgj-8392-5q4r, authorization bypass after transforms) and `certifi` → 2026.10.23. `mcp-atlassian` stays 0.23.1 (no newer upstream release); the lock overrides its transitive `fastmcp`/`certifi` pins.
- Package `yandex-wiki` 1.1.1: `fastmcp` (vendored by `ya-wiki-mcp` 0.3.1) 4.1.0 → 4.1.4 (GHSA-7xgj-8392-5q4r, GHSA-vqqp-x5qm-5qp4) and `certifi` → 2026.10.23. `ya-wiki-mcp` stays 0.3.1 (no newer upstream release); the lock overrides its transitive `fastmcp`/`certifi` pins.
- Package `notion` 1.1.1: `@notionhq/notion-mcp-server` 2.5.2 → 2.6.0 (fixes `zod` and `body-parser` findings) plus a `fast-uri` 3.0.1 → 3.1.9 override (GHSA-w74x-v6f9-j3xg and others). `npm audit --omit=dev` is clean.
- Package `tavily` 1.1.1: `tavily-mcp` stays 0.2.22 (no newer upstream release); `package.json` overrides its transitive `@modelcontextprotocol/sdk` (→ 1.32.2, GHSA-6qxp-vccf-f47h and five more advisories), `body-parser` (→ 2.4.0, GHSA-27r3-jxmh-p828), and `fast-uri` (→ 3.1.9). `npm audit --omit=dev` is clean.

## [0.3.1] - 2026-10-28

### Security

- `mcp` 1.28.1 → 1.30.0 (four known CVEs fixed upstream; none were reachable in this image's code paths, see the 0.3.0 audit notes) and `certifi` → 2026.10.23 (compromised eMudhra root CA advisory).
- Image: upgraded `libssl3`/`openssl` in the final layer, and the npm CLI vendored by the node build stage (`npm@12.2.1`, plus a patched `undici` 6.29.1) to drop fixable CRITICAL/HIGH findings reported by Trivy (`pacote`, `sigstore`, `picomatch`, `brace-expansion`, `ip-address`, `undici`). Base images stay pinned by digest.

### Fixed

- `scripts/pin_digest.py` no longer leaves the `# placeholder until the stdio-box image release records the digest` comment above an `image:` line it has already pinned.
- `tests/packages/test_notion.py`'s `keystonectl lint` check used to hardcode a developer checkout path and silently skip everywhere else; it now reads the checkout path from `KEYSTONECTL_SRC` and skips loudly when it is not set.

### CI

- `release-image.yml` scans the built image with Trivy (CRITICAL/HIGH, ignore-unfixed) before tagging the release version, matching keystone-mcp.
- Added Dependabot for `pip`, `docker`, and `github-actions`.

## [0.3.0] - 2026-10-27

### Fixed

- `creds_desired_set` messages speak to a chat: a secret field, `secrets`, or `env_file` is refused with a message that the secret is entered on the one-time form `creds_sync` sends, not with the `credsctl` advice to give a file or env reference. Messages call the text "the set" rather than "the file".
- `creds_sync` and `credsctl sync` list bindings that the set or file does not name, and that stay because prune is off, with `action: kept`. They are not changes: they do not make a confirm code or count in `changes`. The plan's `next` says how many there are.
- `creds_profiles` returns `managed_by`, and `credsctl profile list` prints it.
- `credsctl <command> --help` prints that command's usage and exits 0; it used to be `unknown argument --help`, exit 2. `credsctl --help` also exits 0.
- The package manifests list every box tool of this build, `creds_snapshot_tools`, `creds_desired_get`, `creds_desired_set`, and `creds_sync` included, and `critical` has `creds_desired_set` and `creds_sync`, so the policy keystonectl installs marks them as the sample policy does. The package test now compares the manifests with `creds_tools` and with `policy.example.yaml`.

### Changed

- An audit row no longer bumps `meta.revision`. Only credential and binding changes do, so a call that changes nothing leaves the instance watcher alone.
- The one-time secret page at `/creds/<token>` is set in type, with the account and its already-known fields set apart from the secret inputs. The page still has no script and no external resources.

### Added

- Packages atlassian 1.1.0, notion 1.1.0, tavily 1.1.0, yandex-tracker 1.1.0, yandex-wiki 1.1.0 on image 0.3.0. They list every box tool of this build and mark `creds_desired_set` and `creds_sync` critical.
- `creds_desired_get`, `creds_desired_set`, and `creds_sync`: the `credsctl sync` set from chat. The set is stored in the box with every version (`from_version` restores an older one) and holds no secrets; `secrets`, `{file:}`, `{env:}`, and `env_file` are refused with `secret_field_in_chat`. `creds_sync` returns a plan and a confirm code, and applies only when called again with that code; the code goes stale (`sync_plan_stale`) when the set or the box changes. A profile that needs a secret gets a one-time form link. `prune` follows the `credsctl sync` rules. Refusal codes `desired_not_found`, `desired_invalid`, and `sync_plan_stale`. The sample policy lists `creds_desired_set` and `creds_sync` in `critical_tools` and `requires_human_identity`. `stdio-box box-tools` now prints fourteen names.
- Store schema 4: table `desired_sets`.
- `credsctl sync --file <path|-> [--dry-run] [--prune] [--no-check]`: brings profiles and bindings to a YAML file. Secrets are file or environment references, never values in the file; a profile that does not differ is not rewritten; a profile the provider rejects is skipped and the rest is applied, exit code 6 (`partially_applied`); `--prune` removes every binding not in the file and deletes only profiles that `sync` wrote.
- Store schema 3: `profiles.managed_by`, set to `credsctl sync` when that command writes a profile. `credsctl profile list` shows it. The migration copies the database to `backup/` first, as before.
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
