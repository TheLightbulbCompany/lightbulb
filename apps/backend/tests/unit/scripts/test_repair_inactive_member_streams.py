"""The one-off repair turns on only a CURRENT member's inactive stream, and a dry run writes nothing."""

from unittest.mock import AsyncMock, patch

import pytest
from scripts import repair_inactive_member_streams as script

OWNER = "org_1"
STREAMS = {
    "user_back": {"active": False, "channelKey": "old"},  # removed, re-added: Clerk lists them
    "user_live": {"active": True, "channelKey": "k"},
    "user_gone": {"active": False, "channelKey": "old2"},  # removed: Clerk does not list them
}


@pytest.fixture
def env():
    members = [{"user_id": "user_back"}, {"user_id": "user_live"}, {"user_id": "user_new"}]
    with (
        patch.object(script.container_repo, "scan_all", AsyncMock(return_value=[{"owner_id": OWNER}])),
        patch.object(script.clerk_admin, "list_organization_members", AsyncMock(return_value=members)),
        patch.object(script.messaging_repo, "get_user_stream", AsyncMock(side_effect=lambda o, u: STREAMS.get(u))),
        patch.object(script.messaging_repo, "ensure_user_stream", AsyncMock(return_value="new")) as ensure,
    ):
        yield ensure


@pytest.mark.asyncio
async def test_apply_repairs_only_an_inactive_current_member(env):
    assert await script.main(["--apply"]) == 0
    env.assert_awaited_once_with(OWNER, "user_back", rejoin=True)


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(env, capsys):
    assert await script.main([]) == 0
    env.assert_not_awaited()
    assert capsys.readouterr().out.strip() == f"{OWNER} user_back would-repair"
