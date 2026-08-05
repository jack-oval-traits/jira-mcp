"""Optional per-call measurement of what the shaping layer saves.

scripts/token_compare.py answers "is this worth it" once, against a synthetic
second query. This answers it continuously, against real traffic: jira.py holds
Jira's actual response before normalize.py shapes it, so both sides of the ratio
are available at no extra request.

Narrower than the script, though. `raw_tokens` is what Jira sent for the fields
the adapter asked for -- the allowlist is applied upstream of anything visible
here, so these records measure the shaping alone and report a smaller saving
than token_compare.py. Measuring the counterfactual would mean every request
twice, which is not a thing to do on live traffic.

Off unless JIRA_MCP_MEASURE is set -- encoding every response costs a few ms,
and the records carry JQL and issue keys into wherever stdout goes.

One JSON object per tool call, on stdout:

    {"measure":"tool_call","tool":"search_issues","raw_tokens":26867,
     "out_tokens":232,"saved":0.991,"jira_calls":1,"ms":412.5,
     "arg":"project = PROJ ORDER BY updated DESC"}

Read them back with scripts/measure_report.py.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from collections.abc import Iterable
from contextvars import ContextVar
from functools import wraps
from typing import Any

ENABLED = (os.environ.get("JIRA_MCP_MEASURE") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Set by @measured for the duration of one tool call. jira.py adds to it from
# whatever depth it is called at -- update_issue makes two requests, and both
# should land against the one record.
_totals: ContextVar[dict[str, int] | None] = ContextVar("jira_mcp_measure", default=None)

_enc: Any = None
_estimated = False


def count(text: str) -> int:
    """Tokens in `text`, cl100k_base if tiktoken is installed.

    The image installs it (`.[measure]`), a bare `pip install jira-mcp` does
    not, and measurement should never be the thing that breaks a tool call --
    so fall back to the usual four-chars-a-token rule and say so in the record.
    """
    global _enc, _estimated
    if _enc is None:
        try:
            import tiktoken

            _enc = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 - any import/download failure is the same story
            _enc = False
            _estimated = True
    if _enc is False:
        return len(text) // 4
    return len(_enc.encode(text))


def record_raw(text: str) -> None:
    """Called by JiraClient with Jira's untouched response body."""
    totals = _totals.get()
    if totals is None:
        return
    totals["raw_tokens"] += count(text)
    totals["jira_calls"] += 1


def _arg(args: tuple, kwargs: dict) -> str:
    value = kwargs.get("jql") or kwargs.get("key") or (args[0] if args else "")
    text = str(value)
    return text[:80] + "…" if len(text) > 80 else text


def measured(fn):
    """Wrap a tool so its call is recorded. A no-op when measurement is off."""
    if not ENABLED:
        return fn

    @wraps(fn)
    async def wrapper(*args, **kwargs):
        totals = {"raw_tokens": 0, "jira_calls": 0}
        token = _totals.set(totals)
        started = time.perf_counter()
        try:
            out = await fn(*args, **kwargs)
        finally:
            _totals.reset(token)

        raw, text = totals["raw_tokens"], out if isinstance(out, str) else str(out)
        record = {
            "measure": "tool_call",
            "tool": fn.__name__,
            "raw_tokens": raw,
            "out_tokens": count(text),
            "jira_calls": totals["jira_calls"],
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "arg": _arg(args, kwargs),
        }
        if raw:
            record["saved"] = round(1 - record["out_tokens"] / raw, 3)
        if _estimated:
            record["estimated"] = True
        print(json.dumps(record), flush=True)
        return out

    return wrapper


# ---------------------------------------------------------------------------
# Reading the records back
# ---------------------------------------------------------------------------


def parse(lines: Iterable[str]) -> list[dict]:
    """Pull tool_call records out of a log stream.

    Everything else on stdout -- uvicorn's startup banner, tracebacks, the
    occasional print -- is skipped rather than fatal, because the usual source
    is `docker logs`, which interleaves all of it.
    """
    records = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("measure") == "tool_call":
            records.append(record)
    return records


def summarize(records: list[dict]) -> list[dict]:
    """Per-tool totals, busiest first, with an `all` row appended.

    Reduction is computed from the totals rather than by averaging the per-call
    ratios: one 27k-token search and one 400-token ack should not count equally
    toward the headline number.
    """
    by_tool: dict[str, list[dict]] = {}
    for record in records:
        by_tool.setdefault(record.get("tool", "?"), []).append(record)

    rows = [_row(tool, calls) for tool, calls in by_tool.items()]
    rows.sort(key=lambda r: r["calls"], reverse=True)
    if records:
        rows.append(_row("all", records))
    return rows


def _row(tool: str, calls: list[dict]) -> dict:
    raw = sum(c.get("raw_tokens", 0) for c in calls)
    out = sum(c.get("out_tokens", 0) for c in calls)
    return {
        "tool": tool,
        "calls": len(calls),
        "raw_tokens": raw,
        "out_tokens": out,
        "saved": round(1 - out / raw, 3) if raw else None,
        "median_ms": round(statistics.median([c.get("ms", 0) for c in calls]), 1),
    }
