"""Image argument decoding and Jira multipart upload tests."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from jira_mcp import server
from jira_mcp.jira import JiraClient

PNG = b"\x89PNG\r\n\x1a\nsmall-test-payload"


def test_component_names_are_trimmed_and_empty_items_are_ignored() -> None:
    assert server._names("Mobile, Checkout, ,") == [
        {"name": "Mobile"},
        {"name": "Checkout"},
    ]


def test_create_and_update_send_component_names(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakeJira:
        async def create(self, fields: dict) -> dict:
            calls.append(("create", fields))
            return {"key": "PROJ-1"}

        async def update(self, key: str, fields: dict) -> None:
            calls.append((key, fields))

    monkeypatch.setattr(server, "client", lambda: FakeJira())

    created = asyncio.run(
        server.create_issue("New story", project="PROJ", components="Mobile, Checkout")
    )
    updated = asyncio.run(server.update_issue("PROJ-1", components="API"))

    assert created == "PROJ-1 created"
    assert updated == "PROJ-1 updated: components"
    assert calls[0][1]["components"] == [{"name": "Mobile"}, {"name": "Checkout"}]
    assert calls[1][1] == {"components": [{"name": "API"}]}


def test_dispatch_state_claim_and_rollback_only_touch_owned_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeJira:
        async def update(self, key: str, fields: dict) -> None:
            calls.append((key, fields))

    monkeypatch.setattr(server, "client", lambda: FakeJira())
    monkeypatch.setitem(server.CUSTOM_FIELDS, "codeyAgentTier", "customfield_20001")
    monkeypatch.setitem(server.CUSTOM_FIELDS, "codeyAgentProvider", "customfield_20002")

    claimed = asyncio.run(
        server.set_dispatch_state(
            "AVBALL-42",
            "claim",
            active_agent="Codey",
            run_id="run-42",
            dispatched_from="Ready for Dev",
            last_heartbeat="2026-09-21T22:30:00-05:00",
            session_link="http://dispatcher/api/v1/runs/run-42",
            codey_agent_tier="fast",
            codey_agent_provider="codex",
        )
    )
    rolled_back = asyncio.run(server.set_dispatch_state("AVBALL-42", "rollback"))

    assert claimed == "AVBALL-42 dispatch state -> claim"
    assert rolled_back == "AVBALL-42 dispatch state -> rollback"
    assert calls[0] == (
        "AVBALL-42",
        {
            server.CUSTOM_FIELDS["activeAgent"]: {"value": "Codey"},
            server.CUSTOM_FIELDS["runId"]: "run-42",
            server.CUSTOM_FIELDS["dispatchedFrom"]: "Ready for Dev",
            server.CUSTOM_FIELDS["lastHeartbeat"]: "2026-09-21T22:30:00-05:00",
            server.CUSTOM_FIELDS["sessionLink"]: "http://dispatcher/api/v1/runs/run-42",
            server.CUSTOM_FIELDS["codeyAgentTier"]: {"value": "Fast"},
            server.CUSTOM_FIELDS["codeyAgentProvider"]: {"value": "Codex"},
        },
    )
    assert set(calls[1][1]) == {
        server.CUSTOM_FIELDS["activeAgent"],
        server.CUSTOM_FIELDS["runId"],
        server.CUSTOM_FIELDS["dispatchedFrom"],
        server.CUSTOM_FIELDS["lastHeartbeat"],
        server.CUSTOM_FIELDS["sessionLink"],
    }
    assert all(value is None for value in calls[1][1].values())


def test_dispatch_state_rejects_unconfigured_codey_selection_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(server.CUSTOM_FIELDS, "codeyAgentTier", raising=False)
    monkeypatch.delitem(server.CUSTOM_FIELDS, "codeyAgentProvider", raising=False)

    result = asyncio.run(
        server.set_dispatch_state(
            "AVBALL-42",
            "claim",
            active_agent="Codey",
            run_id="run-42",
            dispatched_from="Ready for Dev",
            last_heartbeat="2026-09-21T22:30:00-05:00",
            session_link="http://dispatcher/api/v1/runs/run-42",
            codey_agent_tier="Fast",
            codey_agent_provider="Claude",
        )
    )

    assert result == (
        "Codey selection fields are not configured: set "
        "JIRA_FIELD_CODEY_AGENT_TIER, JIRA_FIELD_CODEY_AGENT_PROVIDER."
    )


def test_decode_image_accepts_plain_base64_and_infers_type() -> None:
    encoded = base64.b64encode(PNG).decode()

    image, media_type = server._decode_image("design.png", encoded, "")

    assert image == PNG
    assert media_type == "image/png"


def test_decode_image_accepts_a_data_uri() -> None:
    encoded = base64.b64encode(PNG).decode()

    image, media_type = server._decode_image(
        "design", f"data:image/png;base64,{encoded}", ""
    )

    assert image == PNG
    assert media_type == "image/png"


@pytest.mark.parametrize(
    ("filename", "encoded", "content_type", "message"),
    [
        ("../design.png", "eA==", "image/png", "without a path"),
        ("design.png", "not base64!", "image/png", "not valid base64"),
        ("design.png", "", "image/png", "empty"),
        ("design.txt", "eA==", "text/plain", "must be an image"),
    ],
)
def test_decode_image_rejects_invalid_input(
    filename: str, encoded: str, content_type: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        server._decode_image(filename, encoded, content_type)


def test_attach_image_decodes_and_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class FakeJira:
        async def attach(
            self, key: str, filename: str, content: bytes, content_type: str
        ) -> list[dict]:
            seen.update(
                key=key,
                filename=filename,
                content=content,
                content_type=content_type,
            )
            return [{"filename": filename}]

    monkeypatch.setattr(server, "client", lambda: FakeJira())
    encoded = base64.b64encode(PNG).decode()

    result = asyncio.run(server.attach_image("PROJ-1", "design.png", encoded))

    assert result == "PROJ-1 attached image design.png"
    assert seen == {
        "key": "PROJ-1",
        "filename": "design.png",
        "content": PNG,
        "content_type": "image/png",
    }


def test_attach_posts_jiras_required_multipart_request() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("X-Atlassian-Token")
        seen["content_type"] = request.headers.get("Content-Type")
        seen["body"] = await request.aread()
        return httpx.Response(200, json=[{"id": "42", "filename": "design.png"}])

    async def exercise() -> list[dict]:
        jira = JiraClient("https://example.atlassian.net", "me@example.com", "token")
        await jira._client.aclose()
        jira._client = httpx.AsyncClient(
            base_url="https://example.atlassian.net",
            transport=httpx.MockTransport(handler),
        )
        try:
            return await jira.attach("PROJ-1", "design.png", PNG, "image/png")
        finally:
            await jira.aclose()

    result = asyncio.run(exercise())

    assert result[0]["id"] == "42"
    assert seen["method"] == "POST"
    assert seen["path"] == "/rest/api/3/issue/PROJ-1/attachments"
    assert seen["token"] == "no-check"
    assert str(seen["content_type"]).startswith("multipart/form-data; boundary=")
    body = bytes(seen["body"])
    assert b'name="file"; filename="design.png"' in body
    assert b"Content-Type: image/png" in body
    assert PNG in body
