"""MCP server exposing a small, token-lean surface over Jira Cloud.

Ten tools instead of the ~45 the official Atlassian servers register, and
every response is normalized text rather than raw REST JSON. See normalize.py
for where the savings actually come from.

Transport is streamable HTTP on :8787/mcp, guarded by a bearer token.
"""

from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
import os
import secrets

import httpx
import uvicorn
from marklas import to_adf
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse

from .jira import API, JiraClient, JiraError
from .measure import measured
from .normalize import (
    CUSTOM_FIELDS,
    FIELDS_DETAIL,
    FIELDS_SUMMARY,
    comments_text,
    detail_text,
    summary_block,
    transitions_text,
)

# MCPServer is what the 2.x SDK calls the class the 1.x docs name FastMCP.
mcp = MCPServer("jira-mcp")

_client: JiraClient | None = None


def client() -> JiraClient:
    global _client
    if _client is None:
        _client = JiraClient(
            base_url=_env("JIRA_BASE_URL"),
            email=_env("JIRA_EMAIL"),
            api_token=_env("JIRA_API_TOKEN"),
        )
    return _client


def _env(name: str, default: str | None = None) -> str:
    # .strip(): a trailing space or newline pasted into .env otherwise turns
    # into a 401 with no visible cause.
    value = (os.environ.get(name) or default or "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _adf(markdown: str) -> dict:
    return to_adf(markdown or "")


def _names(value: str) -> list[dict[str, str]]:
    """Turn a comma-separated Jira field into its REST representation."""
    return [{"name": item.strip()} for item in value.split(",") if item.strip()]


def _decode_image(filename: str, encoded: str, content_type: str) -> tuple[bytes, str]:
    """Validate and decode an MCP-friendly image payload.

    MCP tool arguments are JSON, so binary input arrives as base64. Accept a
    plain base64 string or a browser-style ``data:image/...;base64,...`` URI.
    """
    if not filename.strip() or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise ValueError("filename must be a plain file name without a path")

    payload = encoded.strip()
    embedded_type = ""
    if payload.lower().startswith("data:"):
        metadata, separator, payload = payload.partition(",")
        if not separator or ";base64" not in metadata.lower():
            raise ValueError("data URI must use base64 encoding")
        embedded_type = metadata[5:].split(";", 1)[0].strip().lower()

    try:
        image = base64.b64decode("".join(payload.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_base64 is not valid base64") from exc
    if not image:
        raise ValueError("image is empty")

    media_type = (
        content_type.strip().lower()
        or embedded_type
        or (mimetypes.guess_type(filename)[0] or "").lower()
    )
    if not media_type.startswith("image/"):
        raise ValueError("content type must be an image type (for example image/png)")
    return image, media_type


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@mcp.tool()
@measured
async def search_issues(jql: str, limit: int = 25, page_token: str = "") -> str:
    """Search issues by JQL. Returns one line per issue:
    `KEY | type | status | priority | assignee | updated | summary`.
    Descriptions are not included -- call get_issue for those.
    """
    try:
        data = await client().search(
            jql=jql,
            fields=FIELDS_SUMMARY,
            limit=max(1, min(limit, 100)),
            page_token=page_token or None,
        )
    except JiraError as exc:
        return str(exc)
    return summary_block(
        data.get("issues") or [],
        None if data.get("isLast", True) else data.get("nextPageToken"),
    )


@mcp.tool()
@measured
async def get_issue(key: str) -> str:
    """Full detail for one issue, with the description converted to Markdown."""
    try:
        return detail_text(await client().issue(key, FIELDS_DETAIL))
    except JiraError as exc:
        return str(exc)


@mcp.tool()
@measured
async def get_comments(key: str, limit: int = 10) -> str:
    """Comments on an issue, newest first, bodies converted to Markdown."""
    try:
        data = await client().comments(key, max(1, min(limit, 50)))
    except JiraError as exc:
        return str(exc)
    return comments_text(key, data.get("comments") or [])


# ---------------------------------------------------------------------------
# Writes -- these acknowledge, they do not echo the issue back
# ---------------------------------------------------------------------------


@mcp.tool()
@measured
async def create_issue(
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    project: str = "",
    labels: str = "",
    parent: str = "",
    components: str = "",
) -> str:
    """Create an issue. `description` is Markdown. `labels` and `components`
    are comma-separated. Components must already exist in the Jira project.
    `parent` is an issue key: required for a Subtask, and also how a Story or
    Task is placed under an Epic. Returns the new key only.
    """
    fields: dict = {
        "project": {"key": project or os.environ.get("JIRA_DEFAULT_PROJECT", "")},
        "summary": summary,
        "issuetype": {"name": issue_type},
    }
    if not fields["project"]["key"]:
        return "No project given and JIRA_DEFAULT_PROJECT is not set."
    if description:
        fields["description"] = _adf(description)
    if labels:
        fields["labels"] = [x.strip() for x in labels.split(",") if x.strip()]
    if components:
        fields["components"] = _names(components)
    if parent:
        # Subtasks cannot be created and then re-parented -- Jira rejects a
        # parent change on update -- so this has to be set at creation time.
        fields["parent"] = {"key": parent.strip()}

    try:
        created = await client().create(fields)
    except JiraError as exc:
        return str(exc)
    return f"{created.get('key')} created"


@mcp.tool()
@measured
async def update_issue(
    key: str,
    summary: str = "",
    description: str = "",
    labels: str = "",
    assignee: str = "",
    components: str = "",
) -> str:
    """Update an issue. Only non-empty arguments are applied. `description` is
    Markdown; `assignee` is a display name or email; `components` is a
    comma-separated list of existing component names. Returns a short ack.
    """
    fields: dict = {}
    if summary:
        fields["summary"] = summary
    if description:
        fields["description"] = _adf(description)
    if labels:
        fields["labels"] = [x.strip() for x in labels.split(",") if x.strip()]
    if components:
        fields["components"] = _names(components)

    if assignee:
        try:
            matches = await client().find_user(assignee)
        except JiraError as exc:
            return str(exc)
        if not matches:
            return f"No Jira user matches {assignee!r}."
        if len(matches) > 1:
            names = ", ".join(m.get("displayName", "?") for m in matches)
            return f"{assignee!r} is ambiguous: {names}"
        account_id = matches[0].get("accountId")
        if not account_id:
            return f"Jira returned no accountId for {assignee!r}; cannot assign."
        fields["assignee"] = {"accountId": account_id}

    if not fields:
        return "Nothing to update."

    try:
        await client().update(key, fields)
    except JiraError as exc:
        return str(exc)
    return f"{key} updated: {', '.join(fields)}"


@mcp.tool()
@measured
async def set_dispatch_state(
    key: str,
    operation: str,
    active_agent: str = "",
    run_id: str = "",
    dispatched_from: str = "",
    last_heartbeat: str = "",
    session_link: str = "",
    codey_agent_tier: str = "",
    codey_agent_provider: str = "",
) -> str:
    """Update only the custom fields owned by the agent dispatcher.

    Supported operations are ``claim``, ``heartbeat``, ``complete``, and
    ``rollback``. A Codey claim may include its resolved tier and provider;
    these choices remain on the issue as an audit trail after the run ends.
    This deliberately does not accept arbitrary Jira field IDs.
    """
    operation = operation.strip().lower()

    if operation == "claim":
        values = (active_agent, run_id, dispatched_from, last_heartbeat, session_link)
        if not all(value.strip() for value in values):
            return "Invalid dispatch claim: every dispatch field is required."
        fields = {
            CUSTOM_FIELDS["activeAgent"]: {"value": active_agent.strip()},
            CUSTOM_FIELDS["runId"]: run_id.strip(),
            CUSTOM_FIELDS["dispatchedFrom"]: dispatched_from.strip(),
            CUSTOM_FIELDS["lastHeartbeat"]: last_heartbeat.strip(),
            CUSTOM_FIELDS["sessionLink"]: session_link.strip(),
        }
        codey_selection = (codey_agent_tier.strip(), codey_agent_provider.strip())
        if any(codey_selection):
            if active_agent.strip().casefold() != "codey":
                return "Invalid dispatch claim: Codey selection fields require active_agent=Codey."
            if not all(codey_selection):
                return (
                    "Invalid dispatch claim: codey_agent_tier and "
                    "codey_agent_provider must be supplied together."
                )

            allowed_tiers = {
                value.casefold(): value
                for value in ("Fast", "Standard", "Complex", "Frontier")
            }
            allowed_providers = {value.casefold(): value for value in ("Codex", "Claude")}
            tier = allowed_tiers.get(codey_selection[0].casefold())
            provider = allowed_providers.get(codey_selection[1].casefold())
            if not tier:
                return (
                    "Invalid Codey agent tier: choose Fast, Standard, Complex, "
                    "or Frontier."
                )
            if not provider:
                return "Invalid Codey agent provider: choose Codex or Claude."

            missing = [
                name
                for name in ("codeyAgentTier", "codeyAgentProvider")
                if name not in CUSTOM_FIELDS
            ]
            if missing:
                variables = ", ".join(
                    {
                        "codeyAgentTier": "JIRA_FIELD_CODEY_AGENT_TIER",
                        "codeyAgentProvider": "JIRA_FIELD_CODEY_AGENT_PROVIDER",
                    }[name]
                    for name in missing
                )
                return f"Codey selection fields are not configured: set {variables}."

            fields[CUSTOM_FIELDS["codeyAgentTier"]] = {"value": tier}
            fields[CUSTOM_FIELDS["codeyAgentProvider"]] = {"value": provider}
    elif operation == "heartbeat":
        if not last_heartbeat.strip():
            return "Invalid dispatch heartbeat: last_heartbeat is required."
        fields = {CUSTOM_FIELDS["lastHeartbeat"]: last_heartbeat.strip()}
    elif operation == "complete":
        fields = {CUSTOM_FIELDS["activeAgent"]: None}
        if last_heartbeat.strip():
            fields[CUSTOM_FIELDS["lastHeartbeat"]] = last_heartbeat.strip()
    elif operation == "rollback":
        fields = {
            CUSTOM_FIELDS["activeAgent"]: None,
            CUSTOM_FIELDS["runId"]: None,
            CUSTOM_FIELDS["dispatchedFrom"]: None,
            CUSTOM_FIELDS["lastHeartbeat"]: None,
            CUSTOM_FIELDS["sessionLink"]: None,
        }
    else:
        return f"Unknown dispatch operation {operation!r}."

    try:
        await client().update(key, fields)
    except JiraError as exc:
        return str(exc)
    return f"{key} dispatch state -> {operation}"


@mcp.tool()
@measured
async def attach_image(
    key: str,
    filename: str,
    image_base64: str,
    content_type: str = "",
) -> str:
    """Attach an image to an issue. `image_base64` may be plain base64 or a
    `data:image/...;base64,...` URI. `filename` must not contain a path.
    `content_type` is optional when it is in the data URI or filename.
    """
    try:
        image, media_type = _decode_image(filename, image_base64, content_type)
    except ValueError as exc:
        return f"Invalid image: {exc}."

    try:
        attachments = await client().attach(key, filename, image, media_type)
    except JiraError as exc:
        return str(exc)
    uploaded = attachments[0].get("filename", filename) if attachments else filename
    return f"{key} attached image {uploaded}"


@mcp.tool()
@measured
async def add_comment(key: str, body: str) -> str:
    """Add a Markdown comment to an issue. Returns a short ack."""
    try:
        await client().comment(key, _adf(body))
    except JiraError as exc:
        return str(exc)
    return f"{key} commented"


@mcp.tool()
@measured
async def transition_issue(key: str, status: str = "") -> str:
    """Move an issue to `status`. Called without `status`, or with one that does
    not match, it lists the available transitions instead.
    """
    try:
        available = (await client().transitions(key)).get("transitions") or []
    except JiraError as exc:
        return str(exc)

    if not status:
        return transitions_text(key, available)

    match = next((t for t in available if t.get("name", "").lower() == status.lower()), None)
    if match is None:
        names = ", ".join(t.get("name", "") for t in available)
        return f"{key} has no transition named {status!r}. Available: {names}"

    try:
        await client().transition(key, match["id"])
    except JiraError as exc:
        return str(exc)
    return f"{key} -> {match['name']}"


@mcp.tool()
@measured
async def list_issue_types(project: str = "") -> str:
    """Issue types creatable in a project, one per line: `name | subtask?`.

    Schemes differ per project, so a type existing elsewhere in the site does
    not mean this project can create it. Check here before assuming a hierarchy.
    """
    key = project or os.environ.get("JIRA_DEFAULT_PROJECT", "")
    if not key:
        return "No project given and JIRA_DEFAULT_PROJECT is not set."
    try:
        data = await client().issue_types(key)
    except JiraError as exc:
        return str(exc)
    types = data.get("issueTypes") or data.get("values") or []
    if not types:
        return f"No creatable issue types found for {key}."
    return "\n".join(
        f"{t.get('name', '?')} | subtask={bool(t.get('subtask'))}"
        for t in types
    )


@mcp.tool()
@measured
async def link_issues(link_type: str, inward: str, outward: str) -> str:
    """Link two issues, e.g. link_type="Relates", inward="AVBALL-1", outward="QA-2".

    Direction follows Jira's own wording: for "Blocks", `inward` is the issue
    that *is blocked by* `outward`. Call with an unknown link_type to list the
    types this site has.
    """
    try:
        if not link_type:
            data = await client().link_types()
            names = sorted({t.get("name", "") for t in data.get("issueLinkTypes", [])})
            return "Link types: " + ", ".join(n for n in names if n)
        await client().link(link_type, inward, outward)
    except JiraError as exc:
        return str(exc)
    return f"{inward} <-{link_type}-> {outward} linked"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class BearerAuth:
    """Reject anything without the shared secret, except the health check."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        # Compared as bytes: a non-UTF-8 or non-ASCII Authorization header must
        # come back 401, not 500 from .decode()/compare_digest.
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            return await self.app(scope, receive, send)

        header = dict(scope.get("headers") or {}).get(b"authorization", b"")
        if not secrets.compare_digest(header, self._expected):
            return await PlainTextResponse("unauthorized", status_code=401)(
                scope, receive, send
            )
        return await self.app(scope, receive, send)


@mcp.custom_route("/healthz", methods=["GET"])
async def _healthz(_request):
    return PlainTextResponse("ok")


def _allowed_hosts() -> list[str]:
    """Hosts this server will answer to.

    The SDK enables DNS-rebinding protection by default with an empty allowlist,
    which rejects everything -- including the hostname Caddy proxies under. So
    the public hostname has to be named explicitly.
    """
    hosts = {"localhost", "localhost:8787", "127.0.0.1", "127.0.0.1:8787", "jira-mcp:8787"}
    if public := os.environ.get("JIRA_MCP_HOST"):
        hosts |= {public, f"{public}:443"}
    for extra in (os.environ.get("JIRA_MCP_ALLOWED_HOSTS") or "").split(","):
        if extra.strip():
            hosts.add(extra.strip())
    return sorted(hosts)


def _gateway_url(cloud_id: str) -> str:
    return f"https://api.atlassian.com/ex/jira/{cloud_id}"


def _cloud_id(site_url: str) -> str | None:
    """A site's cloud id, from the unauthenticated tenant_info endpoint.

    Best-effort: it only sharpens an error message, so any failure here just
    means the message names the shape of the URL instead of the exact one.
    """
    try:
        r = httpx.get(f"{site_url}/_edge/tenant_info", timeout=10.0)
        return (r.json() or {}).get("cloudId") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _rejected_message(base_url: str, email: str, status: int, cloud_id: str | None) -> str:
    """What to tell someone whose credentials Jira just turned down."""
    lines = [f"Jira rejected the credentials ({status}) at {base_url} for {email}."]

    if "api.atlassian.com" in base_url:
        # Already on the gateway, so the URL is not the problem.
        lines += [
            "The URL is the api.atlassian.com gateway, so this is the token itself:",
            "either it does not belong to JIRA_EMAIL, it has been revoked, or (if it",
            "is a scoped token) it is missing read:jira-work / write:jira-work.",
        ]
    else:
        lines += [
            "If this token was created with scopes, that is the cause: scoped API",
            "tokens authenticate only against the api.atlassian.com gateway, never",
            "against a site URL. A site URL does not reject them, it ignores them --",
            "Jira answers as if logged out, so searches come back empty rather than",
            "failing. Point JIRA_BASE_URL at the gateway instead:",
            f"  JIRA_BASE_URL={_gateway_url(cloud_id or '<cloudId>')}",
        ]
        if not cloud_id:
            lines.append(f"  cloudId comes from {base_url}/_edge/tenant_info")
        lines.append(
            "If it is a classic unscoped token, check JIRA_EMAIL matches the account"
        )
        lines.append("that created it.")

    lines.append("Tokens: https://id.atlassian.com/manage-profile/security/api-tokens")
    return "\n".join(lines)


def _check_credentials() -> None:
    """Confirm the Jira credentials before serving, not on the first tool call.

    Worth a startup round-trip because the interesting failure is silent rather
    than loud: a scoped token sent to a site URL is ignored, not refused, so
    `search_issues` returns "No matching issues." — indistinguishable from an
    empty project. This turns that into a startup error naming the fix.
    """
    base = _env("JIRA_BASE_URL").rstrip("/")
    email = _env("JIRA_EMAIL")
    try:
        r = httpx.get(
            f"{base}{API}/myself",
            auth=(email, _env("JIRA_API_TOKEN")),
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach Jira at {base}: {exc}") from exc

    if r.status_code in (401, 403):
        cloud_id = None if "api.atlassian.com" in base else _cloud_id(base)
        raise RuntimeError(_rejected_message(base, email, r.status_code, cloud_id))
    if r.status_code != 200:
        raise RuntimeError(f"Credential check at {base}{API}/myself returned {r.status_code}.")

    who = (r.json() or {}).get("displayName") or email
    # Says out loud whose name the write tools will carry.
    logging.getLogger("jira-mcp").info("authenticated to %s as %s", base, who)


def build_app():
    # Fail at startup rather than on the first tool call.
    for required in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_MCP_TOKEN"):
        _env(required)
    # "off" boots without the /myself probe (CI, smoke tests, no network to
    # Jira yet). It also gives up the scoped-token trap detection above, so it
    # is not the thing to set when a search mysteriously comes back empty.
    startup_check = (os.environ.get("JIRA_MCP_STARTUP_CHECK") or "").strip().lower()
    if startup_check not in {"0", "false", "no", "off"}:
        _check_credentials()

    hosts = _allowed_hosts()
    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=hosts,
            allowed_origins=[f"https://{h}" for h in hosts] + [f"http://{h}" for h in hosts],
        ),
    )
    return BearerAuth(app, _env("JIRA_MCP_TOKEN"))


def main() -> None:
    try:
        app = build_app()
    except RuntimeError as exc:
        # Misconfiguration, not a crash. The message is the useful part, and a
        # traceback through runpy buries it.
        raise SystemExit(f"jira-mcp: {exc}") from None

    uvicorn.run(
        app,
        # Loopback by default: this process holds a Jira API token, and run bare
        # on a laptop or VPS a 0.0.0.0 bind puts the bearer token alone in front
        # of it. The container image sets JIRA_MCP_LISTEN=0.0.0.0 explicitly,
        # because there the port is only reachable if Docker publishes it.
        host=os.environ.get("JIRA_MCP_LISTEN", "127.0.0.1"),
        port=int(os.environ.get("JIRA_MCP_PORT", "8787")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
