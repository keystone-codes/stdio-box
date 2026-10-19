# stdio-box

stdio-box is one container image that runs a third-party MCP stdio server behind the Keystone door. The wrapper is the same for every server; each server is a provider directory under `packages/`.

Callers reach the box only through Keystone. The door checks the person or service account, and the box checks the door's bearer token and signed identity before it forwards a tool call.

## How Keystone calls the box

Only the door calls the box. A person reaches it through an agent signed in at the door. A service reaches it as a client of the door, with that service's own service account: the assertion then has `sub` of the form `svc:<name>` and `sub_type` of `service`. The box does not read `X-Author-Email`.

`stdio-box serve` listens on `0.0.0.0` and `BOX_PORT`. A missing or unacceptable setting exits 2, one line per problem: `config error: <NAME> is required`.

| Variable | Default | Accepted value |
|---|---|---|
| `BOX_BEARER_TOKEN` | | at least 32 bytes |
| `BOX_IDENTITY_SECRET` | | at least 32 bytes |
| `BOX_IDENTITY_AUDIENCE` | | non-empty; the registry entry name |
| `BOX_PUBLIC_BASE_URL` | | an `https://` URL, including `https://public-domain-pending.invalid` |
| `BOX_STORE_KEY` | | at least 32 bytes |
| `BOX_PROVIDER` | | `^[a-z][a-z0-9_-]{0,30}$` |
| `BOX_PROVIDER_DIR` | `/opt/box/packages/<BOX_PROVIDER>` | directory containing `provider.yaml` and `tools.snapshot.json` |
| `BOX_COMMAND` | | non-empty |
| `BOX_PORT` | `8081` | integer from 1 to 65535 |
| `BOX_DATA_DIR` | `/data` | |
| `BOX_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `BOX_ALLOWED_HOSTS` | empty | comma-separated host names |
| `BOX_DEBUG` | unset | `1` enables `debug-spawn` |
| `BOX_CHILD_ARGV_OVERRIDE` | unset | child program path, used only when `BOX_DEBUG=1` |
| `BOX_MAX_INSTANCES` | `8` | integer from 1 to 32 |
| `BOX_IDLE_SECONDS` | `600` | integer from 1 to 31536000 |
| `BOX_START_TIMEOUT_SECONDS` | `60` | integer from 1 to 31536000 |
| `BOX_CALL_TIMEOUT_SECONDS` | `120` | integer from 1 to 31536000 |
| `BOX_AUDIT_RETENTION_DAYS` | `30` | integer from 1 to 3650 |
| `BOX_ALLOW_HTTP_URLS` | `false` | `true` allows an `http` URL in a check |
| `BOX_ALLOW_PRIVATE_URLS` | `false` | `true` allows a private, loopback, or link-local address |
| `BOX_ALLOWED_URL_HOSTS` | empty | comma-separated host patterns (`fnmatch`) |
| `BOX_FORM_TTL_SECONDS` | `900` | integer from 60 to 3600 |

`BOX_DEBUG` and `BOX_CHILD_ARGV_OVERRIDE` are spawner settings. A missing value is not an error. `BOX_DEBUG=1` enables `debug-spawn`. Together with `BOX_CHILD_ARGV_OVERRIDE`, it names the child program and `launch` does not install.

Checks on `/mcp`, in order:

1. `Host`, with or without a port, must be `BOX_IDENTITY_AUDIENCE`, that name plus `-mcp`, `localhost`, `127.0.0.1`, or a name from `BOX_ALLOWED_HOSTS`. Otherwise HTTP 421 and `{"error":"misdirected_request"}`.
2. `Authorization: Bearer` must match `BOX_BEARER_TOKEN`. The comparison does not stop early on length. Otherwise HTTP 401 and `WWW-Authenticate: Bearer`. The MCP parser does not run.
3. `tools/list` carries no identity. The box returns the published tools from `tools.snapshot.json`, then the eleven `creds_*` tools. It does not start the provider server. Only `creds_snapshot_tools` asks the server for a fresh list, and it does that under the caller's own profile.
4. `tools/call` requires `X-Keystone-Identity`: HS256, header `typ` of `keystone-identity+jwt`, audience `BOX_IDENTITY_AUDIENCE`. `mcp_target` must be the tool name on this call. Each `jti` is accepted once, and remembered until its `exp` plus 30 seconds. The memory holds at most 100000 entries; when it is full the call is rejected and nothing already stored is dropped.

A failed tool call is `isError: true` with one text block of JSON: `code`, `message`, and `hint`. `code` is one of `identity_rejected`, `no_credentials_profile`, `tool_not_in_profile`, `profile_needs_reentry`, `instance_unavailable`, `instance_failed`, `instances_exhausted`, `instance_busy`, `upstream_timeout`, `not_available`, `invalid_argument`, `unknown_provider`, `profile_exists`, `profile_not_found`, `profile_in_use`, `binding_exists`, `binding_not_found`, `secret_field_in_chat`, `secret_reentry_required`, `missing_fields`, `form_unavailable`, `check_failed`, `snapshot_invalid`. There is no `credentials_ambiguous` code: when several bindings match, the first in order is the one that applies.

`tools.snapshot.json` is the provider server's `tools/list`, stored as-is:

```json
{
  "schema_version": 1,
  "server": {"command": "uvx echo-server==0.0.0"},
  "taken_at": "2026-10-15T00:00:00Z",
  "tools": []
}
```

The snapshot's `server.command` must equal the provider's `server.command`. Published names come from the child names by `tools.strip_prefix`, then `tools.normalize: snake` (`re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()`), then `tools.rename`, then `tools.hide`. A published name matches `^[a-z][a-z0-9_]{0,63}$` and does not start with `creds_`. Two child tools that publish as one name are an error at startup.

`GET /healthz` does not check the bearer token. A request that carries `X-Forwarded-Host` (the door's proxy, from the public domain) gets the status code and `{}`. Any other request gets `status`, `store`, `isolation`, and `install`. `isolation` is the wrapper's current value: `unknown` until a child starts, then `per_slot_uid` or `shared_uid`. `install` is `unknown` until launch has installed a server; then it is `key`, `at`, `duration_ms`, and `from_cache`. A store schema newer than this process is HTTP 503. A key that does not match is HTTP 200 with `status` `degraded` and `store` `key_mismatch`.

A call of a provider tool is routed to one profile, described under [How a call is routed](#how-a-call-is-routed). `creds_providers` describes the loaded provider. `creds_whoami` reports which profile this caller would use and does not refuse. The other `creds_*` tools are the account tools under [Box tools](#box-tools). `isolation` and `server.installed_key` are `"unknown"` until those checks report.

## How a call is routed

Each call reads the caller's identity again. Nothing about the choice is cached.

1. A binding `user:<sub>` wins. `sub` is lowercased, so `user:Ann@Example.com` matches `ann@example.com`. A service account is the same rule with `sub` `svc:<name>`: the binding is `user:svc:<name>`.
2. When `rbac` is not `off`, role bindings are checked in ascending `position`. The first role that is in the caller's `roles` wins. Other roles of that caller that sit lower are not used. Two roles never refuse the call; the administrator's order decides. When `rbac` is `off`, this step is skipped because the assertion carries no roles.
3. The `default` binding.
4. Otherwise the call is refused with `no_credentials_profile`: "There is no <title> account for you in this box. Ask an administrator to bind one to you or to your role."

The profile's state does not change which binding wins. If the winning profile has no secrets yet, or its secrets cannot be read, the call is `profile_needs_reentry` and does not fall through to a later binding. A profile that is `ready` or `check_failed` is allowed to run.

The tool must be in the profile variant's `tools` patterns (or the variant must list no patterns, which allows every tool). Otherwise the call is `tool_not_in_profile` and the child is not started: "The account <profile> (<variant title>) does not include <tool>. Ask an administrator for an account that covers it."

`creds_whoami` returns the same choice without refusing: `sub`, `sub_type`, `roles`, `rbac`, `profile`, `tier` (`user`, `role`, or `default`), the winning `binding`, `not_used` (lower role bindings that also matched), the variant's tool patterns, `would_refuse`, and a `hint`. When `not_used` is not empty, the hint says the higher binding was used and how to move another up with `creds_bind … --replace --position`.

## Instances

One profile has at most one child. The child starts on the first call and stops when it is idle. The instance key is the profile id and its secret version.

| Limit | Setting | Default | What happens |
|---|---|---|---|
| Live instances | `BOX_MAX_INSTANCES` | 8 (1–32) | A new profile evicts the idle instance with the oldest `last_call_at`. If every instance has a call in flight, the call is `instances_exhausted` (retryable). |
| Idle stop | `BOX_IDLE_SECONDS` | 600 | A background pass every 30 seconds stops an instance with no calls in flight for longer than this. |
| Start | `BOX_START_TIMEOUT_SECONDS` | 60 | `initialize` must answer within this. Otherwise the start counts as a failure. |
| Call | `BOX_CALL_TIMEOUT_SECONDS` | 120 | The call is `upstream_timeout` and the child is sent `notifications/cancelled`. |
| Calls at once on one instance | constant | 8, wait 30 seconds | `instance_busy` (retryable). One slow account does not take the slots of the others. |
| Ping | constants | every 15 seconds, timeout 5 seconds, restart after 3 misses in a row | One missed ping does not restart. The log says `instance <profile> missed ping n/3`. |
| Restart after a crash | constants | 1 second, doubling up to 60 seconds; back to 1 second after 60 seconds of stable running | During the wait the call is `instance_unavailable` with `retry_after_seconds`. |
| Failed starts | constant | 5 in a row, then `failed` | The call is `instance_failed` until a credential check or a rotation clears it. |
| Stop | constants | close the session, then the spawner closes stdin, waits 2 seconds, `SIGTERM`, waits 5 seconds, `SIGKILL` | In-flight calls fail as soon as the session closes. |
| stderr | constants | 50 lines per second, burst 200 | Suppressed lines are counted. Field values of length 6 or more are masked. |
| Child environment | — | `PATH`, `HOME=/tmp/box/slot-N`, `TMPDIR`, `LANG=C.UTF-8`, `server.static_env`, and the variant's fields | The wrapper's environment is not inherited. A field that is not part of the variant is not passed. |
| Slot cleanup | — | before the slot is given out, and after the child stops | A slot whose cleanup failed is quarantined and is not given out again. |

The wrapper also reads `meta.revision` every 2 seconds. If a live profile was deleted, its version changed, or it now needs its secrets again, that instance is stopped (a version change lets in-flight calls finish for up to 30 seconds). `credsctl` therefore applies without restarting the container. Audit rows older than `BOX_AUDIT_RETENTION_DAYS` are deleted once an hour. A status snapshot is written about every 10 seconds for `credsctl status`.

## Changing limits

Set the `BOX_*` limits in the package override, under `environment:`. The file is `packages/<package>.override.yml`.

```yaml
environment:
  BOX_MAX_INSTANCES: "4"
  BOX_IDLE_SECONDS: "300"
  BOX_START_TIMEOUT_SECONDS: "60"
  BOX_CALL_TIMEOUT_SECONDS: "120"
  BOX_AUDIT_RETENTION_DAYS: "30"
