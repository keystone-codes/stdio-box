# stdio-box

stdio-box is one container image that runs a third-party MCP stdio server behind the Keystone door. The wrapper is the same for every server; each server is a provider directory under `packages/`.

Callers reach the box only through Keystone. The door checks the person or service account, and the box checks the door's bearer token and signed identity before it forwards a tool call.

## One image, many providers

The image is built from this repository and published as `ghcr.io/keystone-codes/stdio-box`. It contains the wrapper and the **data** for every provider: `provider.yaml`, the lock file, and `tools.snapshot.json` land at `/opt/box/packages/<name>/`. Manifests, example policies, and provider READMEs stay out of the image, because the manifest pins the image digest.

The server program itself is not in the image. The first start of a container installs it from the lock file into the `cache` volume. After that the container can run without the public network. A host that cannot reach PyPI or npm can set `BOX_PIP_INDEX_URL` or `BOX_NPM_REGISTRY`. This release does not ship a per-provider image with the server already installed.

Adding a provider is a new directory under `packages/` and a new image release. See [packages/README.md](packages/README.md).

## Processes in the container

```
PID 1  tini                                 (root; reaps orphaned children)
 └─ stdio-box launch                        (root, short-lived)
     │  1. prepares /data, /cache, /tmp/box
     │  2. installs the provider into /cache under uid 10002
     │  3. splits into two processes:
     ├─ spawner                             (root; spawn, signal, and reap over a socketpair)
     └─ wrapper  uid 10001, non-dumpable    (HTTP :8081 — /mcp, /creds/, /healthz)
          ↕ stdin/stdout/stderr passed across by the spawner
         child  uid 20000+slot              (one profile's server; HOME=/tmp/box/slot-N)
```

`stdio-box launch` is the process the image starts. It fixes the directory owners, calls the provider install step, then splits into the root spawner and the wrapper. Until the install step is filled in, `launch` exits with code 2 and `provider install is not implemented yet`, unless `BOX_DEBUG=1` and `BOX_CHILD_ARGV_OVERRIDE` name a program to run as the child.

| Path | Owner | Mode | Use |
|---|---|---|---|
| `/data` | uid 10001 (`box`) | `0700` | Profile store |
| `/cache` | uid 10002 (`box-install`) | `0755` | Installed server, written only by the installer |
| `/tmp/box` | root | `0711` | Per-slot homes for children |

The image does not set `USER`. `launch` starts as root so it can drop to these uids.

## `provider.yaml`

`schemas/provider-v1.json` (Draft 2020-12, `$id` `https://keystone.codes/schemas/stdio-provider-v1.json`). Every object rejects unknown keys. `stdio-box validate-provider <directory>` checks the schema and the rules below. Success is exit 0. Failures are one `path: message` line each and exit 1.

| Field | Required | Constraints |
|---|---|---|
| `schema_version` | yes | `1` |
| `name` | yes | `^[a-z][a-z0-9_-]{0,30}$`, equal to the directory name |
| `title` | yes | 1–80 characters |
| `server.command` | yes | 1–300 characters, starts with `uvx ` or `npx ` |
| `server.bin` | yes | `^[A-Za-z0-9._-]{1,64}$` |
| `server.lock` | yes | `requirements.txt` (command starts with `uvx`) or `package-lock.json` (command starts with `npx`, and `package.json` is in the directory). The file must exist. |
| `server.static_env` | no | Up to 32 pairs. Name `^[A-Z][A-Z0-9_]{0,63}$`, value up to 1024 characters. |
| `fields` | yes | 1–16 objects: `name`, `type` `url` or `string`, `secret`, `hint` (1–200), optional `to` (`env` or `{arg: "--flag"}`), optional `max_length` 1–4096. |
| `variants` | yes | 1–8 objects. `name` matches `^[a-z][a-z0-9_]{0,20}$`. Optional `title` (1–80). `fields` names fields of this variant. Optional `tools` (1–16 fnmatch patterns; omitted means every tool). Optional `allowed_hosts` (1–16 host patterns, only when the variant has a `url` field). `checks` is 1–4 items. |
| `variants[].checks[]` | yes | Exactly one of `http` or `tool`. `http`: `method` `GET` or `POST`, `url`, optional `auth` (`basic` of two fields, `bearer` of one field, or `header` with `name`, optional `scheme`, and `field`), optional `headers`, `expect_status`, optional `identity`, optional `timeout_seconds` 1–30. `tool`: `name`, optional `arguments`, optional `timeout_seconds` 1–30. |
| `tools` | no | `strip_prefix`, `normalize` `none` or `snake`, `rename`, `hide`. |
| `discovery` | yes | `mode` `placeholder` or `manual`. Optional `env`. In `placeholder` mode, `env` must supply every field of at least one variant, and each URL value's host must end with `.invalid`. |
| `identity_to_child` | no | `none` (default) or `meta`. |

