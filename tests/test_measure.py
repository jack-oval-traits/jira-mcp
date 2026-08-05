"""Tests for reading measurement records back.

The recording side needs a live tool call; the reading side is pure and is
where the arithmetic that people will quote actually lives.
"""

from __future__ import annotations

from jira_mcp.measure import parse, summarize


def record(tool: str, raw: int, out: int, ms: float = 100.0) -> dict:
    return {
        "measure": "tool_call",
        "tool": tool,
        "raw_tokens": raw,
        "out_tokens": out,
        "jira_calls": 1,
        "ms": ms,
    }


def test_parse_skips_everything_that_is_not_a_record() -> None:
    lines = [
        "INFO:     Started server process [1]",
        '{"measure":"tool_call","tool":"get_issue","raw_tokens":100,"out_tokens":10}',
        "{not json at all",
        '{"level":"info","msg":"some other json line"}',
        "",
    ]

    records = parse(lines)

    assert len(records) == 1
    assert records[0]["tool"] == "get_issue"


def test_summarize_groups_by_tool_and_appends_a_total() -> None:
    rows = summarize([record("get_issue", 1000, 100), record("search_issues", 4000, 200)])

    assert [r["tool"] for r in rows] == ["get_issue", "search_issues", "all"]
    assert rows[-1]["raw_tokens"] == 5000
    assert rows[-1]["out_tokens"] == 300


def test_busiest_tool_comes_first() -> None:
    rows = summarize(
        [record("get_issue", 1, 1), record("add_comment", 1, 1), record("add_comment", 1, 1)]
    )

    assert rows[0]["tool"] == "add_comment"


def test_reduction_is_weighted_by_size_not_averaged_per_call() -> None:
    # One big search and one tiny ack. Averaging the ratios would report 45%;
    # what was actually saved is 9,900 of 10,100 tokens.
    rows = summarize([record("search_issues", 10_000, 100), record("add_comment", 100, 10)])

    assert rows[-1]["saved"] == 0.989


def test_zero_raw_tokens_does_not_divide_by_zero() -> None:
    # create_issue with a missing project returns before touching Jira.
    rows = summarize([record("create_issue", 0, 12)])

    assert rows[0]["saved"] is None


def test_no_records_summarizes_to_nothing() -> None:
    assert summarize([]) == []