```

A value outside the accepted range is a config error. The process exits 2 and names the variable. The ping, backoff, and per-instance concurrency numbers are not variables.

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

`stdio-box launch` is the process the image starts. It fixes the directory owners, installs the provider server into `/cache` under uid 10002 when setuid works, then splits into the root spawner and the wrapper. A missing `BOX_COMMAND` exits 2. A failed install logs `provider_install_failed` and exits 3. `BOX_DEBUG=1` together with `BOX_CHILD_ARGV_OVERRIDE` skips installation and uses that program as the child. `stdio-box serve` without spawner arguments is the HTTP wrapper. `stdio-box serve --spawner inline` or `--spawner-fd` is the wrapper process `launch` execs. `stdio-box install` and `stdio-box snapshot-tools` install without that split.

| Path | Owner | Mode | Use |
|---|---|---|---|
| `/data` | uid 10001 (`box`) | `0700` | Profile store |
| `/cache` | uid 10002 (`box-install`) | `0755` | Installed server, written only by the installer |
| `/tmp/box` | root | `0711` | Per-slot homes for children |

The image does not set `USER`. `launch` starts as root so it can drop to these uids.

## Installing the server

The image does not contain the provider's server. `BOX_PROVIDER` selects `/opt/box/packages/<BOX_PROVIDER>` (or `BOX_PROVIDER_DIR` when that is set). `BOX_COMMAND` names the server and its exact version. The box parses that string with `shlex`. It does not run it in a shell, and it does not invoke `uvx` or `npx` while the container is up.

Grammar:

- `uvx <distribution>==<version> [arguments…]`
- `npx [-y|--yes] <package>@<version> [arguments…]` (`<package>` may be `@scope/name`)

The version is exact. A Python version must be a single `packaging` version. An npm version must be `X.Y.Z` or `X.Y.Z-<prerelease>`. Ranges, `latest`, and `^ ~ > < * x` are rejected (`box_command_invalid`). A flag before the package (`--from`, `--with`, `-p`, and any other) is rejected: those flags would skip the lock file. Arguments after the package are kept for the child process.

Before installation, the command is checked against the provider lock (`box_command_not_locked` when it does not match):

- Python: `requirements.txt` has a line for the normalized distribution name at that exact version, with at least one `--hash=sha256:`, and every requirement line has a sha256 hash.
- Node: `package.json` `dependencies` is exactly that package and version, `package-lock.json` is lockfileVersion 3, `packages["node_modules/<package>"].version` is that version, and every downloaded package has `integrity`.

The installer runs as uid 10002 and writes `/cache/<provider>/<key>/`. The key is the first 16 hex characters of a SHA-256 over the lock bytes, `package.json` for Node, the installer kind, and the installer version baked into the image. If `<key>/.complete` already contains that key, the tree is reused and the network is not used. The first start needs a route to the index, or to a mirror. Later starts do not.

Mirrors are visible only to the installer. `BOX_PIP_INDEX_URL` is passed as `UV_INDEX_URL`. `BOX_NPM_REGISTRY` is passed as `npm_config_registry`. When either is set it must start with `https://`. Hashes are checked for a mirror the same way as for the public index.

