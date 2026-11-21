"""The CLI network's server side is deleted: the shipped CLI is peer to peer and calls no backend route.
Meetings keeps the raw-WS gateway door; typed chat keeps its send."""

import uuid
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.routing import Match, Mount

from main import app

GONE = (
    "/api/v1/network",
    "/api/v1/internal/messaging/network",
    "/api/v1/internal/network",
    "/api/v1/internal/a2a",
    "/api/v1/settings/agent-keys",
    "/api/v1/sessions",
    "/api/v1/memory/drafts",
    "/api/v1/local-sessions/device",
    "/api/v1/gateway/network",
    "/a2a",
    "/mcp",
    "/.well-known/oauth-protected-resource",
    "/.well-known/openai-apps-challenge",
)


def _routed(path: str, scope_type: str = "http", method: str = "GET") -> list:
    scope = {"type": scope_type, "path": path, "root_path": "", "headers": [], "method": method}
    # Routers are included lazily; a root Mount would match every path, and none is left.
    return [r for r in app.routes if r.matches(scope)[0] is not Match.NONE]


def test_no_network_routes_mounted():
    app.openapi_schema = None
    assert not [p for p in app.openapi()["paths"] if p.startswith(GONE)]
    assert not [r for r in app.routes if isinstance(r, Mount)]  # the hosted MCP mounted at "/"
    for path in ("/mcp", "/a2a/org_x/agent/agent-card.json", "/api/v1/network/wake", "/api/v1/settings/agent-keys"):
        for method in ("GET", "POST", "PUT", "DELETE"):
            assert not _routed(path, method=method), f"{method} {path}"
    assert not _routed("/api/v1/gateway/network", "websocket")
    assert _routed("/api/v1/gateway/meet", "websocket")  # Meetings' audio door stays


def test_meet_audio_url_still_builds(monkeypatch):
    from core.config import settings
    from core.voice.meetings import meeting_bots

    monkeypatch.setattr(settings, "GATEWAY_RELAY_URL", "wss://bv.example.test/api/v1/gateway/ws")
    url = urlsplit(meeting_bots.audio_url("room-1", "tick.et"))
    assert (url.scheme, url.netloc, url.path) == ("wss", "bv.example.test", "/api/v1/gateway/meet/")
    assert parse_qs(url.query) == {"room": ["room-1"], "token": ["tick.et"]}


@pytest.mark.asyncio
async def test_typed_chat_send_without_network(async_client_org_admin, monkeypatch):
    """A person's typed message posts through the one human writer, waking its agents as before."""
    from core.services import messaging

    sent = AsyncMock(side_effect=lambda auth, room_id, request: messaging.CanonicalMessage.model_construct())
    monkeypatch.setattr(messaging, "create_human_message", sent)
    room = str(uuid.uuid4())
    body = {"clientMessageId": str(uuid.uuid4()), "text": "hi", "replyToMessageId": None, "threadRootMessageId": None}
    r = await async_client_org_admin.post(f"/api/v1/messaging/rooms/{room}/messages", json=body)
    assert r.status_code == 201, r.text
    auth, room_id, request = sent.await_args.args
    assert room_id == room and request.text == "hi" and not sent.await_args.kwargs  # no via, no wake=False
