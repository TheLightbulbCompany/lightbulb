import dataclasses

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from core.repositories.workspace_repo import Workspace
from routers.teams.deps import (
    TeamsContext,
    envelope_list,
    resolve_teams_context,
    TeamsContextError,
)


# ─── envelope_list ────────────────────────────────────────────────────────


def test_envelope_list_wraps_bare_array():
    rows = [{"id": "a"}, {"id": "b"}]
    assert envelope_list(rows, "goals") == {"goals": rows}


def test_envelope_list_passes_through_matching_envelope():
    upstream = {"goals": [{"id": "a"}]}
    assert envelope_list(upstream, "goals") == {"goals": [{"id": "a"}]}


def test_envelope_list_renames_items_envelope_to_target_key():
    upstream = {"items": [{"id": "a"}]}
    assert envelope_list(upstream, "goals") == {"goals": [{"id": "a"}]}


def test_envelope_list_drops_non_dict_rows():
    rows = [{"id": "a"}, "stray-string", None, {"id": "b"}]
    assert envelope_list(rows, "x") == {"x": [{"id": "a"}, {"id": "b"}]}


def test_envelope_list_handles_garbage_response():
    assert envelope_list(None, "x") == {"x": []}
    assert envelope_list("not-a-list", "x") == {"x": []}
    assert envelope_list(42, "x") == {"x": []}


@pytest.fixture
def auth_ctx():
    auth = MagicMock()
    auth.user_id = "u1"
    auth.org_id = "o1"
    return auth


@pytest.fixture
def active_ws():
    return Workspace(
        org_id="o1",
        company_id="",  # a natively provisioned workspace has no Paperclip company
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        provisioner_user_id="u1",
        member_user_ids={"u1"},
    )


def _repo(ws=None):
    return MagicMock(get_workspace=AsyncMock(return_value=ws))


async def _resolve(auth, repo, reconcile=None):
    return await resolve_teams_context(auth=auth, repo=repo, reconcile=reconcile)


def test_teams_ctx_has_no_session():
    """Phase 1 task 8b.8: no Paperclip session, user or company rides on the context."""
    assert [f.name for f in dataclasses.fields(TeamsContext)] == ["user_id", "org_id", "owner_id"]


@pytest.mark.asyncio
async def test_resolve_returns_context_for_member(auth_ctx, active_ws):
    """A member per the META row's member set gets a context; no identity row is read."""
    repo = _repo(active_ws)
    ctx = await _resolve(auth_ctx, repo)
    assert ctx == TeamsContext(user_id="u1", org_id="o1", owner_id="o1")
    repo.get_workspace.assert_awaited_once_with("o1")
    repo.get_identity.assert_not_called()


@pytest.mark.asyncio
async def test_no_active_org_is_refused_before_any_read():
    """No org claim → 409 workspace_required, nothing read or reconciled."""
    from core.auth import AuthContext
    from fastapi import HTTPException

    reconcile = AsyncMock()
    repo = _repo(None)
    with pytest.raises(HTTPException) as exc:
        await _resolve(AuthContext(user_id="u1", org_id=None), repo, reconcile)
    assert exc.value.status_code == 409
    reconcile.assert_not_awaited()
    repo.get_workspace.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("members", [set(), {"someone_else"}])
async def test_not_joined_member_gets_no_context(auth_ctx, active_ws, members):
    """The Teams authorization fence: an ACTIVE workspace does not authorize a
    human outside ``member_user_ids``. No reconcile → 409; with reconcile → 202."""
    active_ws.member_user_ids = members
    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(active_ws))
    assert exc.value.status_code == 409

    reconcile = AsyncMock(return_value=None)
    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(active_ws), reconcile)
    assert exc.value.status_code == 202
    reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_raises_when_no_workspace(auth_ctx):
    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(None))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_resolve_self_heals_to_202_when_reconcile_clear(auth_ctx):
    """reconcile returning None (gate clear, provisioning launched) → 202 so the UI keeps polling."""
    reconcile = AsyncMock(return_value=None)
    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(None), reconcile)
    assert exc.value.status_code == 202
    reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_returns_402_when_reconcile_reports_blocked(auth_ctx):
    """A blocked-gate payload → 402 with that payload (the subscribe/setup CTA)."""
    blocked_payload = {"detail": "subscription required", "blocked": {"code": "subscription_required"}}
    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(None), AsyncMock(return_value=blocked_payload))
    assert exc.value.status_code == 402
    assert exc.value.detail == blocked_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ws_status,expected",
    [("provisioning", 202), ("seeding", 202), ("failed", 503), ("disabled", 503)],
)
async def test_resolve_status_codes(auth_ctx, active_ws, ws_status, expected):
    active_ws.status = ws_status
    reconcile = AsyncMock()
    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(active_ws), reconcile)
    assert exc.value.status_code == expected
    reconcile.assert_not_awaited()
