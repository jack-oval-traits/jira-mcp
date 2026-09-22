"""Turn Jira REST payloads into the smallest text that still answers the question.

Savings come from three places, roughly in order of how much each contributes:

  1. Field allowlists (FIELDS_*) stop Jira sending `*navigable` in the first
     place. This is the single biggest win and it happens before a byte crosses
     the wire.
  2. Nested objects collapse to their display string -- `status.name`, not the
     twelve-key object with `self`, `iconUrl`, and a `statusCategory` sub-object.
  3. ADF description and comment bodies become Markdown via marklas.

Output is plain text rather than JSON. Braces, quotes, and repeated key names
are pure overhead once the shape is documented in the tool description.
"""

from __future__ import annotations

import os
from typing import Any

from marklas import to_md

# Requested from Jira for list-style results. No description -- summary records
# are supposed to be cheap; get_issue exists for the rest.
FIELDS_SUMMARY = (
    "summary",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "updated",
    "parent",
)

CUSTOM_FIELDS = {
    "loopCount": os.environ.get("JIRA_FIELD_LOOP_COUNT") or "customfield_10174",
    "timeInStatus": os.environ.get("JIRA_FIELD_TIME_IN_STATUS") or "customfield_10173",
    "activeAgent": os.environ.get("JIRA_FIELD_ACTIVE_AGENT") or "customfield_10175",
    "runId": os.environ.get("JIRA_FIELD_RUN_ID") or "customfield_10176",
    "dispatchedFrom": os.environ.get("JIRA_FIELD_DISPATCHED_FROM") or "customfield_10177",
    "lastHeartbeat": os.environ.get("JIRA_FIELD_LAST_HEARTBEAT") or "customfield_10178",
    "sessionLink": os.environ.get("JIRA_FIELD_SESSION_LINK") or "customfield_10180",
}

# These fields are being introduced after the original dispatcher dashboard.
# Keep them optional so a Jira site can upgrade the adapter before an
# administrator has created the fields and supplied their IDs.
CUSTOM_FIELDS.update(
    {
        label: field_id
        for label, field_id in {
            "codeyAgentTier": os.environ.get("JIRA_FIELD_CODEY_AGENT_TIER", "").strip(),
            "codeyAgentProvider": os.environ.get(
                "JIRA_FIELD_CODEY_AGENT_PROVIDER", ""
            ).strip(),
        }.items()
        if field_id
    }
)

FIELDS_DETAIL = FIELDS_SUMMARY + (
    "description",
    "reporter",
    "labels",
    "created",
    "duedate",
    "resolution",
    "fixVersions",
    "components",
    "attachment",
    "subtasks",
    "issuelinks",
    *CUSTOM_FIELDS.values(),
)

EMPTY = "-"


def _name(obj: Any) -> str:
    """`{"name": "In Progress", "self": ..., "iconUrl": ...}` -> `In Progress`."""
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("value") or EMPTY
    return EMPTY


def _person(obj: Any) -> str:
    if isinstance(obj, dict):
        return obj.get("displayName") or EMPTY
    return EMPTY


def _day(stamp: Any) -> str:
    """Jira sends `2026-08-03T13:04:11.512-0700`; the date is what gets read."""
    return stamp[:10] if isinstance(stamp, str) and len(stamp) >= 10 else EMPTY


