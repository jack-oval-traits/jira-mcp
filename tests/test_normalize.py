"""Tests for the shaping layer.

normalize.py is where the token savings live and where most changes will land,
so it is the part worth pinning down. Everything in it is pure: dict in, text
out, no network.
"""

from __future__ import annotations

import pytest

from jira_mcp.normalize import (
    CUSTOM_FIELDS,
    EMPTY,
    FIELDS_DETAIL,
    FIELDS_SUMMARY,
    adf_to_md,
    comments_text,
    detail_text,
    summary_block,
    summary_line,
    transitions_text,
)


def adf(*paragraphs: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]}
            for p in paragraphs
        ],
    }


def issue(**overrides) -> dict:
    fields = {
        "summary": "Roster import crashes on duplicate jersey",
        "issuetype": {"name": "Bug", "iconUrl": "https://x/i.png", "self": "https://x"},
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "priority": {"name": "High", "iconUrl": "https://x/p.png"},
        "assignee": {"displayName": "Jane Doe", "avatarUrls": {"48x48": "https://x"}},
        "updated": "2026-08-03T13:04:11.512-0700",
    }
    fields.update(overrides)
    return {"key": "PROJ-443", "fields": fields}


# --- field allowlists -------------------------------------------------------


def test_summary_fields_exclude_description():
    """The whole point of the summary allowlist: descriptions stay off the wire."""
    assert "description" not in FIELDS_SUMMARY
    assert "description" in FIELDS_DETAIL


def test_detail_extends_summary():
    assert set(FIELDS_SUMMARY) <= set(FIELDS_DETAIL)


def test_detail_fields_include_components_and_attachments():
    assert "components" in FIELDS_DETAIL
    assert "attachment" in FIELDS_DETAIL


def test_detail_fields_include_dispatcher_custom_fields():
    assert set(CUSTOM_FIELDS.values()) <= set(FIELDS_DETAIL)


# --- summary_line -----------------------------------------------------------


def test_summary_line_is_one_pipe_delimited_row():
    assert summary_line(issue()) == (
        "PROJ-443 | Bug | In Progress | High | Jane Doe | 2026-08-03 | "
        "Roster import crashes on duplicate jersey"
    )


def test_summary_line_collapses_nested_objects():
    """`status.name`, not the twelve-key object -- no self/iconUrl leaks through."""
    line = summary_line(issue())
    assert "iconUrl" not in line
    assert "statusCategory" not in line
    assert "avatarUrls" not in line


def test_summary_line_unassigned():
    assert " | unassigned | " in summary_line(issue(assignee=None))


def test_summary_line_missing_fields_become_placeholder():
    line = summary_line({"key": "PROJ-1", "fields": {}})
    assert line.startswith("PROJ-1 | ")
    assert line.count(EMPTY) >= 3


def test_summary_line_truncates_timestamp_to_day():
    assert " | 2026-08-03 | " in summary_line(issue())


def test_summary_line_strips_summary_whitespace():
    assert summary_line(issue(summary="  padded  ")).endswith("| padded")


# --- summary_block ----------------------------------------------------------


def test_summary_block_empty():
    assert summary_block([]) == "No matching issues."


def test_summary_block_one_line_per_issue():
    out = summary_block([issue(), issue()])
    assert len(out.splitlines()) == 2


def test_summary_block_advertises_page_token():
    out = summary_block([issue()], next_token="tok123")
    assert "page_token=tok123" in out


def test_summary_block_omits_token_when_absent():
    assert "page_token" not in summary_block([issue()])


# --- adf_to_md --------------------------------------------------------------


def test_adf_to_md_converts_paragraphs():
    assert adf_to_md(adf("hello world")).strip() == "hello world"


def test_adf_to_md_empty_inputs():
    assert adf_to_md(None) == ""
    assert adf_to_md({}) == ""
    assert adf_to_md("") == ""


def test_adf_to_md_passes_through_wiki_markup_strings():
    assert adf_to_md("  already text  ") == "already text"


def test_adf_to_md_survives_malformed_body():
    """A bad description must not fail the whole read."""
    out = adf_to_md({"type": "doc", "content": "not-a-list"})
    assert isinstance(out, str)


# --- detail_text ------------------------------------------------------------


