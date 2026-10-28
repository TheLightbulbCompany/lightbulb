"""/api/v1/memory recall + remember, and the runtime plugin's /api/v1/internal/memory routes."""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from core.services import memory_api, workspace_memory  # noqa: E402
from main import app  # noqa: E402

PAGE = {"asOf": "2026-10-27T00:00:00+00:00", "usedEvidenceTokens": 5, "results": []}


@pytest.fixture
def member(monkeypatch):
    monkeypatch.setattr(memory_api.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_me", org_id="org_a", org_role="org:member"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_recall_requires_member():
    app.dependency_overrides.clear()
    assert TestClient(app).post("/api/v1/memory/recall", json={"question": "x"}).status_code in (401, 403)
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_me", org_id=None, org_role=None)
    try:
        assert TestClient(app).post("/api/v1/memory/recall", json={"question": "x"}).status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_recall_calls_sync_then_recall(member):
    order = []
    sync = AsyncMock(side_effect=lambda *a, **k: order.append("sync"))
    rec = AsyncMock(side_effect=lambda **k: order.append("recall") or PAGE)
    with (
        patch.object(memory_api.memory_sync, "sync_if_stale", sync),
        patch.object(memory_api.memory_recall, "recall", rec),
    ):
        r = member.post("/api/v1/memory/recall", json={"question": "what port?", "as_of": "2026-10-01T00:00:00Z"})
    assert r.status_code == 200 and r.json() == PAGE
    assert order == ["sync", "recall"]
    ns = workspace_memory.namespaces_for("org_a", "user_me")
    assert sync.await_args.kwargs["namespaces"] == ns and sync.await_args.args == ("org_a",)
    kw = rec.await_args.kwargs
    assert kw["namespaces"] == ns and kw["include_private"] is True and kw["question"] == "what port?"
    assert kw["budget_tokens"] == 8000 and kw["limit"] == 100
    assert kw["as_of"] == datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("q", ["", "   ", "x" * 2001])
def test_recall_rejects_a_bad_question_before_recall(member, q):
    rec = AsyncMock()
    with patch.object(memory_api.memory_recall, "recall", rec):
        assert member.post("/api/v1/memory/recall", json={"question": q}).status_code == 422
    rec.assert_not_awaited()


def test_remember_returns_neighbours_and_queues_when_parked(member):
    near = [{"id": "mem-n", "kind": "claim", "content": "api port: 8080", "status": "current"}]
    call, queue = AsyncMock(return_value=None), AsyncMock()
    with (
        patch.object(memory_api, "neighbours", AsyncMock(return_value=near)),
        patch.object(memory_api.network_member, "call_op", call),
        patch.object(memory_api.network_member, "queue_op", queue),
    ):
        r = member.post(
            "/api/v1/memory/remember",
            json={"subject": "api port", "fact": "9090", "kind": "fact", "replaces": ["e1"]},
        )
    assert r.json() == {"queued": True, "neighbours": near}
    sent = {
        "subject": "api port",
        "fact": "9090",
        "kind": "fact",
        "scope": "me",
        "replaces": ["e1"],
        "on_behalf_of": "user_me",
    }
    call.assert_awaited_once_with("org_a", "memory.remember", sent)
    queue.assert_awaited_once_with("org_a", "memory.remember", sent)


def test_remember_forwards_when_up(member):
    queue = AsyncMock()
    with (
        patch.object(memory_api, "neighbours", AsyncMock(return_value=[])),
        patch.object(memory_api.network_member, "call_op", AsyncMock(return_value={"id": "e2"})),
        patch.object(memory_api.network_member, "queue_op", queue),
    ):
        r = member.post(
            "/api/v1/memory/remember", json={"subject": "a", "fact": "b", "kind": "decision", "scope": "team"}
        )
    assert r.json() == {"queued": False, "neighbours": []}
    queue.assert_not_awaited()


def test_list_includes_status_and_history(monkeypatch):
    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    team = "/owners/org_a/"
    row = {
        "memoryRecordId": "mem-t1",
        "namespaces": [team],
        "createdAt": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "content": {"text": "Ana: api port: 9090 (since 2026-10-02; was 8080, Bo, until 2026-10-02)"},
        "metadata": {"kind": {"stringValue": "claim"}, "status": {"stringValue": "current"}},
    }
    client = MagicMock()
    client.list_memory_records.side_effect = lambda **kw: {
        "memoryRecordSummaries": [row] if kw["namespace"] == team else []
    }
    monkeypatch.setattr(workspace_memory, "_client", lambda: client)
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_me", org_id="org_a", org_role="org:member"
    )
    try:
        (item,) = TestClient(app).get("/api/v1/memory").json()["team"]
    finally:
        app.dependency_overrides.clear()
    assert item["status"] == "current" and item["speaker"] == "Ana"
    assert item["history"] == [{"value": "8080", "speaker": "Bo", "occurredAt": "2026-10-02T00:00:00+00:00"}]
    assert item["source"]["sourceId"] == "mem-t1"


