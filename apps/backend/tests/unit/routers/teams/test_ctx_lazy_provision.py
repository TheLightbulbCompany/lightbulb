"""Tests for the BFF ``_ctx`` Depends helper.

After Task 9, ``_ctx`` is a thin wrapper around
``deps.resolve_teams_context``: it builds the session_factory closure
and propagates whatever the resolver returns or raises. The lazy
autoprovision branch was deleted in favor of the canonical
``/container/status`` provisioning trigger (Task 8), which every
authenticated user already polls during pre-ready state.

The 409 / 202 / 503 status codes from ``resolve_teams_context``
propagate as-is. A 409 indicates an operator-level error
(missing paperclip-companies row despite a healthy container) rather
than a self-heal opportunity.
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

    with patch.object(
        agents_mod.deps_mod,
        "resolve_teams_context",
        new_callable=AsyncMock,
        return_value=fake_ctx,
    ):
        result = await agents_mod._ctx(auth=_auth())

    assert result is fake_ctx


@pytest.mark.asyncio
async def test_ctx_does_not_autoprovision():
    """After Task 9, _ctx is simplified — it does not autoprovision
    on the no-DDB-row branch. The canonical /container/status trigger
    (Task 8) handles all provisioning. A 409 from
    ``resolve_teams_context`` propagates unchanged.
    """
    from routers.teams import agents as agents_mod
    from routers.teams.deps import TeamsContextError

    raise_409 = TeamsContextError(status_code=409, detail="team workspace not provisioned")

    with patch.object(
        agents_mod.deps_mod,
        "resolve_teams_context",
        new_callable=AsyncMock,
        side_effect=raise_409,
    ):
        with pytest.raises(TeamsContextError) as exc:
            await agents_mod._ctx(auth=_auth())

    assert exc.value.status_code == 409
    assert exc.value.detail == "team workspace not provisioned"
    # The autoprovision import is gone from agents.py — confirm.
    assert not hasattr(agents_mod, "ensure_paperclip_workspace")


@pytest.mark.asyncio
async def test_ctx_propagates_409_for_org_context():
    """Org context: missing row stays 409. The Clerk webhook owned org
    provisioning historically; with the canonical /container/status
    trigger it owns it for everyone now. _ctx never branches on
    is_org_context — it just propagates."""
    from routers.teams import agents as agents_mod
    from routers.teams.deps import TeamsContextError

    raise_409 = TeamsContextError(status_code=409, detail="team workspace not provisioned")

    with patch.object(
        agents_mod.deps_mod,
        "resolve_teams_context",
        new_callable=AsyncMock,
        side_effect=raise_409,
    ):
        with pytest.raises(TeamsContextError) as exc:
            await agents_mod._ctx(auth=_auth(org_id="org_x", is_org_context=True))

    assert exc.value.status_code == 409


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
            await agents_mod._ctx(auth=_auth())

    assert exc.value.status_code == 503
