# Changelog

## [Unreleased]

### Added

- One image for every provider; provider directories under `packages/`; descriptor format `provider-v1`; `stdio-box validate-provider`.
- Encrypted profile store (SQLite, AES-256-GCM, HKDF from `BOX_STORE_KEY`), ordered role bindings, migrations with backup, key-mismatch mode, `credsctl`.
