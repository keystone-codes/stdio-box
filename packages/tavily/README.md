# Tavily provider package

Tavily search tools (`tavily-mcp` 0.2.22) behind Keystone. Each person, service, or role uses its own Tavily API key. The server code is installed from the lock file into the box `cache` volume on first start; it is not baked into the image.

## Install

```sh
keystonectl service apply tavily
```

Answer the domain and certificate questions (or use `--defer-public` if the public URL is not ready yet). The manifest pins the shared `stdio-box` image and sets `BOX_PROVIDER=tavily` and `BOX_COMMAND=npx -y tavily-mcp@0.2.22`.

## Credential variant

| Variant | Fields | Check |
|---|---|---|
| `api_key` | `TAVILY_API_KEY` | `GET https://api.tavily.com/usage` with `Authorization: Bearer` |

Create a key at [app.tavily.com](https://app.tavily.com). The usage request does not spend search credits. It returns the key's credit usage and limit for the current billing cycle, so the remaining balance is the limit minus the usage, plus the account plan. Documented at [Tavily Usage](https://docs.tavily.com/documentation/api-reference/endpoint/usage). The JSON has no top-level string that names the key owner (the plan name sits under `account`), and a check can only display a top-level string, so this check does not set `identity`. A search would spend credits, so it is not the check.

## First profile

From chat (after the box is healthy):

1. `tavily_creds_profile_create` with `{"name": "team", "variant": "api_key"}` — open the form link and paste `TAVILY_API_KEY`.
2. `tavily_creds_bind` with `{"subject": "user:you@example.com", "profile": "tavily/team"}`.

Or import from a file:

```sh
docker compose exec -T tavily-mcp credsctl profile import tavily/shared \
  --from-file - --if-absent <<'EOF'
TAVILY_API_KEY=your-token-here
EOF
docker compose exec tavily-mcp credsctl bind default tavily/shared
```

Replace `tavily-mcp` with the MCP container name from `keystonectl service status tavily --json`.

## Move from deploy `tavily.env`

The old supergateway service used a single `TAVILY_API_KEY` for everyone. Import that env file with `--ignore-unknown` if it contains extra keys. The `api_key` variant is chosen when only `TAVILY_API_KEY` is set.

```sh
credsctl profile import tavily/shared --from-file - --if-absent --ignore-unknown < tavily.env
credsctl bind default tavily/shared
```

## Roles and policy

`policy.example.yaml` is the stdout of `stdio-box policy-example --namespace tavily --provider-dir packages/tavily`. The box does not enforce roles: any caller your gateway policy allows to use `tavily_*` can reach Tavily tools, and anyone allowed `tavily_creds_*` can manage every profile in this box. Do not give a broad `tavily_*` allow rule to non-administrator roles.

`tavily-mcp` 0.2.22 does not set `readOnlyHint` on its tools, so the example reader role only lists `tavily_creds_whoami` and `tavily_creds_providers`. Add the read tools you want that role to call. The writer role lists every published tool. `tavily_box_admin` may call `tavily_creds_*`.

Published names at the door are `tavily_` plus the snapshot name (`tavily_search`, `tavily_extract`, `tavily_crawl`, `tavily_map`, `tavily_research`). Each is at most 64 characters.

## Upgrade the server

1. Bump `tavily-mcp` in `package.json`, refresh `package-lock.json` with `npm install --package-lock-only`, and confirm `npm ci --ignore-scripts` still starts the server.
2. Take a new snapshot with `stdio-box snapshot-tools` inside the `stdio-box:dev` image (`BOX_PROVIDER=tavily`).
3. Commit the new lock file and `tools.snapshot.json`.
4. Release a new stdio-box image and bump the package patch version.

## License

The Tavily MCP server is MIT-licensed ([tavily-ai/tavily-mcp](https://github.com/tavily-ai/tavily-mcp)). It is downloaded at container start under the terms of that license.