`BOX_INSTALL_TIMEOUT_SECONDS` defaults to 240 and must be from 30 to 1800. A failed install logs `provider_install_failed` and `launch` exits 3. Docker's `restart: unless-stopped` starts the container again, and the install is tried again.

A machine with no network and no mirror can be prepared by installing once where the index is reachable, then copying the Docker volume `ks-pkg-<name>-cache`. This release does not publish an image with the server already installed.

The installed tree is owned by uid 10002. Directories are `0755`. Files are not writable by the group or by other users, so a child (uid `20000+slot`) can run the server and cannot change it. Nothing except the installer runs as uid 10002.

If the container cannot change uid (`user:` in an override, or the capabilities that `setuid` needs have been dropped), the installer runs without changing uid and the log records `install_isolation_unavailable`. In that `shared_uid` mode the spawner does not start, children share the wrapper's uid, and a child can modify the cache.

`stdio-box install [--provider-dir DIR] [--cache DIR]` runs the same install without `launch`. When the caller is not root, it does not try to change uid.

`stdio-box snapshot-tools` installs into a temporary cache, starts the server once, and writes `tools/list`. `keystonectl package wrap-stdio` runs it as `docker run --rm --entrypoint stdio-box -v <directory>:/opt/box/provider:ro -e BOX_PROVIDER_DIR=/opt/box/provider <image> snapshot-tools --out -`.

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

