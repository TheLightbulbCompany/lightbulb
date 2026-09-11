import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

import core.services.paperclip_session_cookie as sc
from core.repositories.paperclip_repo import PaperclipCompany
from routers.teams.deps import (
    TeamsContext,
    envelope_list,
    resolve_teams_context,
    TeamsContextError,
)


@pytest.fixture(autouse=True)
def _bind_key_and_singleflight(monkeypatch):
    # resolve_session_cookie mints an HS256 binding cookie on the cold
    # path, signed with PAPERCLIP_SERVICE_TOKEN_KEY.
    monkeypatch.setattr(sc.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-bind-secret", raising=False)
    yield
    # Evict the cold-mint singleflight buffer so a buffered cookie from
    # one test doesn't satisfy the next test's cold-mint.
    sc._cold.evict("u1")


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


class _FakeResponse:
    """Minimal ``.set_cookie`` recorder for resolve_session_cookie."""

    def __init__(self):
        self.cookies: list[dict] = []

    def set_cookie(self, **kwargs):
        self.cookies.append(kwargs)


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
    # Mint returns Better Auth's bare ``name=value`` session-cookie line.
    cookie_line = "__Secure-paperclip-default.session_token=cookie-value"
    session_factory = AsyncMock(return_value=cookie_line)

    # No cookie presented → cold-mint path: session_factory is awaited
    # once for "u1" and its result becomes the context cookie.
    ctx = await resolve_teams_context(
        auth=auth_ctx,
        repo=repo,
        session_factory=session_factory,
        cookies={},
        response=_FakeResponse(),
    )

    assert isinstance(ctx, TeamsContext)
    assert ctx.user_id == "u1"
    assert ctx.org_id == "o1"
    assert ctx.company_id == "co_abc"
    assert ctx.paperclip_user_id == "pcu_xyz"
    assert ctx.session_cookie == cookie_line
    session_factory.assert_awaited_once_with("u1")


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_owner,active_org", [("other-org", "o1"), ("u1", "o1"), ("o1", None)])
async def test_rejects_stale_company_scope_before_cookie_or_reconcile(active_company, stored_owner, active_org):
    from core.auth import AuthContext
    from fastapi import HTTPException

    active_company.org_id = stored_owner
    mint = AsyncMock(return_value="__Secure-paperclip-default.session_token=synthetic")
    reconcile = AsyncMock()
    response = _FakeResponse()
    with pytest.raises(HTTPException) as exc:
        await resolve_teams_context(
            auth=AuthContext(user_id="u1", org_id=active_org),
            repo=MagicMock(get=AsyncMock(return_value=active_company)),
            session_factory=mint,
            cookies={},
            response=response,
            reconcile=reconcile,
        )
    assert exc.value.status_code == (403 if active_org else 409)
    assert response.cookies == []
    mint.assert_not_awaited()
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_raises_when_no_company(auth_ctx):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    session_factory = AsyncMock()
    with pytest.raises(TeamsContextError) as exc:
        await resolve_teams_context(
            auth=auth_ctx,
            repo=repo,
            session_factory=session_factory,
            cookies={},
            response=_FakeResponse(),
        )
    assert exc.value.status_code == 409
    # Provisioning gate: no mint before the status check.
    session_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_self_heals_to_202_when_reconcile_clear(auth_ctx):
    """reconcile returning None (gate clear, provisioning launched) → 202
    'provisioning' instead of 409, so /teams self-heals like /chat instead of
    wedging the member on a terminal error."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    session_factory = AsyncMock()

    fired = {"n": 0}

    async def _reconcile():
        fired["n"] += 1
        return None  # gate clear → keep polling

    with pytest.raises(TeamsContextError) as exc:
        await resolve_teams_context(
            auth=auth_ctx,
            repo=repo,
            session_factory=session_factory,
            cookies={},
            response=_FakeResponse(),
            reconcile=_reconcile,
        )

    assert exc.value.status_code == 202  # not 409 — UI keeps polling
    assert fired["n"] == 1  # reconcile was awaited
    session_factory.assert_not_awaited()  # no mint before the row is active


@pytest.mark.asyncio
async def test_resolve_returns_402_when_reconcile_reports_blocked(auth_ctx):
    """reconcile returning a blocked-gate payload (no subscription/credits/
    provider) → 402 with that payload, so the UI shows the subscribe/setup CTA
    instead of spinning forever on a workspace that can never become active."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    session_factory = AsyncMock()

    blocked_payload = {"detail": "subscription required", "blocked": {"code": "subscription_required"}}

    async def _reconcile():
        return blocked_payload

    with pytest.raises(TeamsContextError) as exc:
        await resolve_teams_context(
            auth=auth_ctx,
            repo=repo,
            session_factory=session_factory,
            cookies={},
            response=_FakeResponse(),
            reconcile=_reconcile,
        )

    assert exc.value.status_code == 402
    assert exc.value.detail == blocked_payload
    session_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_raises_202_when_provisioning(auth_ctx, active_company):
    active_company.status = "provisioning"
    repo = MagicMock()
    repo.get = AsyncMock(return_value=active_company)
    session_factory = AsyncMock()
    with pytest.raises(TeamsContextError) as exc:
        await resolve_teams_context(
            auth=auth_ctx,
            repo=repo,
            session_factory=session_factory,
            cookies={},
            response=_FakeResponse(),
        )
    assert exc.value.status_code == 202
    session_factory.assert_not_awaited()
