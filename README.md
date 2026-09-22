# jira-mcp

A token-lean MCP server in front of Jira Cloud. Eleven tools, normalized text
responses, ADF descriptions converted to Markdown.

## Why

The official Atlassian MCP servers register ~45 tools and return raw REST JSON:
`self` URLs, `avatarUrls` in four sizes, `iconUrl` on every status and priority,
`statusCategory` sub-objects, and descriptions as Atlassian Document Format —
a JSON tree where a paragraph of text costs several hundred tokens.

This adapter does four things about that:

| | |
|---|---|
| **Field allowlists** | Asks Jira for seven fields, not `*navigable`. Biggest single win, and it happens before a byte crosses the wire. |
| **Collapsed objects** | `status.name`, not the twelve-key status object. |
| **ADF → Markdown** | Via [marklas](https://github.com/byExist/marklas). ~2.5–3.9x on body text. |
| **Acks, not echoes** | `PROJ-443 updated`, not the whole issue re-serialized. |

Measure it against your own issues rather than trusting those numbers — see
[Measuring](#measuring).

## Requirements

- A Jira Cloud site and an API token from
  <https://id.atlassian.com/manage-profile/security/api-tokens>
- Docker, **or** Python 3.12+

## Quickstart

```bash
git clone https://github.com/jack-oval-traits/jira-mcp
cd jira-mcp
cp .env.example .env
```

Fill in four values in `.env`:

| Variable | |
|---|---|
| `JIRA_BASE_URL` | your site, e.g. `https://your-org.atlassian.net` — but see [Scoped tokens](#scoped-api-tokens) |
| `JIRA_EMAIL` | the account the API token belongs to |
| `JIRA_API_TOKEN` | from the link above |
| `JIRA_MCP_TOKEN` | shared secret clients present — `openssl rand -hex 32` |

Then:

```bash
docker compose up -d --build
curl -fsS http://127.0.0.1:8787/healthz   # -> ok
```

That's the whole setup. Nothing external is required — no pre-created networks,
no other compose projects.

### Scoped API tokens

Atlassian issues two kinds of API token, and they do not accept the same URL:

| Token | `JIRA_BASE_URL` |
|---|---|
| classic (no scopes) | `https://your-org.atlassian.net` |
| scoped | `https://api.atlassian.com/ex/jira/<cloudId>` |

A scoped token sent to a site URL fails in the worst available way: it is not
rejected, it is **ignored**. Jira answers as if you were logged out, and since
anonymous users can see no issues, `search_issues` returns `No matching issues.`
— which reads exactly like an empty project.

So the server checks `GET /rest/api/3/myself` at startup and refuses to serve on
a 401, printing the gateway URL for your site. If you need the cloud id yourself:

```bash
curl -s https://your-org.atlassian.net/_edge/tenant_info
```

A scoped token also needs `read:jira-work` and `write:jira-work`; without them
the same call returns 403 and startup fails the same way.

### Without Docker

```bash
uv pip install -e .
set -a && . ./.env && set +a
jira-mcp                 # listens on 127.0.0.1:8787
```

## Connecting a client

For Claude Code:

```bash
claude mcp add --transport http jira http://127.0.0.1:8787/mcp \
  --header "Authorization: Bearer $JIRA_MCP_TOKEN"
```

Any MCP client that speaks streamable HTTP works the same way: point it at
`/mcp` and send the bearer token in an `Authorization` header.

If you already have a full Atlassian MCP server registered, **remove it** — the
savings come from removal, not addition. Two servers exposing the same Jira
means both tool sets land in the context window:

```bash
claude mcp remove <your-atlassian-server-name>
```

Keep one around if you need Confluence, Bitbucket, or JSM Ops; this adapter
covers Jira issues only.

## Tools

| Tool | Returns |
|---|---|
| `search_issues(jql, limit, page_token)` | one line per issue, no descriptions |
| `get_issue(key)` | full issue, description as Markdown |
| `get_comments(key, limit)` | comments as Markdown, newest first |
| `create_issue(summary, description, issue_type, project, labels, parent, components)` | `PROJ-501 created` |
| `update_issue(key, summary, description, labels, assignee, components)` | `PROJ-443 updated: summary` |
| `set_dispatch_state(key, operation, ...)` | updates dispatcher-owned Jira fields with a short ack |
| `attach_image(key, filename, image_base64, content_type)` | `PROJ-443 attached image design.png` |
| `add_comment(key, body)` | `PROJ-443 commented` |
| `transition_issue(key, status)` | `PROJ-443 -> Done`, or lists options |
| `list_issue_types(project)` | creatable issue types and whether each is a subtask |
| `link_issues(link_type, inward, outward)` | links two issues, or lists link types |

Markdown goes in and comes out; the adapter converts to and from ADF at the
boundary.

`get_issue` includes the `devRetryApproved` value from Jira's **Dev Retry**
single-select field (`customfield_10183`). A human may set it to `Approved` in
Jira; `set_dispatch_state(key, "consume_retry")` clears only that field after
the orchestrator accepts the one-time retry. It does not grant approval.

`components` is a comma-separated list of component names that already exist
in the target project. `attach_image` accepts either plain base64 or a
`data:image/...;base64,...` URI and adds the image to Jira's Attachments section.

`search_issues` returns lines shaped like:

```
PROJ-443 | Bug | In Progress | High | Jane Doe | 2026-08-03 | Roster import crashes on duplicate jersey
```

Set `JIRA_DEFAULT_PROJECT` in `.env` and `create_issue` can omit the project.

## Configuration

Everything is environment variables; `.env.example` documents each one.

| Variable | Default | |
|---|---|---|
| `JIRA_BASE_URL` | — | required; site URL, or the gateway for a [scoped token](#scoped-api-tokens) |
| `JIRA_EMAIL` | — | required |
| `JIRA_API_TOKEN` | — | required |
| `JIRA_MCP_TOKEN` | — | required, the client-facing bearer |
| `JIRA_DEFAULT_PROJECT` | — | fallback project for `create_issue` |
| `JIRA_MCP_BIND_ADDRESS` | `127.0.0.1` | host side of the published port |
| `JIRA_MCP_PORT` | `8787` | host side of the published port |
| `JIRA_MCP_LISTEN` | `127.0.0.1` | what the process itself binds; the image sets `0.0.0.0` |
| `JIRA_MCP_HOST` | — | public hostname, when behind a proxy |
| `JIRA_MCP_ALLOWED_HOSTS` | — | extra `Host` values to accept, comma-separated |
| `JIRA_MCP_STARTUP_CHECK` | `on` | `off` skips the startup credential probe (CI, smoke tests) |
| `JIRA_MCP_MEASURE` | off | log per-call token counts — see [Measuring](#measuring) |
| `LOG_LEVEL` | `info` | |

The dispatcher dashboard field IDs use `JIRA_FIELD_*` variables documented in
`.env.example`. `JIRA_FIELD_CODEY_AGENT_TIER` and
`JIRA_FIELD_CODEY_AGENT_PROVIDER` are optional until those Jira single-select
fields exist. Once configured, `get_issue` returns both values and a Codey
`set_dispatch_state(..., operation="claim")` call can write them with the
resolved tier/provider selection. Supported tiers are Fast, Standard, Complex,
and Frontier; supported providers are Codex and Claude.

Missing required variables fail at startup, not on the first tool call — as do
credentials Jira turns down, which costs one round-trip to `/myself` per boot.
`JIRA_MCP_STARTUP_CHECK=off` skips that probe — and with it the scoped-token
diagnosis above — for environments with no Jira reachable, like CI.

## Behind a reverse proxy

To serve this on a hostname with TLS, layer the optional overlay on top of the
base compose file:

```bash
docker network create web                    # if the proxy's network doesn't exist
echo 'JIRA_MCP_HOST=jira-mcp.example.com' >> .env
echo 'PROXY_NETWORK=web' >> .env
docker compose -f compose.yaml -f compose.proxy.yaml up -d --build
```

The overlay joins an existing external network so the proxy can reach the
container as `jira-mcp:8787`. With Caddy:

```
jira-mcp.example.com {
    reverse_proxy jira-mcp:8787
}
```

`JIRA_MCP_HOST` has to be set for the server too, not just the proxy — the MCP
SDK enables DNS-rebinding protection with an empty allowlist, so any hostname it
should answer to must be named explicitly. See `_allowed_hosts` in `server.py`.

## Measuring

Runs the same JQL as a naive client and as this adapter, and counts both:

```bash
docker compose run --rm --entrypoint python jira-mcp \
  scripts/token_compare.py 'project = PROJ ORDER BY updated DESC' 6
```

Counting uses tiktoken, a GPT tokenizer, so absolute numbers are approximate for
Claude. The ratio is the point.

### Against real traffic

That script is a one-off against a synthetic second query. To track the same
thing continuously, set `JIRA_MCP_MEASURE=1` and recreate the container. Every
tool call then prints one JSON record — `jira.py` holds Jira's response before
`normalize.py` shapes it, so both halves are there without a second request:

```json
{"measure":"tool_call","tool":"search_issues","raw_tokens":8756,"out_tokens":523,
 "jira_calls":1,"ms":760.6,"arg":"project = PROJ ORDER BY updated DESC","saved":0.94}
```

Read them back with:

```bash
docker logs jira-mcp-jira-mcp-1 2>&1 | python3 scripts/measure_report.py
```

```
tool              calls   from jira      sent   saved   median
--------------------------------------------------------------
search_issues         1       8,756       523   94.0%     761ms
get_issue             1       3,662       828   77.4%     184ms
get_comments          1          18         8   55.6%     155ms
--------------------------------------------------------------
all                   3      12,436     1,359   89.1%     184ms

11,077 tokens not spent across 3 calls.
```

Reduction is weighted by size, not averaged per call — one 9k-token search and
one 8-token ack should not count equally toward the headline.

**These numbers are smaller than the script's, and deliberately so.**
`raw_tokens` is what Jira sent *for the fields this adapter asked for*. The
allowlist — the biggest single win — has already been applied by then, upstream
of anything measurable here, so what these records show is the shaping alone:
collapsing, ADF→Markdown, one line per issue. `token_compare.py` measures
against `fields=*navigable`, which is why it reports ~99% where this reports
~94%. Both are true; they answer different questions. Measuring the
counterfactual continuously would mean issuing every request twice.

It is off by default for two reasons: encoding every response costs a few ms,
and the records carry JQL and issue keys into wherever stdout goes. Records
survive only as long as the container's logs, so redirect them somewhere if you
want history.

## Layout

```
src/jira_mcp/
  server.py      tool definitions, bearer auth, uvicorn wiring
  jira.py        async REST v3 client — takes field allowlists, returns raw JSON
  normalize.py   all shaping lives here: allowlists, collapsing, ADF conversion
  measure.py     optional per-call token accounting, off unless asked for
scripts/
  token_compare.py
  measure_report.py
tests/
  test_normalize.py
  test_credentials.py
  test_measure.py
```

`normalize.py` is the file to edit when a response is still too fat, or when a
field you need got allowlisted out.

```bash
uv pip install -e ".[dev]"
pytest -q
ruff check .
```

## Security

The adapter authenticates to Jira with Basic auth (email + API token), so it
**acts as you**: it has your permissions and its writes carry your name. The
bearer token is the only thing between the network and that Jira token, and six
of the ten tools write.

Bind to loopback unless something in front terminates TLS. `/healthz` is
deliberately unauthenticated; everything else is not. See
[SECURITY.md](SECURITY.md).

## Notes

- Uses `POST /rest/api/3/search/jql`, the token-paginated replacement for the
  removed `/rest/api/3/search`. There is no total count; `search_issues` reports
  a page token when more results exist.
- Requires MCP SDK 2.x — the server class is `MCPServer` there and `FastMCP` on
  1.x, so the pin in `pyproject.toml` is load-bearing.

## License

MIT — see [LICENSE](LICENSE).
