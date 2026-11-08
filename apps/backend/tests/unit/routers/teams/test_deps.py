import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

import core.services.paperclip_session_cookie as sc
from core.repositories.paperclip_repo import PaperclipIdentity, PaperclipWorkspace
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
def active_ws():
    return PaperclipWorkspace(
        org_id="o1",
        company_id="co_abc",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        provisioner_user_id="u1",
        member_user_ids={"u1"},
    )


@pytest.fixture
def identity():
    return PaperclipIdentity(user_id="u1", paperclip_user_id="pcu_xyz", paperclip_password_encrypted="ENC")


def _repo(ws=None, ident=None):
    return MagicMock(get_workspace=AsyncMock(return_value=ws), get_identity=AsyncMock(return_value=ident))


async def _resolve(auth, repo, session_factory, reconcile=None, response=None):
    return await resolve_teams_context(
        auth=auth,
        repo=repo,
        session_factory=session_factory,
        cookies={},
        response=response or _FakeResponse(),
        reconcile=reconcile,
    )


@pytest.mark.asyncio
async def test_resolve_returns_context_for_joined_member(auth_ctx, active_ws, identity):
    repo = _repo(active_ws, identity)
    # Mint returns Better Auth's bare ``name=value`` session-cookie line.
    cookie_line = "__Secure-paperclip-default.session_token=cookie-value"
    session_factory = AsyncMock(return_value=cookie_line)

    # No cookie presented → cold-mint path: session_factory is awaited
    # once for "u1" and its result becomes the context cookie.
    ctx = await _resolve(auth_ctx, repo, session_factory)

    assert isinstance(ctx, TeamsContext)
    assert ctx.user_id == "u1"
    assert ctx.org_id == "o1"
    assert ctx.owner_id == "o1"
    # company from the workspace row, paperclip user from the identity row.
    assert ctx.company_id == "co_abc"
    assert ctx.paperclip_user_id == "pcu_xyz"
    assert ctx.session_cookie == cookie_line
    session_factory.assert_awaited_once_with("u1")
    repo.get_workspace.assert_awaited_once_with("o1")
    repo.get_identity.assert_awaited_once_with("u1")


@pytest.mark.asyncio
async def test_no_active_org_is_refused_before_any_read_or_mint(identity):
    """No org claim → 409 workspace_required, nothing read, minted or reconciled."""
    from core.auth import AuthContext
    from fastapi import HTTPException

    mint = AsyncMock()
    reconcile = AsyncMock()
    repo = _repo(None, identity)
    response = _FakeResponse()
    with pytest.raises(HTTPException) as exc:
        await _resolve(AuthContext(user_id="u1", org_id=None), repo, mint, reconcile, response)
    assert exc.value.status_code == 409
    assert response.cookies == []
    mint.assert_not_awaited()
    reconcile.assert_not_awaited()
    repo.get_workspace.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "members,pc_user_id",
    [
        (set(), "pcu_xyz"),  # removed from / never joined this workspace's company
        ({"someone_else"}, "pcu_xyz"),
    ],
)
async def test_not_joined_member_gets_no_context(auth_ctx, active_ws, identity, members, pc_user_id):
    """The Teams authorization fence: an ACTIVE workspace does not authorize a
    human outside ``member_user_ids``. No
    reconcile → 409; with reconcile → 202. Never a cookie mint."""
    active_ws.member_user_ids = members
    identity.paperclip_user_id = pc_user_id
    mint = AsyncMock(return_value="__Secure-paperclip-default.session_token=synthetic")
    response = _FakeResponse()

    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(active_ws, identity), mint, response=response)
    assert exc.value.status_code == 409

    reconcile = AsyncMock(return_value=None)
    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(active_ws, identity), mint, reconcile, response)
    assert exc.value.status_code == 202
    reconcile.assert_awaited_once()

    mint.assert_not_awaited()
    assert response.cookies == []


@pytest.mark.asyncio
async def test_resolve_raises_when_no_workspace(auth_ctx):
    session_factory = AsyncMock()
    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(None, None), session_factory)
    assert exc.value.status_code == 409
    # Provisioning gate: no mint before the status check.
    session_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_self_heals_to_202_when_reconcile_clear(auth_ctx):
    """reconcile returning None (gate clear, provisioning launched) → 202
    'provisioning' instead of 409, so /teams self-heals like /chat instead of
    wedging the member on a terminal error."""
    session_factory = AsyncMock()

    fired = {"n": 0}

    async def _reconcile():
        fired["n"] += 1
        return None  # gate clear → keep polling

    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(None, None), session_factory, _reconcile)

    assert exc.value.status_code == 202  # not 409 — UI keeps polling
    assert fired["n"] == 1  # reconcile was awaited
    session_factory.assert_not_awaited()  # no mint before the row is active


@pytest.mark.asyncio
async def test_resolve_returns_402_when_reconcile_reports_blocked(auth_ctx):
    """reconcile returning a blocked-gate payload (no subscription/credits/
    provider) → 402 with that payload, so the UI shows the subscribe/setup CTA
    instead of spinning forever on a workspace that can never become active."""
    session_factory = AsyncMock()

    blocked_payload = {"detail": "subscription required", "blocked": {"code": "subscription_required"}}

    async def _reconcile():
        return blocked_payload

    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(None, None), session_factory, _reconcile)

    assert exc.value.status_code == 402
    assert exc.value.detail == blocked_payload
    session_factory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ws_status,identity_status,expected",
    [
        ("provisioning", None, 202),
        ("seeding", None, 202),
        ("active", "seeding", 202),
        ("failed", None, 503),
        ("active", "failed", 503),
        ("disabled", None, 503),
    ],
)
async def test_resolve_status_codes(auth_ctx, active_ws, identity, ws_status, identity_status, expected):
    active_ws.status = ws_status
    identity.status = identity_status
    session_factory = AsyncMock()
    reconcile = AsyncMock()
    with pytest.raises(TeamsContextError) as exc:
        await _resolve(auth_ctx, _repo(active_ws, identity), session_factory, reconcile)
    assert exc.value.status_code == expected
    session_factory.assert_not_awaited()
    reconcile.assert_not_awaited()
