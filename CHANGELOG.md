# Changelog

## [Unreleased]

### Added

- One image for every provider; provider directories under `packages/`; descriptor format `provider-v1`; `stdio-box validate-provider`.
- The provider's server is installed at container start from its hash-pinned lock file into the `cache` volume, owned by a separate user; `stdio-box install`, `stdio-box snapshot-tools`.
