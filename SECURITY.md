# Security

## What this server holds

Running it means giving a long-lived process two secrets:

- **A Jira Cloud API token** (`JIRA_API_TOKEN`), paired with `JIRA_EMAIL`. The
  adapter authenticates to Jira with HTTP Basic and therefore **acts as that
  user**. It has exactly their permissions, and every issue it creates, edits,
  transitions, or comments on carries their name. There is no separate service
  identity and no reduced scope.
- **A bearer token** (`JIRA_MCP_TOKEN`) that clients present to reach the
  server. Anyone holding it can drive the Jira token above.

The bearer token is the only thing between the network and your Jira account.
Treat it as equivalent to the Jira credential itself.

## Deployment expectations

- **Bind to loopback unless something else terminates TLS.** The default
  `JIRA_MCP_LISTEN` is `127.0.0.1`; the container image overrides it to
  `0.0.0.0` because Docker publishes the port itself, and `compose.yaml`
  publishes only to `127.0.0.1`. If you expose the port more widely, put a
  reverse proxy with TLS in front — the bearer token crosses the wire in a
  header on every request.
- **Generate the bearer token randomly:** `openssl rand -hex 32`. Comparison is
  constant-time (`secrets.compare_digest`), so a high-entropy value is not
  guessable, but a short or reused one is.
- **`/healthz` is deliberately unauthenticated.** It returns the literal string
  `ok` and reads no Jira state. It exists so container health checks and proxies
  can probe without holding the secret. Everything else requires the bearer.
- **Rotate by editing `.env` and restarting.** Both tokens are read at startup.
- **`.env` is gitignored.** Keep it that way. If a token is ever committed or
  pasted somewhere shared, revoke it at
  <https://id.atlassian.com/manage-profile/security/api-tokens> rather than
  rewriting history and hoping.

## Scope of the tool surface

The seven tools include four writes (`create_issue`, `update_issue`,
`add_comment`, `transition_issue`). There is no read-only mode. A client that
reaches this server can modify your Jira project. If you want read-only access,
run it with a Jira account whose project permissions are read-only — enforce it
on the Jira side, not here.

## Reporting a vulnerability

Open a private GitHub security advisory on the repository ("Report a
vulnerability" under the Security tab). Please don't file a public issue for
anything that would expose someone's credentials.
