"""Thin async client over the Jira Cloud REST v3 API.

Deliberately not a general-purpose SDK. Every method takes an explicit field
allowlist and returns raw JSON; shaping happens in normalize.py.
"""

from __future__ import annotations

from typing import Any

import httpx

from .measure import record_raw

API = "/rest/api/3"


class JiraError(RuntimeError):
    """A Jira API call failed. Message is already caller-readable."""


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(email, api_token),
            timeout=httpx.Timeout(30.0),
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise JiraError(f"Could not reach Jira: {exc}") from exc

        if r.status_code >= 400:
            raise JiraError(_error_message(r))
        # Jira's bytes, before normalize.py touches them -- the "before" half of
        # the measurement. No-op unless JIRA_MCP_MEASURE is set.
        record_raw(r.text)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    # --- reads ------------------------------------------------------------

    async def search(
        self,
        jql: str,
        fields: tuple[str, ...],
        limit: int,
        page_token: str | None = None,
    ) -> dict:
        """POST /search/jql -- the v3 replacement for the removed /search."""
        payload: dict[str, Any] = {
            "jql": jql,
            "fields": list(fields),
            "maxResults": limit,
        }
        if page_token:
            payload["nextPageToken"] = page_token
        return await self._request("POST", f"{API}/search/jql", json=payload)

    async def issue(self, key: str, fields: tuple[str, ...]) -> dict:
        return await self._request(
            "GET", f"{API}/issue/{key}", params={"fields": ",".join(fields)}
        )

    async def comments(self, key: str, limit: int) -> dict:
        return await self._request(
            "GET",
            f"{API}/issue/{key}/comment",
            params={"maxResults": limit, "orderBy": "-created"},
        )

    async def transitions(self, key: str) -> dict:
        return await self._request("GET", f"{API}/issue/{key}/transitions")

    async def find_user(self, query: str) -> list[dict]:
        return await self._request(
            "GET", f"{API}/user/search", params={"query": query, "maxResults": 5}
        )

    async def issue_types(self, project_key: str) -> dict:
        """Issue types creatable in a project, from createmeta.

        The only way to answer "does this project have Subtask?" without
        creating one -- project schemes differ, so a type existing in the site
        says nothing about a given project.
        """
        return await self._request(
            "GET", f"{API}/issue/createmeta/{project_key}/issuetypes"
        )

    async def link_types(self) -> dict:
        return await self._request("GET", f"{API}/issueLinkType")

    # --- writes -----------------------------------------------------------

    async def create(self, fields: dict) -> dict:
        return await self._request("POST", f"{API}/issue", json={"fields": fields})

    async def update(self, key: str, fields: dict) -> None:
        await self._request("PUT", f"{API}/issue/{key}", json={"fields": fields})

    async def link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        """Link two issues. Direction matters: `inward_key` is the one the type's
        inward description reads from (for "Blocks", inward is blocked by outward).
        """
        await self._request(
            "POST",
            f"{API}/issueLink",
            json={
                "type": {"name": link_type},
                "inwardIssue": {"key": inward_key},
                "outwardIssue": {"key": outward_key},
            },
        )

    async def comment(self, key: str, body_adf: dict) -> dict:
        return await self._request(
            "POST", f"{API}/issue/{key}/comment", json={"body": body_adf}
        )

    async def transition(self, key: str, transition_id: str) -> None:
        await self._request(
            "POST",
            f"{API}/issue/{key}/transitions",
            json={"transition": {"id": transition_id}},
        )


def _error_message(r: httpx.Response) -> str:
    """Jira spreads errors across two differently-shaped keys."""
    detail = ""
    try:
        body = r.json()
        parts = list(body.get("errorMessages") or [])
        parts += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
        detail = "; ".join(parts)
    except Exception:  # noqa: BLE001 - non-JSON error bodies happen (proxies, 502s)
        detail = r.text[:200]

    if r.status_code == 401:
        return "Jira rejected the credentials (401). Check JIRA_EMAIL and JIRA_API_TOKEN."
    if r.status_code == 404:
        return f"Not found (404). {detail}".strip()
    return f"Jira returned {r.status_code}. {detail}".strip()
