# atlassian

Jira and Confluence behind Keystone. One package runs both products through mcp-atlassian 0.23.1. Each person, service, or role uses its own account. The box does not check roles.

The server is not stored in the image. On first start the box installs `uvx mcp-atlassian==0.23.1` from `requirements.txt` into the `cache` volume. Later starts reuse that volume.

mcp-atlassian is MIT licensed: https://github.com/sooperset/mcp-atlassian

## Install

From a catalog that points at this directory with `path`:

```sh
keystonectl service apply atlassian
```

The installer asks for the domain that serves the secret form, and for a certificate. The public site exposes `/creds/` and `/healthz`. Without a domain yet:

```sh
keystonectl service apply atlassian --defer-public
```

Then load an account with `credsctl` (below) instead of the form.

`BOX_PROVIDER` and `BOX_COMMAND` have defaults. The installer does not ask for them. Do not put box limits (`BOX_MAX_INSTANCES` and the other `BOX_*` settings) in this manifest. Change those in `packages/atlassian.override.yml` if you need to.

The image line is a placeholder of 64 zero digits until the stdio-box image release records the digest.

## Choose an account variant

| Variant | Use it when | Fields | Tools |
|---|---|---|---|
| `cloud` | Jira and Confluence on Atlassian Cloud | `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`, `CONFLUENCE_URL`, `CONFLUENCE_USERNAME`, `CONFLUENCE_API_TOKEN` | every tool |
| `jira_cloud` | Jira Cloud only | `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` | `jira_*` |
| `confluence_cloud` | Confluence Cloud only | `CONFLUENCE_URL`, `CONFLUENCE_USERNAME`, `CONFLUENCE_API_TOKEN` | `confluence_*` |
| `server` | Jira and Confluence on Server or Data Center | `JIRA_URL`, `JIRA_PERSONAL_TOKEN`, `CONFLUENCE_URL`, `CONFLUENCE_PERSONAL_TOKEN` | every tool |
| `jira_server` | Jira Server or Data Center only | `JIRA_URL`, `JIRA_PERSONAL_TOKEN` | `jira_*` |
| `confluence_server` | Confluence Server or Data Center only | `CONFLUENCE_URL`, `CONFLUENCE_PERSONAL_TOKEN` | `confluence_*` |

Cloud variants accept only hosts matching `*.atlassian.net`. A call to a tool outside the variant's set is `tool_not_in_profile`. The child server is not started for that call.

`JIRA_URL` and `CONFLUENCE_URL` must be `https` unless the operator sets `BOX_ALLOW_HTTP_URLS`. Private addresses are refused unless `BOX_ALLOW_PRIVATE_URLS` is set.

## First profile from chat

Tool names at the door carry the `atlassian_` prefix.

1. `atlassian_creds_profile_create` with a profile name, a variant, and the non-secret fields (the URLs, and the Cloud usernames). Secret fields are not accepted in chat.
2. Open the link the tool returns and submit the tokens. The page names the account it reached.
3. `atlassian_creds_bind` with `user:<email>`, `role:<role>`, or `default`, and the profile id from the create call (`atlassian/<name>`).

## Move an account from deploy

A `jira.env` file can be piped in. The variant is the one whose fields are exactly the non-empty known keys. A file with both Jira and Confluence Cloud keys selects `cloud`. A file with only the Jira Cloud keys selects `jira_cloud`. Unknown keys such as `JIRA_SSL_VERIFY` need `--ignore-unknown`.

```sh
credsctl profile import atlassian/shared --from-file - --if-absent --ignore-unknown < jira.env
```

`--if-absent` leaves an existing profile untouched. Bind it separately, for example `credsctl bind default atlassian/shared`.

## Roles

`stdio-box policy-example --namespace atlassian` prints `policy.example.yaml`. keystonectl prints that file and never merges it into your policy.

The example defines three roles:

- `atlassian_reader` reads Jira and Confluence and may call `atlassian_creds_whoami` and `atlassian_creds_providers`.
- `atlassian_writer` reads and changes Jira and Confluence and may call those same two credential tools.
- `atlassian_box_admin` may call `atlassian_creds_*`.

The box does not check roles. Whoever the policy lets call `atlassian_creds_*` manages everyone's accounts in this box. Do not give a non-administrator role `atlassian_*`: that pattern also matches `atlassian_creds_*`.

Changing credential tools in the example require a signed-in person (`requires_human_identity`). API keys and service accounts may not change accounts.

## Tool names

`tools.snapshot.json` is `tools/list` from mcp-atlassian 0.23.1, taken with the placeholder discovery environment in `provider.yaml` (hosts under `.invalid`). Both groups are present: `jira_*` and `confluence_*`. Those prefixes stay on the published names. `mcp.tools` in the manifest is that list plus the ten `creds_*` tools.

A published name at the door is `atlassian_` plus the tool name, and is at most 64 characters.

## Update the server

1. Change the pin in `requirements.in` and set the same version in `server.command` and `BOX_COMMAND`.
2. Rebuild the lock:

```sh
uv pip compile --universal --generate-hashes --python-version 3.12 requirements.in -o requirements.txt
```

3. Install that lock with `uv pip install --require-hashes --no-deps --only-binary :all:` on `linux/amd64` and `linux/arm64`. If a dependency has no wheel for either platform, stop. This release does not allow an exception to `--only-binary`.
4. Take a new snapshot with `keystonectl package wrap-stdio packages/atlassian --image stdio-box:dev`, or the same `snapshot-tools` run inside that image.
5. Raise the package version. A new server build with the same tool names is a patch. Renaming or removing a tool is a new major version.