## Adding a provider

A provider is a directory `packages/<name>/`. The image build copies the directory's data (`provider.yaml`, the lock, `package.json` when the server is Node, and `tools.snapshot.json`) to `/opt/box/packages/<name>/`. Manifests and the provider README stay out of the image.

Build the lock in that directory:

```sh
uv pip compile --universal --generate-hashes --python-version 3.12 requirements.in -o requirements.txt
npm install --package-lock-only
```

`uv pip compile --universal` records wheels for more than one platform, and `uv pip install` honors those markers. Do not hand-write `tools.snapshot.json`. `keystonectl package wrap-stdio` runs `snapshot-tools` in this image and writes the file. A server that does not answer `tools/list` with placeholder values can be listed by a running box instead: `creds_snapshot_tools` takes the list under an account someone already entered on the form, and `credsctl snapshot export <id>` prints it. No secret has to be written to a file for that. Commit the lock and the snapshot with `provider.yaml`.

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

## Storage

Profiles and bindings live in `<BOX_DATA_DIR>/box.sqlite3` (in the image, `/data/box.sqlite3`). The file is created mode `0600`. The database uses WAL. Tables:

| Table | What it holds |
|---|---|
| `meta` | schema version, salt, key check, revision |
| `profiles` | id, variant, non-secret fields, state, check result |
| `profile_secrets` | secret fields, AES-256-GCM, one row per version |
| `bindings` | who receives which profile, and the role order |
| `form_links` | SHA-256 of a form token, never the token |
| `audit` | calls and admin actions, kept for `BOX_AUDIT_RETENTION_DAYS` (30) |
| `tool_snapshots` | the newest five `tools/list` snapshots taken by `creds_snapshot_tools`: id, `sha256`, `taken_at`, profile, variant, the binding subject, and the JSON |
| `status` | one JSON snapshot written by the running wrapper |

