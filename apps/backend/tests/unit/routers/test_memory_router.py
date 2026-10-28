"""/api/v1/memory -- a person reviews what the workspace's agents remember."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import memory as mod  # noqa: E402

RID = "mem-" + "a1" * 20


@pytest.fixture
def member(monkeypatch):
    monkeypatch.setattr(mod.memory_sync, "entry_for", AsyncMock(return_value=None))
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_me", org_id="org_a", org_role="org:member"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_get_lists_for_the_caller_only(member):
    lst = AsyncMock(return_value={"private": [], "team": [], "truncated": False})
    with patch.object(mod.workspace_memory, "list_memories", lst):
        assert member.get("/api/v1/memory").json() == {"private": [], "team": [], "truncated": False}
    lst.assert_awaited_once_with("org_a", "user_me")


def test_delete_is_204_and_passes_auth_ids(member):
    rm = AsyncMock()
    with patch.object(mod.workspace_memory, "delete_memory", rm):
        assert member.delete(f"/api/v1/memory/{RID}").status_code == 204
    rm.assert_awaited_once_with("org_a", "user_me", RID)


@pytest.mark.parametrize("bad", ["x", "mem-short", "mem-" + "a" * 60, "mem-" + "a/" * 20])
def test_malformed_id_is_404_without_an_aws_call(member, bad):
    rm = AsyncMock()
    with patch.object(mod.workspace_memory, "delete_memory", rm):
        assert member.delete(f"/api/v1/memory/{bad}").status_code == 404
    rm.assert_not_awaited()


def test_lookup_error_is_404_runtime_error_is_502(member):
    with patch.object(mod.workspace_memory, "delete_memory", AsyncMock(side_effect=LookupError("x"))):
        assert member.delete(f"/api/v1/memory/{RID}").status_code == 404
    with patch.object(mod.workspace_memory, "delete_memory", AsyncMock(side_effect=RuntimeError("x"))):
        assert member.delete(f"/api/v1/memory/{RID}").status_code == 502


def test_personal_caller_is_refused():
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_me", org_id=None, org_role=None)
    try:
        assert TestClient(app).get("/api/v1/memory").status_code == 409
    finally:
        app.dependency_overrides.clear()


def _denied(op, code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "secret aws text"}}, op)


def test_get_aws_access_denied_is_a_fixed_502(member, monkeypatch):
    from unittest.mock import MagicMock

    from core.services import workspace_memory as wm

    client = MagicMock()
    client.list_memory_records.side_effect = _denied("ListMemoryRecords", "AccessDeniedException")
    monkeypatch.setattr(wm, "_client", lambda: client)
    monkeypatch.setattr(wm.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    r = member.get("/api/v1/memory")
    assert r.status_code == 502 and "secret" not in r.text


def test_delete_aws_throttling_is_a_fixed_502(member, monkeypatch):
    from unittest.mock import MagicMock

    from core.services import workspace_memory as wm

    client = MagicMock()
    client.get_memory_record.side_effect = _denied("GetMemoryRecord", "ThrottlingException")
    monkeypatch.setattr(wm, "_client", lambda: client)
    monkeypatch.setattr(wm.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    r = member.delete(f"/api/v1/memory/{RID}")
    assert r.status_code == 502 and "secret" not in r.text


@pytest.mark.parametrize("action,fn", [("share", "share_memory"), ("confirm", "confirm_memory")])
def test_share_and_confirm_return_the_team_id(member, action, fn):
    call = AsyncMock(return_value="mem-" + "b" * 40)
    with patch.object(mod.workspace_memory, fn, call):
        resp = member.post(f"/api/v1/memory/{RID}/{action}")
    assert resp.status_code == 200 and resp.json() == {"id": "mem-" + "b" * 40}
    call.assert_awaited_once_with("org_a", "user_me", RID)


@pytest.mark.parametrize("action,fn", [("share", "share_memory"), ("confirm", "confirm_memory")])
def test_share_and_confirm_422_when_too_long(member, action, fn):
    with patch.object(mod.workspace_memory, fn, AsyncMock(side_effect=ValueError("x"))):
        resp = member.post(f"/api/v1/memory/{RID}/{action}")
    assert resp.status_code == 422 and resp.json()["detail"] == "This memory is too long to share or confirm."
