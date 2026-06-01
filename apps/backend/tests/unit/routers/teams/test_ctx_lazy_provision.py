"""Tests for the BFF ``_ctx`` Depends helper.

``_ctx`` is a thin wrapper around ``deps.resolve_teams_context``: it
builds the session_factory closure, wires a ``reconcile`` callback
(``ensure_user_ready(auth)``), and propagates whatever the resolver
returns or raises.

Self-heal: ``/teams`` never polls ``/container/status``, so a missing
Paperclip row used to wedge the member on a terminal 409 until they
opened ``/chat``. ``_ctx`` now passes ``reconcile`` so the resolver fires
provisioning itself and returns 202 — the UI keeps polling until the row
goes active. The 202 / 503 status codes still propagate as-is.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def _auth(user_id: str = "u1", org_id: str | None = None, is_org_context: bool = False) -> MagicMock:
    auth = MagicMock()
    auth.user_id = user_id
    auth.org_id = org_id
    auth.is_org_context = is_org_context
    return auth


def _req() -> MagicMock:
    """Minimal Request stand-in for ``_ctx``: a cookie jar + a writable
    ``state`` for the 401-self-heal user-id stash."""

    class _State:
        pass

    req = MagicMock()
    req.cookies = {}
    req.state = _State()
    return req


@pytest.mark.asyncio
async def test_ctx_returns_context_when_resolve_succeeds():
    """Happy path — resolve_teams_context returned a context, _ctx
    passes it through unchanged."""
    from routers.teams import agents as agents_mod
    from routers.teams.deps import TeamsContext

    fake_ctx = TeamsContext(
        user_id="u1",
        org_id=None,
        owner_id="u1",
        company_id="co_abc",
        paperclip_user_id="pcu_xyz",
        session_cookie="cookie",
    )

    req = _req()
    with patch.object(
        agents_mod.deps_mod,
        "resolve_teams_context",
        new_callable=AsyncMock,
        return_value=fake_ctx,
    ):
        result = await agents_mod._ctx(req, MagicMock(), auth=_auth())

    assert result is fake_ctx
    # _ctx stamps the user id for the 401 self-heal handler.
    assert req.state.teams_user_id == "u1"


@pytest.mark.asyncio
async def test_ctx_passes_reconcile_to_resolver():
    """_ctx wires a ``reconcile`` callback into ``resolve_teams_context`` so a
    missing Paperclip row self-heals: /teams drives provisioning itself
    instead of wedging the member on a 409 until they open /chat."""
    from routers.teams import agents as agents_mod
    from routers.teams.deps import TeamsContext

    fake_ctx = TeamsContext(
        user_id="u1",
        org_id=None,
        owner_id="u1",
        company_id="co",
        paperclip_user_id="pcu",
        session_cookie="cookie",
    )

    with patch.object(
        agents_mod.deps_mod,
        "resolve_teams_context",
        new_callable=AsyncMock,
        return_value=fake_ctx,
    ) as mock_resolve:
        await agents_mod._ctx(_req(), MagicMock(), auth=_auth())

    reconcile = mock_resolve.await_args.kwargs["reconcile"]
    assert callable(reconcile)


def _ecs_mock(*, container):
    """ECS manager double whose resolve_running_container returns
    ``(container, ip)``. ``container=None`` => no running container."""
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(container, None))
    return ecs


@pytest.mark.asyncio
async def test_self_heal_reconcile_starts_container_when_none_running():
    """No running container (owner's first entry point is /teams) →
    provision_container=True so the container actually gets created. Otherwise
    the Paperclip row goes active, polling stops, and agents can't run until
    the owner visits /chat (Codex P2)."""
    from routers.teams import agents as agents_mod

    auth = _auth(user_id="owner1")
    with (
        patch.object(agents_mod, "get_ecs_manager", return_value=_ecs_mock(container=None)),
        patch.object(agents_mod, "ensure_user_ready", new_callable=AsyncMock) as mock_fire,
    ):
        await agents_mod._self_heal_reconcile(auth)

    mock_fire.assert_awaited_once_with(auth, provision_container=True)


@pytest.mark.asyncio
async def test_self_heal_reconcile_does_not_restart_running_container():
    """A container is already running (existing multi-admin/joined-org case) →
    provision_container=False so /teams self-heal NEVER force-redeploys the
    live shared container (Codex P1)."""
    from routers.teams import agents as agents_mod

    auth = _auth(user_id="admin1", org_id="org_x", is_org_context=True)
    running = {"status": "running"}
    with (
        patch.object(agents_mod, "get_ecs_manager", return_value=_ecs_mock(container=running)),
        patch.object(agents_mod, "ensure_user_ready", new_callable=AsyncMock) as mock_fire,
    ):
        await agents_mod._self_heal_reconcile(auth)

    mock_fire.assert_awaited_once_with(auth, provision_container=False)


@pytest.mark.asyncio
async def test_self_heal_reconcile_falls_back_to_no_restart_on_ecs_error():
    """If container state can't be determined, fall back to the safe
    no-restart path (Paperclip-only); the /chat poll backstops the container."""
    from routers.teams import agents as agents_mod

    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(side_effect=RuntimeError("ecs down"))
    auth = _auth(user_id="owner1")
    with (
        patch.object(agents_mod, "get_ecs_manager", return_value=ecs),
        patch.object(agents_mod, "ensure_user_ready", new_callable=AsyncMock) as mock_fire,
    ):
        await agents_mod._self_heal_reconcile(auth)

    mock_fire.assert_awaited_once_with(auth, provision_container=False)


@pytest.mark.asyncio
async def test_ctx_self_heal_202_propagates_for_org_context():
    """Org context: a missing row now self-heals to 202 (the resolver fires
    reconcile + returns 202). _ctx never branches on is_org_context — it just
    propagates whatever the resolver raises."""
    from routers.teams import agents as agents_mod
    from routers.teams.deps import TeamsContextError

    raise_202 = TeamsContextError(status_code=202, detail="team workspace provisioning")

    with patch.object(
        agents_mod.deps_mod,
        "resolve_teams_context",
        new_callable=AsyncMock,
        side_effect=raise_202,
    ):
        with pytest.raises(TeamsContextError) as exc:
            await agents_mod._ctx(_req(), MagicMock(), auth=_auth(org_id="org_x", is_org_context=True))

    assert exc.value.status_code == 202


@pytest.mark.asyncio
async def test_ctx_propagates_other_status_codes_unchanged():
    """A 503 (failed) or 202 (already provisioning) from
    resolve_teams_context propagates unchanged."""
    from routers.teams import agents as agents_mod
    from routers.teams.deps import TeamsContextError

    raise_503 = TeamsContextError(status_code=503, detail="team workspace provisioning failed")

    with patch.object(
        agents_mod.deps_mod,
        "resolve_teams_context",
        new_callable=AsyncMock,
        side_effect=raise_503,
    ):
        with pytest.raises(TeamsContextError) as exc:
            await agents_mod._ctx(_req(), MagicMock(), auth=_auth())

    assert exc.value.status_code == 503
