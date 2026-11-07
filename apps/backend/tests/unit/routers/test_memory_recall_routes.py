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


def test_recall_starts_the_team_sync_and_recalls(member):
    sync = AsyncMock(return_value=True)
    rec = AsyncMock(return_value=PAGE)
    with (
        patch.object(memory_api.memory_sync, "sync_if_stale", sync),
        patch.object(memory_api.memory_recall, "recall", rec),
    ):
        r = member.post("/api/v1/memory/recall", json={"question": "what port?", "as_of": "2026-10-01T00:00:00Z"})
    assert r.status_code == 200 and r.json() == PAGE
    ns = workspace_memory.namespaces_for("org_a", "user_me")
    assert sync.call_args.args == ("org_a",)
    assert sync.call_args.kwargs["namespaces"] == workspace_memory.namespaces_for("org_a", None)
    kw = rec.await_args.kwargs
    assert kw["namespaces"] == ns and kw["include_private"] is True and kw["question"] == "what port?"
    assert kw["budget_tokens"] == 8000 and kw["limit"] == 100
    assert kw["as_of"] == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert kw["use_expand"] is True


async def test_recall_returns_without_waiting_for_the_sync(monkeypatch):
    import asyncio

    monkeypatch.setattr(memory_api.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    started, never = asyncio.Event(), asyncio.Event()

    async def slow_sync(*a, **k):
        started.set()
        await never.wait()  # the RPCs of a real sync outlast every client's timeout

    with (
        patch.object(memory_api.memory_sync, "sync_if_stale", slow_sync),
        patch.object(memory_api.memory_recall, "recall", AsyncMock(return_value=PAGE)),
    ):
        page = await asyncio.wait_for(memory_api.recall_page("org_a", None, "q"), timeout=1)
        assert page == PAGE
        await asyncio.wait_for(started.wait(), timeout=1)  # it was started, not awaited
        (task,) = memory_api._syncs
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not memory_api._syncs


async def test_a_failed_background_sync_is_logged_not_raised(monkeypatch, caplog):
    import asyncio

    monkeypatch.setattr(memory_api.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    with (
        patch.object(memory_api.memory_sync, "sync_if_stale", AsyncMock(side_effect=RuntimeError("x"))),
        patch.object(memory_api.memory_recall, "recall", AsyncMock(return_value=PAGE)),
        caplog.at_level("WARNING", logger="core.services.memory_api"),
    ):
        assert await memory_api.recall_page("org_a", None, "q") == PAGE
        for _ in range(3):
            await asyncio.sleep(0)
    assert "memory sync failed error=RuntimeError" in caplog.text and not memory_api._syncs


async def test_sweep_syncs_only_stale_workspaces(monkeypatch):
    import time

    monkeypatch.setattr(memory_api.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    fresh = {"org_fresh": int(time.time()), "org_stale": int(time.time()) - memory_api.SWEEP_INTERVAL_S - 1}
    ran = AsyncMock(return_value={})
    monkeypatch.setattr(memory_api.container_repo, "scan_all", AsyncMock(return_value=[{"owner_id": o} for o in fresh]))
    with (
        patch.object(memory_api.memory_sync, "_get", lambda owner_id, sk: {"synced_at": fresh[owner_id]}),
        patch.object(memory_api.memory_sync, "sync_workspace", ran),
    ):
        assert await memory_api.sweep_once() == 1
    assert [c.args for c in ran.await_args_list] == [("org_stale",)]
    assert ran.await_args.kwargs["namespaces"] == workspace_memory.namespaces_for("org_stale", None)


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
        patch.object(
            memory_api.network_member,
            "call_op",
            AsyncMock(return_value={"stored": True, "entry_id": "e2", "neighbours": []}),
        ),
        patch.object(memory_api.network_member, "queue_op", queue),
    ):
        r = member.post(
            "/api/v1/memory/remember", json={"subject": "a", "fact": "b", "kind": "decision", "scope": "team"}
        )
    assert r.json() == {"queued": False, "neighbours": []}
    queue.assert_not_awaited()


@pytest.mark.parametrize("reason", ["secret:aws_key", "too_long", "team_off"])
def test_remember_answers_the_members_refusal_as_422(member, reason):
    refused = {"stored": False, "refused": reason, "neighbours": []}  # ipc.rs memory_remember
    with (
        patch.object(memory_api, "neighbours", AsyncMock(return_value=[])),
        patch.object(memory_api.network_member, "call_op", AsyncMock(return_value=refused)),
    ):
        r = member.post("/api/v1/memory/remember", json={"subject": "a", "fact": "b", "kind": "fact"})
    assert r.status_code == 422 and r.json()["detail"] == f"Not remembered: {reason}"


def test_parked_remember_of_a_secret_is_refused_and_never_queued(member):
    queue = AsyncMock()
    with (
        patch.object(memory_api, "neighbours", AsyncMock(return_value=[])),
        patch.object(memory_api.network_member, "call_op", AsyncMock(return_value=None)),
        patch.object(memory_api.network_member, "queue_op", queue),
    ):
        r = member.post(
            "/api/v1/memory/remember", json={"subject": "deploy", "fact": "the api key is abc123", "kind": "fact"}
        )
    assert r.status_code == 422 and r.json()["detail"] == "Not remembered: secret"
    queue.assert_not_awaited()


def test_a_credential_shaped_fact_is_refused_before_any_call_or_queue(member):
    near, call, queue = AsyncMock(return_value=[]), AsyncMock(return_value=None), AsyncMock()  # parked
    with (
        patch.object(memory_api, "neighbours", near),
        patch.object(memory_api.network_member, "call_op", call),
        patch.object(memory_api.network_member, "queue_op", queue),
    ):
        r = member.post(
            "/api/v1/memory/remember",
            json={"subject": "ci", "fact": "use ghp_" + "a" * 36 + " for pushes", "kind": "fact"},
        )
    assert r.status_code == 422 and r.json()["detail"] == "Not remembered: secret:vendor_key"
    near.assert_not_awaited(), call.assert_not_awaited(), queue.assert_not_awaited()


async def test_hosted_remember_tool_raises_the_refusal_as_a_tool_error(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from core.services import hosted_mcp

    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_: AuthContext(user_id="user_me", org_id="org_a"))
    refused = {"stored": False, "refused": "too_long", "neighbours": []}
    with (
        patch.object(memory_api, "neighbours", AsyncMock(return_value=[])),
        patch.object(memory_api.network_member, "call_op", AsyncMock(return_value=refused)),
        pytest.raises(ToolError, match="too_long"),
    ):
        await hosted_mcp.remember.__wrapped__("s", "f", "fact")


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
    assert item["source"] == "room"


def test_delete_forgets_the_entry_and_drops_the_mirror(member):
    rid = "mem-" + "a1" * 20
    forget = AsyncMock()
    drop = AsyncMock()
    with (
        patch("routers.memory.workspace_memory.check_deletable", AsyncMock()),
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
    assert kw["use_expand"] is False and kw["use_rerank"] is False  # no model call before the Router's


def test_internal_recall_in_a_dm_adds_that_persons_private_scope(hook):
    client, _ = hook
    rec = AsyncMock(return_value=PAGE)
    with (
        patch.object(memory_api.memory_sync, "sync_if_stale", AsyncMock()),
        patch.object(memory_api.memory_recall, "recall", rec),
    ):
        r = client.post("/api/v1/internal/memory/recall/org_a", json={"question": "q", "member": "user_ana"})
        assert r.status_code == 200
        kw = rec.await_args.kwargs
        assert kw["include_private"] is True and kw["namespaces"]["private"] == "/owners/org_a:user_ana/"
        for bad in ("Ana", "user_a/../x", "user_a:b"):
            r = client.post("/api/v1/internal/memory/recall/org_a", json={"question": "q", "member": bad})
            assert r.status_code == 422, bad


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
    call = AsyncMock(return_value={"stored": True, "entry_id": "e1", "neighbours": []})
    with (
        patch.object(memory_api, "neighbours", AsyncMock(return_value=[])),
        patch.object(memory_api.network_member, "call_op", call),
        patch("core.services.effect_doors.live_attempts", AsyncMock(return_value=[])),  # no phone caller's task
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


async def test_forget_queues_when_the_call_raises():
    queue = AsyncMock()
    with (
        patch.object(memory_api.network_member, "call_op", AsyncMock(side_effect=OSError("boom"))),
        patch.object(memory_api.network_member, "queue_op", queue),
    ):
        await memory_api.forget("org_a", "e7")
    queue.assert_awaited_once_with("org_a", "memory.forget", {"entry": "e7"})


def test_another_members_private_mirror_is_404_before_any_forget(member, monkeypatch):
    rid = "mem-" + "a1" * 20
    order = []
    client = MagicMock()
    client.batch_delete_memory_records.side_effect = lambda **kw: order.append("deleted") or {"failedRecords": []}
    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    monkeypatch.setattr(workspace_memory, "_client", lambda: client)
    forget = AsyncMock(side_effect=lambda *a: order.append("forget"))
    with (
        patch("routers.memory.memory_sync.entry_for", AsyncMock(return_value="e7")),
        patch("routers.memory.memory_sync.drop_mirror", AsyncMock()),
        patch("routers.memory.memory_api.forget", forget),
    ):
        client.get_memory_record.return_value = {
            "memoryRecord": {"memoryRecordId": rid, "namespaces": ["/owners/org_a:user_ana/"]}
        }
        assert member.delete(f"/api/v1/memory/{rid}").status_code == 404  # user_ana's note, user_me asking
        assert order == []
        client.get_memory_record.return_value = {
            "memoryRecord": {"memoryRecordId": rid, "namespaces": ["/owners/org_a:user_me/"]}
        }
        assert member.delete(f"/api/v1/memory/{rid}").status_code == 204
    assert order == ["forget", "deleted"]


def test_delete_sends_the_forget_before_deleting_the_record_and_survives_a_failing_call(member):
    rid = "mem-" + "a1" * 20
    order = []
    queue = AsyncMock(side_effect=lambda *a: order.append("queued"))
    rm = AsyncMock(side_effect=lambda *a: order.append("deleted"))
    with (
        patch("routers.memory.workspace_memory.check_deletable", AsyncMock()),
        patch("routers.memory.workspace_memory.delete_memory", rm),
        patch("routers.memory.memory_sync.entry_for", AsyncMock(return_value="e7")),
        patch("routers.memory.memory_sync.drop_mirror", AsyncMock()),
        patch.object(memory_api.network_member, "call_op", AsyncMock(side_effect=OSError("boom"))),
        patch.object(memory_api.network_member, "queue_op", queue),
    ):
        assert member.delete(f"/api/v1/memory/{rid}").status_code == 204
    assert order == ["queued", "deleted"]


def test_delete_without_mirror_rows_sends_no_forget(member):
    rid = "mem-" + "a1" * 20
    forget = AsyncMock()
    with (
        patch("routers.memory.workspace_memory.delete_memory", AsyncMock()),
        patch("routers.memory.memory_sync.entry_for", AsyncMock(return_value=None)),
        patch("routers.memory.memory_api.forget", forget),
    ):
        assert member.delete(f"/api/v1/memory/{rid}").status_code == 204
    forget.assert_not_awaited()


def test_recall_treats_a_naive_as_of_as_utc(member):
    rec = AsyncMock(return_value=PAGE)
    with (
        patch.object(memory_api.memory_sync, "sync_if_stale", AsyncMock()),
        patch.object(memory_api.memory_recall, "recall", rec),
    ):
        member.post("/api/v1/memory/recall", json={"question": "q", "as_of": "2026-10-01T00:00:00"})
    assert rec.await_args.kwargs["as_of"] == datetime(2026, 10, 1, tzinfo=timezone.utc)
