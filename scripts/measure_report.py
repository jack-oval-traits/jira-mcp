"""Aggregate the records JIRA_MCP_MEASURE writes, from any log stream.

    docker logs jira-mcp-jira-mcp-1 2>&1 | python3 scripts/measure_report.py

Reads stdin, ignores anything that is not a measurement record, and prints
per-tool totals. Pure stdlib on purpose -- it runs on the host, against
`docker logs`, without needing the image's dependencies.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jira_mcp.measure import parse, summarize  # noqa: E402

# "from jira", not "raw": the field allowlist has already been applied by the
# time these bytes exist, so this column is smaller than what a naive client
# would have received. See "Against real traffic" in README.md.
HEADER = f"{'tool':<17}{'calls':>6}{'from jira':>12}{'sent':>10}{'saved':>8}{'median':>9}"


def main() -> None:
    records = parse(sys.stdin)
    if not records:
        print(
            "No measurement records found.\n"
            "Set JIRA_MCP_MEASURE=1 in .env, recreate the container, and make a tool call.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    rows = summarize(records)
    print(HEADER)
    print("-" * len(HEADER))
    for row in rows:
        if row["tool"] == "all":
            print("-" * len(HEADER))
        saved = f"{row['saved']:.1%}" if row["saved"] is not None else "--"
        print(
            f"{row['tool']:<17}{row['calls']:>6}{row['raw_tokens']:>12,}"
            f"{row['out_tokens']:>10,}{saved:>8}{row['median_ms']:>8.0f}ms"
        )

    total = rows[-1]
    if total["saved"] is not None:
        kept = total["raw_tokens"] - total["out_tokens"]
        print(f"\n{kept:,} tokens not spent across {total['calls']} calls.")
    if any(r.get("estimated") for r in records):
        print("(tiktoken unavailable -- counts are 4-chars-per-token estimates)")


if __name__ == "__main__":
    main()
