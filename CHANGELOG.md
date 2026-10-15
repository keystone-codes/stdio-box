# Changelog

## [Unreleased]

### Added

- One image for every provider; provider directories under `packages/`; descriptor format `provider-v1`; `stdio-box validate-provider`.

### Security

- Children run under per-slot uids started by a root-only spawner; the wrapper is non-dumpable; child stderr is rate-limited and masked.
