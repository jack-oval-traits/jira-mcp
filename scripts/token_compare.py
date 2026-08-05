"""Measure what the adapter actually saves, on your own issues.

Runs one JQL query twice -- once the way a naive client asks (`fields=*navigable`,
raw JSON) and once through this adapter's normalizer -- and counts tokens both
ways.

    docker compose run --rm --entrypoint python jira-mcp \
        scripts/token_compare.py 'project = PROJ ORDER BY updated DESC' 6

Counting uses tiktoken (a GPT tokenizer), so the absolute numbers are an
approximation for Claude. The *ratio* is what matters and it holds up.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import tiktoken

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jira_mcp.jira import API, JiraClient  # noqa: E402
from jira_mcp.normalize import (  # noqa: E402
    FIELDS_DETAIL,
    FIELDS_SUMMARY,
    detail_text,
    summary_block,
)

enc = tiktoken.get_encoding("cl100k_base")


def count(text: str) -> int:
    return len(enc.encode(text))


async def main(jql: str, limit: int) -> None:
    jira = JiraClient(
        base_url=os.environ["JIRA_BASE_URL"],
        email=os.environ["JIRA_EMAIL"],
        api_token=os.environ["JIRA_API_TOKEN"],
    )

    # Naive: everything Jira will volunteer, serialized as-is.
    raw = await jira._request(
        "POST",
        f"{API}/search/jql",
        json={"jql": jql, "fields": ["*navigable"], "maxResults": limit},
    )
    raw_tokens = count(json.dumps(raw, separators=(",", ":")))

    # Adapter: summary records only.
    lean = await jira.search(jql, FIELDS_SUMMARY, limit)
    lean_text = summary_block(lean.get("issues") or [])
    lean_tokens = count(lean_text)

    # Adapter: one full issue, for the description-heavy comparison.
    keys = [i["key"] for i in (lean.get("issues") or [])]
    detail_tokens = 0
    if keys:
        detail_tokens = count(detail_text(await jira.issue(keys[0], FIELDS_DETAIL)))

    await jira.aclose()

    n = len(lean.get("issues") or [])
    print(f"JQL: {jql}")
    print(f"Issues returned: {n}\n")
    print(f"  raw JSON, fields=*navigable : {raw_tokens:>7,} tokens")
    print(f"  adapter summary records     : {lean_tokens:>7,} tokens")
    if raw_tokens:
        print(f"  reduction                   : {1 - lean_tokens / raw_tokens:>7.1%}")
    if keys:
        print(f"\n  one full issue ({keys[0]}) via get_issue: {detail_tokens:,} tokens")
    print("\n--- adapter output ---")
    print(lean_text)


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "ORDER BY updated DESC"
    count_arg = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    asyncio.run(main(query, count_arg))
