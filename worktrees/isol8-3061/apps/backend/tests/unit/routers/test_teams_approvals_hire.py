"""The approve handler's marketplace-install hook.

Approving a marketplace hire has to do two things: flip the agent live in
Paperclip AND install its workspace on Isol8's side. Wiring only the first
would hand the owner an agent with no workspace while the UI says "approved" —
the silent-failure shape this codebase has been bitten by before.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers.teams import approvals as approvals_router


def _ctx(owner_id="user_x"):
    ctx = MagicMock()
    ctx.owner_id = owner_id
    ctx.session_cookie = "ck"
    return ctx


@pytest.mark.asyncio
@patch("routers.teams.approvals.marketplace_hire.deploy_for_approval", new_callable=AsyncMock)
async def test_approve_installs_the_workspace_for_the_approved_hire(deploy):
    approved = {"id": "ap_1", "payload": {"agentId": "ag_1"}}
    admin = MagicMock()
    admin.approve_approval = AsyncMock(return_value=approved)

    with patch.object(approvals_router._agents, "_admin", return_value=admin):
        result = await approvals_router.approve(approval_id="ap_1", body=MagicMock(note=None), ctx=_ctx())

    assert result == approved
    # The install runs against the SAME row Paperclip just approved, and is
    # scoped to the approving user's own container.
    deploy.assert_awaited_once_with(approval=approved, owner_id="user_x")


@pytest.mark.asyncio
@patch("routers.teams.approvals.marketplace_hire.deploy_for_approval", new_callable=AsyncMock)
async def test_a_failed_install_surfaces_to_the_approving_user(deploy):
    """The owner must not be told a hire succeeded when its workspace never
    landed — deploy_for_approval already rolled the agent back."""
    deploy.side_effect = RuntimeError("listing is no longer published")
    admin = MagicMock()
    admin.approve_approval = AsyncMock(return_value={"id": "ap_1", "payload": {}})

    with patch.object(approvals_router._agents, "_admin", return_value=admin):
        with pytest.raises(RuntimeError, match="no longer published"):
            await approvals_router.approve(approval_id="ap_1", body=MagicMock(note=None), ctx=_ctx())


@pytest.mark.asyncio
@patch("routers.teams.approvals.marketplace_hire.deploy_for_approval", new_callable=AsyncMock)
async def test_approve_still_proxies_non_marketplace_approvals_unchanged(deploy):
    """A budget-override or CEO-strategy approval keeps its existing behaviour;
    the hook is a no-op for them (deploy_for_approval self-filters)."""
    approved = {"id": "ap_2", "type": "budget_override_required", "payload": {}}
    admin = MagicMock()
    admin.approve_approval = AsyncMock(return_value=approved)

    with patch.object(approvals_router._agents, "_admin", return_value=admin):
        result = await approvals_router.approve(approval_id="ap_2", body=MagicMock(note="ok"), ctx=_ctx())

    assert result == approved
    admin.approve_approval.assert_awaited_once()
