"""Tests for the startup credential check's messaging.

The check itself needs a network round-trip, so what is pinned here is the part
that decides what a rejected credential is told to do about it. That advice is
the whole point of the check: a scoped token on a site URL fails silently, and
the message is what turns "no issues found" into a fixable error.
"""

from __future__ import annotations

from jira_mcp.server import _rejected_message

SITE = "https://your-org.atlassian.net"
CLOUD_ID = "bea749e9-bd84-4b75-bac2-3418b3eda63b"
GATEWAY = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"


def test_site_url_with_known_cloud_id_gives_the_exact_replacement() -> None:
    msg = _rejected_message(SITE, "you@example.com", 401, CLOUD_ID)

    assert f"JIRA_BASE_URL={GATEWAY}" in msg
    assert "scoped" in msg
    # The whole trap is that it looks like an empty result set, so say so.
    assert "empty" in msg


def test_site_url_without_cloud_id_says_where_to_find_it() -> None:
    msg = _rejected_message(SITE, "you@example.com", 401, None)

    assert "<cloudId>" in msg
    assert f"{SITE}/_edge/tenant_info" in msg


def test_gateway_url_blames_the_token_not_the_url() -> None:
    msg = _rejected_message(GATEWAY, "you@example.com", 403, None)

    assert "read:jira-work" in msg
    # Suggesting the gateway to someone already on it is noise.
    assert "JIRA_BASE_URL=" not in msg


def test_message_names_the_account_and_status() -> None:
    msg = _rejected_message(SITE, "ai@example.com", 403, CLOUD_ID)

    assert "ai@example.com" in msg
    assert "403" in msg