Further checks: field names are unique, variant names are unique, and two variants cannot list the same set of fields. Every name used in a variant's fields, in `auth`, or as `{FIELD}` in a check URL or header must exist. `{FIELD}` in a URL or header must be a non-secret field of that variant. The credential in `auth` (the second `basic` field, `bearer`, or `header.field`) must be a secret field of that variant; the first `basic` field is the username and may be non-secret. A secret field with `to: {arg: ...}` is rejected: `secret field <name> cannot be passed as an argument`.

Worked example, Atlassian (Jira and Confluence). The same file is `tests/data/packages/atlassian/provider.yaml`. Its `requirements.txt` in the test tree is a stub so the presence check passes; the real lock arrives with the provider package.

```yaml
schema_version: 1
name: atlassian
title: Atlassian (Jira and Confluence)
server:
  command: "uvx mcp-atlassian==0.23.1"
  bin: mcp-atlassian
  lock: requirements.txt
  static_env:
    READ_ONLY_MODE: "false"
fields:
  - {name: JIRA_URL, type: url, secret: false, hint: "Jira address, for example https://example.atlassian.net"}
  - {name: JIRA_USERNAME, type: string, secret: false, hint: "Account email (Atlassian Cloud)"}
  - {name: JIRA_API_TOKEN, type: string, secret: true, hint: "API token from id.atlassian.com, Security, API tokens"}
  - {name: JIRA_PERSONAL_TOKEN, type: string, secret: true, hint: "Personal access token (Jira Server or Data Center)"}
  - {name: CONFLUENCE_URL, type: url, secret: false, hint: "Confluence address, for example https://example.atlassian.net/wiki"}
  - {name: CONFLUENCE_USERNAME, type: string, secret: false, hint: "Account email (Atlassian Cloud)"}
  - {name: CONFLUENCE_API_TOKEN, type: string, secret: true, hint: "API token from id.atlassian.com (the same token works for Jira)"}
  - {name: CONFLUENCE_PERSONAL_TOKEN, type: string, secret: true, hint: "Personal access token (Confluence Server or Data Center)"}
variants:
  - name: cloud
    title: Jira and Confluence, Atlassian Cloud
    fields: [JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN, CONFLUENCE_URL, CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN]
    allowed_hosts: ["*.atlassian.net"]
    checks:
      - http:
          method: GET
          url: "{JIRA_URL}/rest/api/2/myself"
          auth:
            basic: [JIRA_USERNAME, JIRA_API_TOKEN]
          expect_status: [200]
          identity: displayName
      - http:
          method: GET
          url: "{CONFLUENCE_URL}/rest/api/user/current"
          auth:
            basic: [CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN]
          expect_status: [200]
          identity: displayName
  - name: jira_cloud
    fields: [JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN]
    tools: ["jira_*"]
    allowed_hosts: ["*.atlassian.net"]
    checks:
      - http:
          method: GET
          url: "{JIRA_URL}/rest/api/2/myself"
          auth:
            basic: [JIRA_USERNAME, JIRA_API_TOKEN]
          expect_status: [200]
          identity: displayName
  - name: confluence_cloud
    fields: [CONFLUENCE_URL, CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN]
    tools: ["confluence_*"]
    allowed_hosts: ["*.atlassian.net"]
    checks:
      - http:
          method: GET
          url: "{CONFLUENCE_URL}/rest/api/user/current"
          auth:
            basic: [CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN]
          expect_status: [200]
          identity: displayName
  - name: server
    title: Jira and Confluence, Server or Data Center
    fields: [JIRA_URL, JIRA_PERSONAL_TOKEN, CONFLUENCE_URL, CONFLUENCE_PERSONAL_TOKEN]
    checks:
      - http:
          method: GET
          url: "{JIRA_URL}/rest/api/2/myself"
          auth:
            bearer: JIRA_PERSONAL_TOKEN
          expect_status: [200]
          identity: displayName
      - http:
          method: GET
          url: "{CONFLUENCE_URL}/rest/api/user/current"
          auth:
            bearer: CONFLUENCE_PERSONAL_TOKEN
          expect_status: [200]
          identity: displayName
  - name: jira_server
    fields: [JIRA_URL, JIRA_PERSONAL_TOKEN]
    tools: ["jira_*"]
    checks:
      - http:
          method: GET
          url: "{JIRA_URL}/rest/api/2/myself"
          auth:
            bearer: JIRA_PERSONAL_TOKEN
          expect_status: [200]
          identity: displayName
  - name: confluence_server
    fields: [CONFLUENCE_URL, CONFLUENCE_PERSONAL_TOKEN]
    tools: ["confluence_*"]
    checks:
      - http:
          method: GET
          url: "{CONFLUENCE_URL}/rest/api/user/current"
          auth:
            bearer: CONFLUENCE_PERSONAL_TOKEN
          expect_status: [200]
          identity: displayName
tools:
  normalize: none
discovery:
  mode: placeholder
  env:
    JIRA_URL: https://discovery.invalid
    JIRA_USERNAME: discovery@example.invalid
    JIRA_API_TOKEN: discovery-placeholder
    CONFLUENCE_URL: https://discovery.invalid/wiki
    CONFLUENCE_USERNAME: discovery@example.invalid
    CONFLUENCE_API_TOKEN: discovery-placeholder
identity_to_child: none
```