def test_delete_forgets_the_entry_and_drops_the_mirror(member):
    rid = "mem-" + "a1" * 20
    forget = AsyncMock()
    drop = AsyncMock()
    with (
        patch("routers.memory.workspace_memory.delete_memory", AsyncMock()),
        patch("routers.memory.memory_sync.entry_for", AsyncMock(return_value="e7")),
        patch("routers.memory.memory_sync.drop_mirror", drop),
        patch("routers.memory.memory_api.forget", forget),
    ):
        assert member.delete(f"/api/v1/memory/{rid}").status_code == 204
    forget.assert_awaited_once_with("org_a", "e7")
    drop.assert_awaited_once_with("org_a", "e7")


def test_forget_queues_when_parked():
    call, queue = AsyncMock(return_value=None), AsyncMock()
    with (
        patch.object(memory_api.network_member, "call_op", call),
        patch.object(memory_api.network_member, "queue_op", queue),
    ):
        import asyncio

        asyncio.run(memory_api.forget("org_a", "e7"))
    queue.assert_awaited_once_with("org_a", "memory.forget", {"entry": "e7"})


# --- the Hermes plugin's channel --------------------------------------------------------------


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setattr(memory_api.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    seen = []

    async def ok(owner_id, request):
        seen.append(owner_id)
        return {}

    monkeypatch.setattr("routers.memory.container_auth._require_hook_token", ok)
    return TestClient(app), seen


def test_internal_recall_runs_as_the_workspace(hook):
    client, seen = hook
    rec = AsyncMock(return_value=PAGE)
    with (
        patch.object(memory_api.memory_sync, "sync_if_stale", AsyncMock()),
        patch.object(memory_api.memory_recall, "recall", rec),
    ):
        r = client.post("/api/v1/internal/memory/recall/org_a", json={"question": "q", "budget_tokens": 400})
    assert r.json() == PAGE and seen == ["org_a"]
    kw = rec.await_args.kwargs
    assert kw["include_private"] is False and "private" not in kw["namespaces"] and kw["budget_tokens"] == 400


def test_internal_routes_refuse_without_the_hook_token(monkeypatch):
    assert TestClient(app).post("/api/v1/internal/memory/recall/org_a", json={"question": "q"}).status_code == 401
    assert (
        TestClient(app)
        .post("/api/v1/internal/memory/remember/org_a", json={"subject": "s", "fact": "f", "kind": "fact"})
        .status_code
        == 401
    )


def test_internal_remember_takes_on_behalf_of_from_the_payload(hook):
    client, _ = hook
    call = AsyncMock(return_value={})
    with (
        patch.object(memory_api, "neighbours", AsyncMock(return_value=[])),
        patch.object(memory_api.network_member, "call_op", call),
    ):
        r = client.post(
            "/api/v1/internal/memory/remember/org_a",
            json={"subject": "s", "fact": "f", "kind": "lesson", "scope": "team", "on_behalf_of": "Ana"},
        )
    assert r.json() == {"queued": False, "neighbours": []}
    assert call.await_args.args[2]["on_behalf_of"] == "Ana"


async def test_hosted_recall_tool_rejects_a_bad_question_as_a_tool_error(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from core.services import hosted_mcp

    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_: AuthContext(user_id="user_me", org_id="org_a"))
    with pytest.raises(ToolError):
        await hosted_mcp.recall.__wrapped__("")
