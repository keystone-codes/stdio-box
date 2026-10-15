# Provider directories

Each third-party stdio server is a directory under `packages/<name>/` in this repository. The catalog points at that directory with `path`. One `stdio-box` image serves every provider.

A provider directory contains:

| File | Role |
|---|---|
| `provider.yaml` | Command, credential fields, variants, and checks. Schema: `schemas/provider-v1.json`. |
| `requirements.txt` or `package-lock.json` | Lock file named by `server.lock`. Python locks are `requirements.txt` (command starts with `uvx`). Node locks are `package-lock.json` with `package.json` beside them (command starts with `npx`). |
| `tools.snapshot.json` | `tools/list` captured once, so the box can publish tools without an account. |
| `keystone-service.yaml` | Package manifest. It pins the image digest. |
| `policy.example.yaml` | Example policy printed for the operator. |
| `README.md` | Notes for that provider. |

The image build copies `packages/` to `/opt/box/packages/`. `.dockerignore` leaves out `keystone-service.yaml`, `policy.example.yaml`, and `README.md`. Those files pin or describe the image, so they must not be inside it. Lock files, `provider.yaml`, `package.json`, and `tools.snapshot.json` do go into the image.

The server's own code does not. On first start the box installs the locked server into the `cache` volume. Later starts reuse that volume and do not need the network. A mirror (`BOX_PIP_INDEX_URL`, `BOX_NPM_REGISTRY`) is enough for a host that cannot reach the public indexes. This release does not publish an image with a server already installed.

Build the lock from the provider directory:

```sh
uv pip compile --universal --generate-hashes --python-version 3.12 requirements.in -o requirements.txt
npm install --package-lock-only
```

`tools.snapshot.json` comes from `keystonectl package wrap-stdio`, which runs `stdio-box snapshot-tools` in the image.

Names for this release, without a `-box` suffix: `atlassian` (Jira and Confluence), `notion`, `tavily`, `yandex-tracker`, `yandex-wiki`. Those directories are added by later changes. The echo server and the atlassian descriptor used by tests live under `tests/data/packages/`.