## Security model

Who may call a tool is decided by the door's policy. The box keeps no role list of its own.

The signed identity assertion is forwarded to the child only when the provider sets `identity_to_child: meta`. The default is `none`.

A secret field cannot be passed as a command-line argument. Credential values travel in the child's environment, and only in the explicit list the wrapper builds for that profile. The child never receives the wrapper's environment.

```
PID 1  tini                                 (root; reaps orphaned children)
 └─ stdio-box launch                        (root, short-lived)
     ├─ spawner                             (root; spawn, signal, and reap over a socketpair)
     └─ wrapper  uid 10001, non-dumpable    (HTTP :8081 — /mcp, /creds/, /healthz)
          ↕ stdin/stdout/stderr passed across by the spawner
         child  uid 20000+slot              (one profile's server; HOME=/tmp/box/slot-N)
```

The wrapper calls `prctl(PR_SET_DUMPABLE, 0)` as soon as it starts, so `/proc/<wrapper>/environ` is not readable by another process of uid 10001. After `exec`, a child is dumpable again; what protects the next profile is that the child runs as `20000 + slot`, not as the wrapper.

A child can see its own profile values, its slot directory `/tmp/box/slot-N` (mode `0700`), and the server code under `/cache` (readable and executable, not writable). It cannot see the store key, the database under `/data` (mode `0700`, owner 10001), or another profile's environment. It cannot change the code a later profile's child will run: `/cache` is owned by uid 10002, and the spawner never runs a child as that user.

The spawner is the only process that stays root. It does not listen on the network and does not read `/data`. The wrapper asks it to spawn, signal, and reap over the socketpair inherited across `exec`. There is no control socket on the filesystem.

`keystonectl` sets `security_opt: no-new-privileges:true` and does not set `user:` or drop capabilities. `no-new-privileges` does not block this layout: root drops uid with `setuid`, not with a setuid binary.

If an override sets `user:` (the process is not root) or drops `SETUID`/`SETGID`, the box keeps running in `shared_uid` mode and does not start the root spawner. Children then share the wrapper's uid, the log contains `child_isolation_unavailable` once, and `isolation` is `shared_uid`. Dropping those capabilities leaves the process root, because it can no longer call `setuid` to become uid 10001 either. In that mode a uid-wide reap would kill the wrapper, so cleanup is only `killpg` of the child's process group and recreating `/tmp/box/slot-N`. A descendant that called `setsid` can outlive that.

Slot numbers are reused, and with them the uid `20000 + slot`. Before every spawn into a slot, and after every stop, the spawner reaps that uid: a helper process setuid to the slot sends `SIGKILL` to every process it can see (this container's PID namespace), then deletes that uid's entries at the top of `/tmp` and `/dev/shm` without following symlinks, and recreates the slot directory. If processes remain after 5 seconds the slot is quarantined until the container restarts and is not handed out again.

Each child sets `RLIMIT_NPROC` to 512 and `RLIMIT_CORE` to 0 before `exec`. Core dumps would contain tokens. There is no address-space limit: `RLIMIT_AS` breaks Node; an operator can set the container memory limit with an override. Without user namespaces the process count is per uid on the whole host, so two boxes on one machine share the 512 processes of a slot with the same number.

Child stderr is logged at info as `stdio_box.child.stderr` with `profile`, `generation`, and `slot`. Lines are masked before they are written: every profile field value of 6 characters or more becomes `***`, then well-known token shapes (GitHub, GitLab, Slack, cloud keys, Atlassian, Notion, Yandex OAuth, Tavily). Lines longer than 2000 characters are cut. A token bucket keeps 50 lines per second with a burst of 200; the rest increment `stderr_suppressed_total` and, once a second, produce a warning `suppressed N stderr line(s)`.

Children share the container network. Third-party code can open connections to other packages' facades on the `keystone-mcp-upstreams` network and to addresses outside the host. Those facades still require their own bearer; the network path itself is open.

## Releasing

`IMAGE` in the repository root is the pinned reference `ghcr.io/keystone-codes/stdio-box:<version>@sha256:<digest>`. Until the first release it has no digest line.

1. Run the `Release image` workflow (`workflow_dispatch`) with a version that matches `pyproject.toml`. The workflow tests, builds `linux/amd64` and `linux/arm64`, and pushes `ghcr.io/keystone-codes/stdio-box:<version>` with provenance and an SBOM.
2. It opens a pull request that writes the digest reference into `IMAGE`.
3. Merging that pull request to `main` runs `Tag release`, which checks the registry digest against `IMAGE`, tags `v<version>`, and publishes a GitHub release.

Package manifests pin the same digest in a later change. Package tags use `<name>/v<version>`.

## Development

Requires Python 3.12, uv, and Docker.

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
docker build .
```

`pytest -q` includes the image-layout test, which builds an image from the echo provider under `tests/data/packages`. `pytest -q -m "not integration"` skips it. `credsctl` prints usage and exits 2 until profile commands exist.
