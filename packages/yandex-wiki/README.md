# Yandex Wiki provider package

Yandex Wiki tools (`ya-wiki-mcp` 0.3.1) behind Keystone. Each person, service, or role uses its own Wiki account. The server is not stored in the image. On first start the box installs `uvx ya-wiki-mcp==0.3.1` from `requirements.txt` into the `cache` volume. Later starts reuse that volume.

The door namespace is `yandex_wiki`.

## Install

```sh
keystonectl service apply yandex-wiki
```

Answer the domain and certificate questions (or use `--defer-public` if the public URL is not ready yet). The manifest pins the shared `stdio-box` image and sets `BOX_PROVIDER=yandex-wiki` and `BOX_COMMAND=uvx ya-wiki-mcp==0.3.1`.

## Credential variant

| Variant | Fields | Check |
|---|---|---|
| `token` | `YA_WIKI_TOKEN`, `YA_WIKI_ORG_ID` | `GET https://api.wiki.yandex.net/v1/users/me` with `Authorization: OAuth` and `X-Cloud-Org-Id` |

The current-user method is [Me](https://yandex.ru/support/wiki/en/api-ref/users/users__me). The check reads the top-level `username` string. Access headers are documented at [API access](https://yandex.ru/support/wiki/en/api-ref/access): `X-Org-Id` for Yandex 360 and `X-Cloud-Org-Id` for Yandex Cloud.

`ya-wiki-mcp` 0.3.1 sends `X-Cloud-Org-Id` unless `YA_WIKI_ORG_TYPE=business`. Deploy's `yandex-wiki.env` sets only `YA_WIKI_TOKEN` and `YA_WIKI_ORG_ID`, so the running server uses the Cloud header. This check uses that same header, so it accepts the accounts the child accepts. A Yandex 360 organization needs `YA_WIKI_ORG_TYPE=business`, which this variant does not set. A token that starts with `t1.` is an IAM token; the server sends it as `Bearer`, and this OAuth check does not cover that shape.

Create an OAuth token at [oauth.yandex.ru](https://oauth.yandex.ru/) with Wiki access. The organization id is the Cloud organization id the server will send.

## First profile

From chat (after the box is healthy). Tool names at the door carry the `yandex_wiki_` prefix.

1. `yandex_wiki_creds_profile_create` with `{"name": "team", "variant": "token", "fields": {"YA_WIKI_ORG_ID": "your-org-id"}}`. The token is not accepted in chat.
2. Open the link and submit `YA_WIKI_TOKEN`. The page names the account it reached (`username`).
3. `yandex_wiki_creds_bind` with `{"subject": "user:you@example.com", "profile": "yandex-wiki/team"}`.

## Move from deploy `yandex-wiki.env`

```sh
credsctl profile import yandex-wiki/shared --from-file - --if-absent --ignore-unknown < yandex-wiki.env
credsctl bind default yandex-wiki/shared
```

The `token` variant is chosen when the non-empty known keys are exactly `YA_WIKI_TOKEN` and `YA_WIKI_ORG_ID`.

## Roles and policy

`policy.example.yaml` is the stdout of `stdio-box policy-example --namespace yandex_wiki --provider-dir packages/yandex-wiki`. The box does not enforce roles. Do not give a non-administrator role `yandex_wiki_*`: that pattern also matches `yandex_wiki_creds_*`.

`ya-wiki-mcp` 0.3.1 does not set `readOnlyHint`, so the example reader role only lists `yandex_wiki_creds_whoami` and `yandex_wiki_creds_providers`. Add the read tools you want that role to call. The writer role lists every published tool. `yandex_wiki_box_admin` may call `yandex_wiki_creds_*`.

Tools whose snapshot name contains `delete` are critical, together with the credential tools that change accounts. A published name is `yandex_wiki_` plus the snapshot name, and is at most 64 characters.

## Upgrade the server

1. Change the pin in `requirements.in` and set the same version in `server.command` and `BOX_COMMAND`.
2. Rebuild the lock:

```sh
uv pip compile --universal --generate-hashes --python-version 3.12 requirements.in -o requirements.txt
```

3. Install that lock with `uv pip install --require-hashes --no-deps --only-binary :all:` on `linux/amd64` and `linux/arm64`. If a dependency has no wheel for either platform, stop.
4. Take a new snapshot with `stdio-box snapshot-tools` inside the `stdio-box:dev` image (`BOX_PROVIDER=yandex-wiki`).
5. Raise the package version. A new server build with the same tool names is a patch. Renaming or removing a tool is a new major version.

## License

ya-wiki-mcp is MIT licensed: https://pypi.org/project/ya-wiki-mcp/0.3.1/
