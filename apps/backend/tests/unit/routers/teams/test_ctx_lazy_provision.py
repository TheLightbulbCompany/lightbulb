"""Tests for the BFF ``_ctx`` Depends helper.

``_ctx`` is a thin wrapper around ``deps.resolve_teams_context``: it
wires a ``reconcile`` callback
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
    )

    with patch.object(
        agents_mod.deps_mod,
        "resolve_teams_context",
        new_callable=AsyncMock,
        return_value=fake_ctx,
    ) as mock_resolve:
        await agents_mod._ctx(auth=_auth())

    reconcile = mock_resolve.await_args.kwargs["reconcile"]
    assert callable(reconcile)


@pytest.mark.asyncio
async def test_self_heal_reconcile_backgrounds_provision_when_gate_clear():
    """Gate clear → return None (→ 202 keep-polling) and schedule the actual
    provisioning OFF the request path. ensure_user_ready must NOT be awaited
    inline: a transient failure would escape resolve_teams_context as a 500 and
    strand the user (the Teams frontend only auto-retries the 202 sentinel)."""
    from routers.teams import agents as agents_mod

    initialized = False

    async def initialize(_auth, owner_id):
        nonlocal initialized
        assert owner_id == "org_1"
        initialized = True

    async def gate(**_kwargs):
        if initialized:
            return None
        blocked = MagicMock()
        blocked.to_payload.return_value = {"blocked": {"code": "team_billing_required"}}
        return blocked

    auth = _auth(user_id="owner1", org_id="org_1", is_org_context=True)
    auth.is_org_admin = True
    with (
        patch.object(agents_mod, "ensure_workspace_trial_default", new=initialize, create=True),
        patch.object(agents_mod, "evaluate_provision_gate", new=gate),
        patch.object(agents_mod, "_provision_after_gate") as mock_bg,
        patch.object(agents_mod.asyncio, "create_task", side_effect=lambda c: c.close()) as mock_create,
    ):
        result = await agents_mod._self_heal_reconcile(auth)

    assert result is None  # gate clear → 202 keep-polling
    assert initialized is True
    mock_bg.assert_called_once_with(auth, "org_1")  # scheduled, not awaited inline
    mock_create.assert_called_once()  # dispatched as a background task


@pytest.mark.asyncio
async def test_self_heal_reconcile_returns_payload_and_skips_provision_when_gate_up():
    """Gate is up (no subscription / credits / provider) → return the blocked
    payload and schedule NO provisioning. Otherwise ensure_user_ready no-ops
    forever and the UI spins on 202 instead of showing the subscribe CTA
    (Codex P2)."""
    from routers.teams import agents as agents_mod

    gate = MagicMock()
    gate.to_payload.return_value = {"blocked": {"code": "subscription_required"}}
    auth = _auth(user_id="owner1", org_id="org_1", is_org_context=True)
    auth.is_org_admin = True
    with (
        patch.object(
            agents_mod,
            "ensure_workspace_trial_default",
            new_callable=AsyncMock,
            create=True,
        ),
        patch.object(agents_mod, "evaluate_provision_gate", new_callable=AsyncMock, return_value=gate),
        patch.object(agents_mod, "_provision_after_gate") as mock_bg,
        patch.object(agents_mod.asyncio, "create_task") as mock_create,
    ):
        result = await agents_mod._self_heal_reconcile(auth)

    assert result == {"blocked": {"code": "subscription_required"}}
    mock_bg.assert_not_called()  # blocked → never launch provisioning
    mock_create.assert_not_called()  # no background task on a blocked gate


@pytest.mark.asyncio
async def test_provision_after_gate_calls_ensure_user_ready_with_no_flag():
    """The /teams self-heal hands the WHOLE decision to ensure_user_ready — no
    caller-derived container flag. The old ``provision_container`` kwarg made
    this call site re-derive "is the box up?" from ECS, and its
    already-running/parked answers silently skipped the SEED too, which is how
    an owner entering via /teams on a running container sat on 202 forever.
    ensure_user_ready now reads the same row and reconciles the seed itself."""
    from routers.teams import agents as agents_mod

    auth = _auth(user_id="owner1")
    with patch.object(agents_mod, "ensure_user_ready", new_callable=AsyncMock) as mock_fire:
        await agents_mod._provision_after_gate(auth, "owner1")

    mock_fire.assert_awaited_once_with(auth)


@pytest.mark.asyncio
async def test_provision_after_gate_swallows_ensure_user_ready_failure():
    """A transient ensure_user_ready failure in the background task is logged
    and swallowed — it must NOT propagate (the request already returned 202;
    the next /teams poll re-fires)."""
    from routers.teams import agents as agents_mod

    auth = _auth(user_id="owner1")
    with patch.object(
        agents_mod,
        "ensure_user_ready",
        new_callable=AsyncMock,
        side_effect=RuntimeError("paperclip 503"),
    ):
        # Does not raise.
        await agents_mod._provision_after_gate(auth, "owner1")


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
            await agents_mod._ctx(auth=_auth(org_id="org_x", is_org_context=True))

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
            await agents_mod._ctx(auth=_auth())

    assert exc.value.status_code == 503