def _custom_value(value: Any) -> str:
    """Render the small set of dispatcher custom fields without leaking raw JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, list):
        return ", ".join(filter(None, (_custom_value(item) for item in value)))
    if isinstance(value, dict):
        for key in ("value", "name", "displayValue", "formattedValue"):
            if rendered := _custom_value(value.get(key)):
                return rendered
    return ""


def adf_to_md(adf: Any) -> str:
    """ADF JSON -> plain Markdown. Returns "" for empty/absent bodies."""
    if not adf:
        return ""
    if isinstance(adf, str):  # some endpoints still return wiki markup
        return adf.strip()
    try:
        return to_md(adf, plain=True).strip()
    except Exception:  # noqa: BLE001 - a malformed body must not fail the read
        return "(description could not be converted from ADF)"


def summary_line(issue: dict) -> str:
    """One issue as one line.

    Format: `KEY | type | status | priority | assignee | updated | summary`
    """
    f = issue.get("fields") or {}
    return " | ".join(
        (
            issue.get("key", EMPTY),
            _name(f.get("issuetype")),
            _name(f.get("status")),
            _name(f.get("priority")),
            _person(f.get("assignee")) if f.get("assignee") else "unassigned",
            _day(f.get("updated")),
            (f.get("summary") or EMPTY).strip(),
        )
    )


def summary_block(issues: list[dict], next_token: str | None = None) -> str:
    if not issues:
        return "No matching issues."
    lines = [summary_line(i) for i in issues]
    if next_token:
        lines.append(f"\n(more results available -- pass page_token={next_token})")
    return "\n".join(lines)


def _links(raw: list[dict] | None) -> str:
    """`blocks PROJ-1, is blocked by PROJ-2`"""
    out = []
    for link in raw or []:
        t = link.get("type") or {}
        if (other := link.get("outwardIssue")) is not None:
            out.append(f"{t.get('outward', 'relates to')} {other.get('key')}")
        elif (other := link.get("inwardIssue")) is not None:
            out.append(f"{t.get('inward', 'relates to')} {other.get('key')}")
    return ", ".join(out)


def detail_text(issue: dict) -> str:
    """Full issue: a compact header block, then the Markdown description."""
    f = issue.get("fields") or {}
    key = issue.get("key", EMPTY)

    head: list[str] = [f"{key} - {(f.get('summary') or EMPTY).strip()}"]

    def add(label: str, value: str) -> None:
        if value and value != EMPTY:
            head.append(f"{label}: {value}")

    add("type", _name(f.get("issuetype")))
    add("status", _name(f.get("status")))
    add("priority", _name(f.get("priority")))
    add("assignee", _person(f.get("assignee")) if f.get("assignee") else "unassigned")
    add("reporter", _person(f.get("reporter")))
    add("created", _day(f.get("created")))
    add("updated", _day(f.get("updated")))
    add("due", _day(f.get("duedate")) if f.get("duedate") else "")
    add("resolution", _name(f.get("resolution")) if f.get("resolution") else "")
    add("labels", ", ".join(f.get("labels") or []))
    add("fixVersions", ", ".join(_name(v) for v in (f.get("fixVersions") or [])))
    add("components", ", ".join(_name(v) for v in (f.get("components") or [])))
    add(
        "attachments",
        ", ".join(
            f"{a.get('filename', '?')} ({a.get('mimeType', 'unknown type')})"
            for a in (f.get("attachment") or [])
        ),
    )

    if parent := f.get("parent"):
        pf = parent.get("fields") or {}
        add("parent", f"{parent.get('key')} {(pf.get('summary') or '').strip()}".strip())

    if subs := f.get("subtasks"):
        add("subtasks", ", ".join(s.get("key", "") for s in subs))

    add("links", _links(f.get("issuelinks")))

    for label, field_id in CUSTOM_FIELDS.items():
        add(label, _custom_value(f.get(field_id)))

    body = adf_to_md(f.get("description"))
    if body:
        head.append("\n## Description\n" + body)

    return "\n".join(head)


def comments_text(key: str, comments: list[dict]) -> str:
    if not comments:
        return f"{key} has no comments."
    out = [f"{key} - {len(comments)} comment(s)"]
    for c in comments:
        author = _person(c.get("author"))
        when = _day(c.get("created"))
        body = adf_to_md(c.get("body")) or "(empty)"
        out.append(f"\n--- {author} on {when}\n{body}")
    return "\n".join(out)


def transitions_text(key: str, transitions: list[dict]) -> str:
    if not transitions:
        return f"{key} has no available transitions."
    names = ", ".join(
        t.get("name", "?") for t in transitions if not t.get("isConditional")
    ) or ", ".join(t.get("name", "?") for t in transitions)
    return f"{key} can move to: {names}"