`BOX_STORE_KEY` is the key generated for the package (`secrets.token_urlsafe(32)`). The store derives separate 32-byte keys with HKDF-SHA256 for secrets, the key check, form CSRF, and audit subject hashes. Secret fields of one profile are a single JSON object sealed with AES-256-GCM. The nonce is random. The additional data is the profile id and the secret version, so a row cannot be moved under another profile or version.

What this protects: a copy of the data volume without `BOX_STORE_KEY`. The secret fields cannot be opened.

What this does not protect:

- Non-secret fields (the address, the account name) are stored in the clear, as are profile ids, bindings, and audit rows.
- The key is in the container environment. `docker inspect` shows it. Anyone who can read the running container can read the store.
- Encryption does not hide which profile was used, only the secret values.

A database written by a newer build is not opened and is not modified. An internal `/healthz` then answers 503. A request that arrived through the public domain (`X-Forwarded-Host`) gets the same status code and an empty body.

## Box tools

An administrator configures the box from chat. These eleven tools are part of the server's catalog. Secret values never travel in either direction: a tool result, a refusal, a log line, and an audit row never contain one. The person who owns the account types secrets on the one-time form.

| Tool | What it does |
|---|---|
| `creds_providers` | The provider in this box: variants, which fields are secret, `server.installed_key`, and `isolation`. |
| `creds_profiles` | Profiles, their non-secret fields, last check, bindings, and the instance if one is running. |
| `creds_bindings` | Every binding in the order a call is resolved: `user`, then `role` by position, then `default`. Each row includes the profile's state. |
| `creds_profile_create` | Create a profile. When the variant has secret fields, the result is a one-time link. |
| `creds_profile_rotate` | Change non-secret fields, or issue a new link so the secrets can be entered again. |
| `creds_profile_delete` | Delete a profile that nothing is bound to, and stop its instance. |
| `creds_bind` | Bind `user:<email>`, `user:svc:<name>`, `role:<role>`, or `default` to a profile. |
| `creds_unbind` | Remove one binding. Role positions below it move up. |
| `creds_whoami` | Which profile this caller would use, which binding won, and which other role bindings were skipped. |
| `creds_check` | Run the variant's checks and start the instance. Both have to succeed. |
| `creds_snapshot_tools` | Take the server's `tools/list` under the caller's own profile and store the snapshot in the box. |

### `creds_snapshot_tools`

