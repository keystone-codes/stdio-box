# Yandex Tracker provider package

Yandex Tracker tools (`@fractalizer/mcp-server-yandex-tracker` 5.1.0) behind Keystone. Each person, service, or role uses its own Tracker account. The server code is installed from the lock file into the box `cache` volume on first start; it is not baked into the image.

## Install

```sh
keystonectl service apply yandex-tracker
```

Answer the domain and certificate questions (or use `--defer-public` if the public URL is not ready yet). The manifest pins the shared `stdio-box` image and sets `BOX_PROVIDER=yandex-tracker` and `BOX_COMMAND=npx -y @fractalizer/mcp-server-yandex-tracker@5.1.0`.

The door namespace is `yandex_tracker`.

## Credential variants

Set one organization id, not both. That matches `mcp/env/yandex-tracker.env.example` in deploy.

| Variant | Use it when | Fields | Check |
|---|---|---|---|
| `org` | Yandex 360 | `YANDEX_TRACKER_TOKEN`, `YANDEX_ORG_ID` | `GET https://api.tracker.yandex.net/v2/myself` with `Authorization: OAuth` and `X-Org-ID` |
| `cloud_org` | Yandex Cloud | `YANDEX_TRACKER_TOKEN`, `YANDEX_CLOUD_ORG_ID` | the same URL with `Authorization: OAuth` and `X-Cloud-Org-ID` |

The check reads the top-level `display` string (the name Tracker shows for the account). The current reference documents `GET /v3/myself` as the current method and still accepts `v2`; both return that `display` field ([Getting information about the current user](https://yandex.ru/support/tracker/en/api-ref/users/get-user-info), [common request format](https://yandex.ru/support/tracker/en/api-ref/common-format)). This package calls `v2/myself`, which that reference still accepts. Find the organization id under Tracker, Administration, Organizations.

Create an OAuth token at [oauth.yandex.ru](https://oauth.yandex.ru/) with Tracker access.

## First profile

From chat (after the box is healthy). Tool names at the door carry the `yandex_tracker_` prefix.

1. `yandex_tracker_creds_profile_create` with a profile name, variant `org` or `cloud_org`, and the organization id. The token is not accepted in chat.
2. Open the link the tool returns and submit `YANDEX_TRACKER_TOKEN`. The page names the account it reached (`display`).
3. `yandex_tracker_creds_bind` with `user:<email>`, `role:<role>`, or `default`, and the profile id (`yandex-tracker/<name>`).

## Move from deploy `yandex-tracker.env`

A file with `YANDEX_TRACKER_TOKEN` and `YANDEX_ORG_ID` selects `org`. A file with `YANDEX_TRACKER_TOKEN` and `YANDEX_CLOUD_ORG_ID` selects `cloud_org`. Leave the unused organization id commented out, as in the deploy example. Unknown keys need `--ignore-unknown`.

```sh
credsctl profile import yandex-tracker/shared --from-file - --if-absent --ignore-unknown < yandex-tracker.env
credsctl bind default yandex-tracker/shared
```

## Roles and policy

`policy.example.yaml` is the stdout of `stdio-box policy-example --namespace yandex_tracker --provider-dir packages/yandex-tracker`. The box does not enforce roles. Do not give a non-administrator role `yandex_tracker_*`: that pattern also matches `yandex_tracker_creds_*`.

- `yandex_tracker_reader` lists tools the server marks read-only, plus `yandex_tracker_creds_whoami` and `yandex_tracker_creds_providers`.
- `yandex_tracker_writer` lists every published tool and those same two credential tools.
- `yandex_tracker_box_admin` may call `yandex_tracker_creds_*`.

Changing credential tools require a signed-in person. A published name is `yandex_tracker_` plus the snapshot name, and is at most 64 characters. The server prefixes its own names with `fr_yandex_tracker_`; those names are kept as the server returns them.

## Upgrade the server

1. Bump `@fractalizer/mcp-server-yandex-tracker` in `package.json` and refresh `package-lock.json` with `npm install --package-lock-only`. Confirm `npm ci --ignore-scripts` still starts the server.
2. Take a new snapshot with `stdio-box snapshot-tools` inside the `stdio-box:dev` image (`BOX_PROVIDER=yandex-tracker`).
3. Commit the new lock file and `tools.snapshot.json`.
4. Release a new stdio-box image and bump the package patch version.

## License

The Tracker MCP server is PolyForm Shield 1.0.0 ([FractalizeR/mcp_servers](https://github.com/FractalizeR/mcp_servers/tree/main/packages/servers/yandex-tracker), [license text](https://polyformproject.org/licenses/shield/1.0.0/)). It is downloaded at container start under the terms of that license.
