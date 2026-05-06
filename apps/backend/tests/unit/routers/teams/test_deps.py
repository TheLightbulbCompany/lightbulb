import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from core.repositories.paperclip_repo import PaperclipCompany
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
def active_company():
    return PaperclipCompany(
        user_id="u1",
        org_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu_xyz",
        paperclip_password_encrypted="ENC",
        service_token_encrypted="TOK",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_resolve_returns_context_for_active_company(auth_ctx, active_company):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=active_company)
    session_factory = AsyncMock(return_value="cookie-value")

    ctx = await resolve_teams_context(
        auth=auth_ctx,
        repo=repo,
        session_factory=session_factory,
    )

    assert isinstance(ctx, TeamsContext)
    assert ctx.user_id == "u1"
    assert ctx.org_id == "o1"
    assert ctx.company_id == "co_abc"
    assert ctx.paperclip_user_id == "pcu_xyz"
    assert ctx.session_cookie == "cookie-value"
    session_factory.assert_awaited_once_with("u1")


@pytest.mark.asyncio
async def test_resolve_raises_when_no_company(auth_ctx):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    with pytest.raises(TeamsContextError) as exc:
        await resolve_teams_context(
            auth=auth_ctx,
            repo=repo,
            session_factory=AsyncMock(),
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_resolve_raises_202_when_provisioning(auth_ctx, active_company):
    active_company.status = "provisioning"
    repo = MagicMock()
    repo.get = AsyncMock(return_value=active_company)
    with pytest.raises(TeamsContextError) as exc:
        await resolve_teams_context(
            auth=auth_ctx,
            repo=repo,
            session_factory=AsyncMock(),
        )
    assert exc.value.status_code == 202