Takes no arguments. The profile is the one a provider call of this caller would use, by the same order as [How a call is routed](#how-a-call-is-routed): the profile `creds_whoami` shows. No argument can name another profile. A caller with no profile gets `no_credentials_profile`; a profile without readable secrets gets `profile_needs_reentry`. The box does not ask for a signed-in person: the tool only reads, under the caller's own account. Who may call it is the policy's decision; `<ns>_box_admin` covers it through `<ns>_creds_*`, and it is not in `requires_human_identity`. Annotations: `readOnlyHint: true`, `openWorldHint: true`.

The box starts one short-lived server on a free slot with that profile, calls `tools/list`, and stops it. When every slot is in use the call is `instances_exhausted`; a server that does not start or does not list its tools is `instance_failed`; a listing that does not arrive within `BOX_CALL_TIMEOUT_SECONDS` is `upstream_timeout`. A listing whose names cannot be published under the provider's `tools` rules is `snapshot_invalid` and is not stored.

The stored snapshot is the same document `stdio-box snapshot-tools --out -` writes: `schema_version`, `server.command`, `taken_at`, and `tools` exactly as the server listed them. The `tools` rules are not applied to it; they are applied when the box loads a `tools.snapshot.json`. The box keeps the newest five snapshots in the table `tool_snapshots` and deletes older ones. `credsctl snapshot list` and `credsctl snapshot export <id>` read them.

Result:

```json
{
  "id": 3,
  "sha256": "5f0c…e1a2",
  "tools": ["get_issue", "search", "create_issue"],
  "profile": "atlassian/shared",
  "variant": "cloud",
  "variant_filters_tools": true
}
```

`tools` are the published names after the provider's `tools` rules (`strip_prefix`, `normalize`, `rename`, `hide`), without the namespace and without the variant's patterns. `variant_filters_tools` is `true` when the profile's variant lists `tools` patterns: that account may see a narrower set, and a package built from this snapshot publishes that set to everyone. `sha256` is the hex SHA-256 of the snapshot without `taken_at`, as `json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` encoded as UTF-8, so two snapshots of the same server have the same hash. The result, the log, the audit row, and the table never contain a secret value.

`creds_profile_create` and `creds_profile_rotate` accept only non-secret fields. Naming a secret field, in any letter case, is refused and the value is discarded. A field of type `url` is checked before anything is written. Changing a URL, or any field that a check places in a URL or a header, requires the secrets to be entered again on the form. Stored secrets are never sent to a new address.

## Bindings and their order

The box picks one profile for a call, in this order:

1. `user:<sub>` — the subject from the signed identity, lowercased. `user:svc:backend` is a service account.
2. `role:<name>` bindings whose role is in the call, walked in the order an administrator set. The first hit wins.
3. `default`.

There is no error when several roles match. The administrator sets the order with `credsctl bind <subject> <profile> --position N`. A new role binding without `--position` goes to the end. `--replace --position N` moves an existing role binding and can point it at another profile. Removing a role binding closes the gap so positions stay `1, 2, 3` with no holes. `user:` and `default` have no position.

Profile state does not affect the choice. If the first match needs its secrets entered again, the call uses that profile and fails, instead of quietly using the next account.

`creds_bindings` is that same order, with the profile state on every row. `user` is sorted by subject. `role` is `position` 1, 2, 3 with no holes. `default` is last, or null when it is not set. `creds_whoami` says which row won (`tier` and `binding`, including `position` for a role) and lists the caller's other role rows under `not_used`, because they were lower in the order. `hint` names the binding to move up.

To put a role binding somewhere else, call `creds_bind` again with `replace: true` and the new `position`. The reply for a role includes `order`, the full role list after the change. A new role binding without `position` goes to the end. `position` on `user:` or `default` is refused. `user:` and `default` have no position.

## Recommended policy

The box does not check roles. Whoever the policy lets call `<ns>_creds_*` manages everyone's accounts in this box. That is the administrator's decision and their responsibility. The box does not keep an allow-list of administrators.

Do not give `<ns>_*` to an ordinary role. A pattern is matched against the full published name, so `billing_*` covers **every** tool of namespace `billing`, including `billing_admin_report`. The same pattern on this box also covers `<ns>_creds_profile_delete` and the other account tools. List the tool names the role should have. `stdio-box policy-example --namespace <ns>` prints a reader role and a writer role that do that, and a `<ns>_box_admin` role whose only grant is `<ns>_creds_*`. keystonectl prints that file and does not merge it into the policy.

The five tools that change accounts (`creds_profile_create`, `creds_profile_rotate`, `creds_profile_delete`, `creds_bind`, `creds_unbind`) are listed in `requires_human_identity`. `creds_snapshot_tools` is not: it changes no account and runs under the caller's own profile. `<ns>_box_admin` covers it through `<ns>_creds_*`. Only a signed-in person may change accounts. An API key or a service account must not: those calls act for a machine, and a machine must not be able to rebind the accounts everyone else uses. The door enforces that list. The box does not.

## Moving or reinstalling a box

Copy the store volume and use the same `BOX_STORE_KEY`. The process still starts if the key is different: bindings and non-secret fields stay readable, secret fields do not, and those profiles are reported as `needs_reentry`. An internal `/healthz` answers 200 with `"status": "degraded"` and `"store": "key_mismatch"`. A public-domain request gets 200 and `{}`. Enter the secrets again, or re-encrypt with the old key (below). `sub_hash` values in logs from before the key change do not match values from after it.

## Backups and the key

Before a schema migration the store copies the database to `<BOX_DATA_DIR>/backup/` and keeps the three newest copies. Keep a backup of the volume **separate** from `BOX_STORE_KEY`. The ciphertext is only a secret from someone who does not have the key. The key is also visible in the container environment, so a backup that includes both the volume and the key (or `docker inspect` output) is not protected.

The cache volume does not hold profiles. It does not need the same treatment.

## Changing the key

`credsctl store rekey --old-key-file <path>` (or `-` for stdin) opens every secret row with the old key and seals it with the current `BOX_STORE_KEY`. Rows already sealed with the current key are left as they are. If you no longer have the old key, enter the secrets again; the first successful write adopts the new key, and profiles you have not rewritten stay `needs_reentry`.

## Entering secrets

There are three ways to put an account into the box:

1. A one-time form in the browser, at a link the administrator asked for.
2. `credsctl profile import`, from a mode `0600` env file or from stdin.
3. OAuth done by the person, in a later version. This version has no such flow.

The form is not a login. A link that expires in 15 minutes and works once is enough: the person who opens it types the token, and the box checks that token with the provider before it stores anything. There is no other sign-in on the box.

| Request | Response |
|---|---|
| `GET /creds/<token>` while the token is live | 200, an HTML form: provider and variant titles, profile id, non-secret fields read-only, `type=password autocomplete=off` inputs for the secret fields, a hidden `csrf`. Sets cookie `__Host-box_form=<nonce>; Secure; HttpOnly; SameSite=Strict; Path=/`. |
| `GET` with an unknown, expired, or used token | 404, the same page: "This link is not valid. Ask the administrator for a new one." |
| `POST /creds/<token>`, `application/x-www-form-urlencoded`, at most 16 KiB | Checks below. Success is 200: "Saved. \<title\> says this account is \<identity\>. You can close this page." |
| `POST` when the provider rejects the values | 422, the same form, "\<title\> did not accept these values (HTTP \<code\>).", and one more attempt. The fifth failure is 410. |
| `POST` when the provider cannot be reached | Values are stored, state `ready`, `last_check_ok` null. 200: "Saved, but \<title\> could not be reached to check them." |
| Any other method | 405 |

`POST` is accepted only in this order: the token is live, `Origin` is the origin of `BOX_PUBLIC_BASE_URL` (`Origin: null` is rejected), the cookie is present, `csrf` matches, the body contains only `csrf` and the variant's secret fields (each non-empty, within `max_length`, no line break), and the box has seen fewer than 60 such posts in the last minute. A failure before the provider check is 400, "The form could not be accepted. Open the link again." An unknown token is 404. The rate limit is 429.

`GET` does not use the link up. A link is used up when the values are saved, or after five rejected attempts. One profile has one live link; a new link replaces the previous one. The lifetime is `BOX_FORM_TTL_SECONDS` (default 900, allowed 60–3600).

Every `/creds/` response sends `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`. The page has no script and no external resources. `Referrer-Policy` is `same-origin`, not `no-referrer`: with `no-referrer` a browser sends `Origin: null` on the form post, and the origin check would reject every real submission.

nginx in front of the box sets `Host` to `127.0.0.1` and passes the public name in `X-Forwarded-Host`. `/creds/` accepts `Host` `127.0.0.1`, `localhost`, or the host of `BOX_PUBLIC_BASE_URL`. If `X-Forwarded-Host` is present it must be that public host, otherwise the response is 421.

When `BOX_PUBLIC_BASE_URL` is `https://public-domain-pending.invalid` (`--defer-public`), the form cannot be opened. Enter the account with `credsctl profile import` only.

A check URL has to be `https` unless `BOX_ALLOW_HTTP_URLS=true`. Its host has to match the variant's `allowed_hosts` when that list is set, and `BOX_ALLOWED_URL_HOSTS` when that list is set. Every address the name resolves to has to be public: not loopback, not a private network, not link-local, not a unique-local IPv6 address, and not carrier-grade NAT. `BOX_ALLOW_PRIVATE_URLS=true` turns that address check off for a provider that lives on an internal network; set it only on purpose. The check connects to the address it just resolved and keeps the original name for `Host` and TLS.

With `TLS=manual`, keystonectl's nginx does not write an access log for public paths, but your own proxy will record `/creds/<token>` unless you turn that log off for `/creds/`.

```nginx
location ^~ /creds/ {
    access_log off;
    proxy_pass http://127.0.0.1:8081;
}
```

```caddy
handle /creds/* {
    log {
        output discard
    }
    reverse_proxy 127.0.0.1:8081
}
```

Rotation writes the new values only after the check is not a refusal. Until then the previous version stays as it was.

## credsctl

`credsctl` is the operator CLI. In the container it runs as uid 10001. Started as root, it drops to that uid before it opens the database. Every command accepts `--json`. Field values are never printed. Each command that changes the store writes an audit row `event=admin`, `sub=cli:credsctl`.

```
credsctl profile list
credsctl profile delete <id>
credsctl profile import <id> [--variant <name>] --from-file <path|-> \
    [--if-absent] [--ignore-unknown] [--no-check] [--description <text>]
credsctl bind <subject> <profile> [--replace] [--position N]
credsctl unbind <subject>
credsctl bindings
credsctl store status
credsctl store rekey --old-key-file <path|->
credsctl audit [--since 24h] [--profile <id>] [--event <name>]
credsctl snapshot list
credsctl snapshot export <id>
credsctl status
```

`profile import` reads an env file (`NAME=value`, blank lines and `#` comments skipped, quotes left as typed). The file must be mode `0600` unless the path is `-` (stdin). Without `--variant`, the variant is the one whose fields are exactly the non-empty provider fields in the file. A file that matches none (or more than one) is `variant_ambiguous`. Keys that are not provider fields are `unknown_fields` unless `--ignore-unknown` is set, in which case those names are skipped and listed as `ignored`. `--if-absent` on a profile that already exists exits 4 and writes nothing. Import does not bind the profile; bind is a separate command. Without `--no-check`, the variant's HTTP checks run before anything is written. A refusal exits 1 and leaves the store unchanged. If a check cannot be reached, the import still exits 0 (`check: unreachable`) and `last_check_ok` stays null. `--no-check` stores the values without a check (`check: skipped`, `last_check_ok` null).

`bindings` prints `user`, then `role` with positions, then `default` — the same order a call is resolved in.

`snapshot list` prints the stored tool snapshots, newest first: `id`, `sha256`, `taken_at`, and `profile`, without the JSON. `snapshot export <id>` prints that snapshot's JSON on stdout byte for byte as stored. With `--json`, `result` is `{"id", "sha256", "snapshot"}` and `snapshot` is that same text as a string. An id that is not stored exits 3 with `snapshot_not_found`.

`store status` reports `key_state`, `schema_version`, how many profiles there are, and how many are `needs_reentry`. `status` prints the wrapper's status row. Until the wrapper is running it is `{"status": "not running"}`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Done. An import whose check could not reach the provider is still 0 (`check: unreachable`). |
| 1 | The provider rejected the values. Nothing was written. |
| 2 | Usage. |
| 3 | Invalid input: `unknown_fields`, `missing_fields`, `variant_ambiguous`, file mode, a bad id, subject, or position, `profile_in_use`, `binding_exists`, `snapshot_not_found`. |
| 4 | `--if-absent` and the profile already exists. |
| 5 | The store cannot be opened, including a database newer than this program (`store_schema_newer`). |

`--json` prints one object: `{"ok", "code", "error": {"id", "message"}, "result"}`. `code` is the exit code. `error` is `null` on success.

Moving an account out of a deploy env file, then binding it as the default. `<service>` is the `service` field of the container whose `role` is `mcp`, from `keystonectl service status <name> --json` (`result.services[].containers[]`). Do not build that name from the package name.

```sh
docker compose exec -T <service> \
  credsctl profile import atlassian/shared --from-file - --if-absent --ignore-unknown \
  < jira.env
docker compose exec -T <service> credsctl bind default atlassian/shared
```

## Box tools of this image

`stdio-box box-tools` prints the names of this image's own `creds_*` tools as one JSON array on one line and exits 0:

```json
["creds_providers", "creds_profiles", "creds_bindings", "creds_profile_create", "creds_profile_rotate", "creds_profile_delete", "creds_bind", "creds_unbind", "creds_whoami", "creds_check", "creds_snapshot_tools"]
```

It reads no configuration. Use it to learn which box tools an image has instead of reading its tag.

## Releasing

`IMAGE` in the repository root is the pinned reference `ghcr.io/keystone-codes/stdio-box:<version>@sha256:<digest>`. Until the first release it has no digest line. Package manifests pin that same reference on `containers.mcp.image`. A package that is not part of the release keeps its own image.

1. Run the `Release image` workflow (`workflow_dispatch`) with a version that matches `pyproject.toml`. The workflow tests, builds `linux/amd64` and `linux/arm64`, and pushes `ghcr.io/keystone-codes/stdio-box:<version>` with provenance and an SBOM.
2. It runs `scripts/pin_digest.py` with that image reference and opens a pull request. The pull request writes the reference into `IMAGE` and into `containers.mcp.image` of each package whose image is still the zero digest or a previous `stdio-box` release. The description lists those packages and their versions.
3. Merging that pull request to `main` runs `Tag release`. It checks the registry digest against `IMAGE`, tags `v<version>`, and for each manifest whose `image` equals `IMAGE` tags `<name>/v<version>` when that tag does not exist yet. An existing package tag is left where it is. The same package version with different contents fails the workflow, because a tag is immutable. The GitHub release records the image digest and the package tags.

The next change to a provider is a new image version and a new patch version of that package.

## Development

Requires Python 3.12, uv, and Docker.

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
docker build .
```

`pytest -q` includes the image-layout test, which builds an image from the echo provider under `tests/data/packages`. `pytest -q -m "not integration"` skips it.