def test_detail_text_header_and_description():
    out = detail_text(issue(description=adf("It throws on the second row.")))
    assert out.startswith("PROJ-443 - Roster import crashes on duplicate jersey")
    assert "status: In Progress" in out
    assert "## Description" in out
    assert "It throws on the second row." in out


def test_detail_text_renders_dispatcher_custom_fields(monkeypatch):
    monkeypatch.setitem(CUSTOM_FIELDS, "codeyAgentTier", "customfield_20001")
    monkeypatch.setitem(CUSTOM_FIELDS, "codeyAgentProvider", "customfield_20002")
    monkeypatch.setitem(CUSTOM_FIELDS, "devRetryApproved", "customfield_20003")
    values = {
        "loopCount": 3,
        "timeInStatus": "22m",
        "activeAgent": "Codey",
        "runId": "run-123",
        "dispatchedFrom": "Ready for Dev",
        "lastHeartbeat": "2026-09-21T14:30:00.000-0500",
        "sessionLink": "https://example.test/session/run-123",
        "codeyAgentTier": "Fast",
        "codeyAgentProvider": "Codex",
        "devRetryApproved": {"value": "Approved"},
    }
    custom_fields = {
        CUSTOM_FIELDS[label]: value for label, value in values.items()
    }

    out = detail_text(issue(**custom_fields))

    for label, value in values.items():
        if label == "devRetryApproved":
            value = "Approved"
        assert f"{label}: {value}" in out


def test_detail_text_omits_absent_fields():
    out = detail_text(issue())
    assert "## Description" not in out
    assert "due:" not in out
    assert "resolution:" not in out


def test_detail_text_renders_links_both_directions():
    out = detail_text(
        issue(
            issuelinks=[
                {"type": {"outward": "blocks"}, "outwardIssue": {"key": "PROJ-1"}},
                {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "PROJ-2"}},
            ]
        )
    )
    assert "links: blocks PROJ-1, is blocked by PROJ-2" in out


def test_detail_text_parent_and_subtasks():
    out = detail_text(
        issue(
            parent={"key": "PROJ-400", "fields": {"summary": "Import epic"}},
            subtasks=[{"key": "PROJ-444"}, {"key": "PROJ-445"}],
        )
    )
    assert "parent: PROJ-400 Import epic" in out
    assert "subtasks: PROJ-444, PROJ-445" in out


def test_detail_text_labels_and_fix_versions():
    out = detail_text(issue(labels=["import", "regression"], fixVersions=[{"name": "1.2"}]))
    assert "labels: import, regression" in out
    assert "fixVersions: 1.2" in out


def test_detail_text_components_and_attachments():
    out = detail_text(
        issue(
            components=[{"name": "Mobile"}, {"name": "Checkout"}],
            attachment=[{"filename": "design.png", "mimeType": "image/png"}],
        )
    )

    assert "components: Mobile, Checkout" in out
    assert "attachments: design.png (image/png)" in out


# --- comments_text ----------------------------------------------------------


def test_comments_text_none():
    assert comments_text("PROJ-443", []) == "PROJ-443 has no comments."


def test_comments_text_renders_author_date_body():
    out = comments_text(
        "PROJ-443",
        [
            {
                "author": {"displayName": "Jane Doe"},
                "created": "2026-08-01T09:00:00.000-0700",
                "body": adf("Reproduced on staging."),
            }
        ],
    )
    assert "PROJ-443 - 1 comment(s)" in out
    assert "--- Jane Doe on 2026-08-01" in out
    assert "Reproduced on staging." in out


def test_comments_text_marks_empty_body():
    out = comments_text("PROJ-443", [{"author": {"displayName": "Jane Doe"}, "body": None}])
    assert "(empty)" in out


# --- transitions_text -------------------------------------------------------


def test_transitions_text_none():
    assert transitions_text("PROJ-443", []) == "PROJ-443 has no available transitions."


def test_transitions_text_lists_names():
    out = transitions_text("PROJ-443", [{"name": "Done"}, {"name": "In Review"}])
    assert out == "PROJ-443 can move to: Done, In Review"


@pytest.mark.parametrize("bad", [None, "string", 42, []])
def test_name_helpers_tolerate_junk(bad):
    """Jira occasionally omits or reshapes these; nothing should raise."""
    summary_line({"key": "PROJ-1", "fields": {"status": bad, "assignee": bad}})
